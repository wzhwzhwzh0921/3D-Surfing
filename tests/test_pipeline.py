import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scene_factory.schema import (answer_from_model, answer_from_registry, read_candidate_pool,
                                  read_plan, select_candidates, validate_selection)
from scene_factory.download import glb_header
from scene_factory.gateway import normalize_usage
from scene_factory.question_types import QUESTION_TYPES, type_order
from scene_factory.runner import observed_evidence_reads, observed_probe_reads, visual_quality_status


def variant(level, kind, count):
    objects = ["Bottle", "Cup", "Box"]
    kinds = ["bottle", "cup", "box"]
    entries = [{"id": k, "kind": k, "objects": [o], "pose": "upright", "region": "table"}
               for k, o in zip(kinds, objects)]
    copies = [{"id": f"{kind}_{i}", "from": kind, "xy": [i, 1],
               "support_object": "Table", "pose": "upright", "region": "table"} for i in range(count)]
    q = {"mode": "compare" if level == "hard" else "count", "kind": kind,
         "text": f"{level} question", "pose": "upright", "region": "table"}
    if level == "hard":
        q["other_kind"] = "cup"
    return {"level": level, "entities": entries, "copies": copies,
            "question": q, "start_camera": {"azimuth_deg": 20, "elevation_deg": 35}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "plan.json"
        self.plan = {"schema_version": "1.0", "variants": [variant("simple", "bottle", 1),
                                                            variant("medium", "cup", 2),
                                                            variant("hard", "box", 3)]}
        self.path.write_text(json.dumps(self.plan))

    def tearDown(self):
        self.temp.cleanup()

    def test_visual_self_rejection_cannot_become_candidate(self):
        path = Path(self.temp.name) / "visual_quality.json"
        self.assertEqual(visual_quality_status(path)[0], "needs_review")
        tiers = {level: {"status": "pass", "reason": "Visible after orbit"}
                 for level in ("simple", "medium", "hard")}
        tiers["medium"] = {"status": "fail", "reason": "Only a fabric corner is visible"}
        path.write_text(json.dumps({"status": "rejected", "tiers": tiers}))
        self.assertEqual(visual_quality_status(path)[0], "visual_rejected")
        path.write_text(json.dumps({"status": "accepted", "tiers": tiers}))
        with self.assertRaisesRegex(ValueError, "cannot accept a failed tier"):
            visual_quality_status(path)

    def test_plan_and_computed_answers(self):
        p = read_plan(self.path)
        self.assertEqual(answer_from_registry(p["variants"][0])["value"], 2)
        self.assertEqual(answer_from_registry(p["variants"][1])["value"], 3)
        self.assertEqual(answer_from_registry(p["variants"][2])["value"], "box")

    def test_reject_same_target_kind(self):
        self.plan["variants"][1]["question"]["kind"] = "bottle"
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "different object kinds"):
            read_plan(self.path)

    def test_reject_missing_copy_source(self):
        self.plan["variants"][0]["copies"][0]["from"] = "ghost"
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "copy source"):
            read_plan(self.path)

    def test_reject_duplicate_original_mesh(self):
        self.plan["variants"][0]["entities"][1]["objects"] = ["Bottle"]
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "assigned to multiple"):
            read_plan(self.path)

    def test_reject_invalid_glb(self):
        bad = Path(self.temp.name) / "bad.glb"
        bad.write_bytes(b"not a glb")
        with self.assertRaisesRegex(ValueError, "Not a binary"):
            glb_header(bad)

    def test_medium_v11_requires_local_occlusion_and_limits_added_targets(self):
        self.plan["schema_version"] = "1.1"
        self.plan["variants"][2]["occlusion_targets"] = [
            {"target_copy_id": "box_0", "blocker_objects": ["Bottle"]},
            {"target_copy_id": "box_1", "blocker_objects": ["Cup"]}]
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "occlusion target"):
            read_plan(self.path)
        self.plan["variants"][1]["occlusion_targets"] = [
            {"target_copy_id": "cup_0", "blocker_objects": ["Box"]}]
        self.path.write_text(json.dumps(self.plan))
        read_plan(self.path)
        self.plan["variants"][1]["copies"].append({"id": "cup_2", "from": "cup",
             "xy": [3, 1], "support_object": "Table"})
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "at most two"):
            read_plan(self.path)

    def test_identify_answer_is_tied_to_hidden_anchor(self):
        self.plan["schema_version"] = "1.1"
        self.plan["variants"][1]["occlusion_targets"] = [
            {"target_copy_id": "cup_0", "blocker_objects": ["Box"]}]
        hard = self.plan["variants"][2]
        hard["copies"][0]["from"] = "cup"
        hard["occlusion_targets"] = [
            {"target_copy_id": "box_0", "blocker_objects": ["Bottle"]},
            {"target_copy_id": "box_1", "blocker_objects": ["Cup"]}]
        hard["question"] = {"mode": "identify", "kind": "box", "answer_copy_id": "box_1",
                            "anchor_blocker_object": "Cup", "text": "杯子后面藏的是什么？"}
        self.path.write_text(json.dumps(self.plan))
        self.assertEqual(answer_from_registry(read_plan(self.path)["variants"][2])["value"], "box")
        hard["question"]["anchor_blocker_object"] = "Bottle"
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "named occluding anchor"):
            read_plan(self.path)

    def test_ten_type_order_is_reproducible_and_complete(self):
        first = type_order("scene-42", 7)
        self.assertEqual(first, type_order("scene-42", 7))
        self.assertEqual(set(first), set(QUESTION_TYPES))
        self.assertEqual(len(first), 10)
        self.assertNotEqual(first, type_order("scene-42", 8))

    def test_v12_separates_type_from_difficulty_and_marks_draft_answer(self):
        self.plan["schema_version"] = "1.2"
        easy, medium, hard = self.plan["variants"]
        easy["question"]["question_type"] = "count"
        medium["occlusion_targets"] = [{"target_copy_id": "cup_0", "blocker_objects": ["Box"]}]
        medium["question"] = {"question_type": "color", "mode": "evidence", "kind": "cup",
                              "text": "杯子是什么颜色？", "declared_answer": "红色", "evidence_ids": ["cup_0"]}
        hard["occlusion_targets"] = [{"target_copy_id": "box_1", "blocker_objects": ["Cup"]}]
        hard["anchor_query"] = {"relation": "nearest", "reference_object": "Bottle",
                                "candidate_objects": ["Cup", "Box"],
                                "answer_anchor_object": "Cup", "target_copy_id": "box_1"}
        hard["question"] = {"question_type": "identity", "mode": "identify", "kind": "box",
                            "answer_copy_id": "box_1", "anchor_blocker_object": "Cup",
                            "text": "离瓶子最近的地标后面是什么？"}
        self.path.write_text(json.dumps(self.plan))
        plan = read_plan(self.path)
        self.assertEqual([v["question"]["question_type"] for v in plan["variants"]],
                         ["count", "color", "identity"])
        self.assertFalse(answer_from_registry(medium)["verified"])
        hard["anchor_query"]["target_copy_id"] = "box_0"
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, "answer target must be occluded"):
            read_plan(self.path)

    def test_deepseek_cache_usage(self):
        usage = normalize_usage({"input_tokens": 100, "prompt_cache_hit_tokens": 80,
                                 "prompt_cache_miss_tokens": 20, "output_tokens": 9}, "anthropic")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_tokens"], 80)
        self.assertAlmostEqual(usage["cache_hit_rate"], 0.8)

    def test_v13_uses_model_answers_for_all_types(self):
        sample = Path(__file__).resolve().parents[1] / "examples/model_answer_garden_plan.json"
        plan = read_plan(sample)
        self.assertEqual(plan["schema_version"], "1.3")
        self.assertEqual([answer_from_model(v)["value"] for v in plan["variants"]],
                         [5, "蓝色", "茶杯和茶碟"])
        self.assertTrue(all(answer_from_model(v)["source"] == "model_visual"
                            for v in plan["variants"]))
        plan["variants"][0]["question"].pop("declared_answer")
        self.path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "declared answer"):
            read_plan(self.path)

    def test_answer_views_must_be_opened_by_agent(self):
        work = Path(self.temp.name)
        stream = work / "claude_stream.jsonl"
        stream.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "output/simple/reveal.png"}},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "cat output/hard/detail.png"}}
        ]}}) + "\n")
        self.assertEqual(observed_evidence_reads(stream, work), ["simple/reveal.png"])

    def test_probe_previews_must_be_opened_before_selection(self):
        work = Path(self.temp.name)
        stream = work / "claude_stream.jsonl"
        stream.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "probe/simple_a/start_1.png"}},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "cat probe/medium_a/start_1.png"}}
        ]}}) + "\n")
        self.assertEqual(observed_probe_reads(stream, work, {"simple_a", "medium_a"}),
                         ["simple_a"])

    def test_selected_candidate_must_have_probed_camera_and_unchanged_placement(self):
        sample = Path(__file__).resolve().parents[1] / "examples/candidate_fixture_pool.json"
        pool = read_candidate_pool(sample)
        selected = [next(c for c in pool["candidates"] if c["candidate_id"] == f"{level}_a")
                    for level in ("simple", "medium", "hard")]
        plan = {"workflow_version": "candidate_probe_v1", "variants": json.loads(json.dumps(selected))}
        report = [{"candidate_id": v["candidate_id"],
                   "probe_protocol_version": "landmark_surface_v2",
                   "candidate_sha256": hashlib.sha256(json.dumps(
                       v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                   "recommended": [
            {"azimuth_deg": 90, "elevation_deg": 35, "eligible": True}]} for v in selected]
        for variant in plan["variants"]:
            variant["start_camera"] = {"azimuth_deg": 90, "elevation_deg": 35}
        validate_selection(plan, pool, report)
        assembled = select_candidates(pool, report, {level: (f"{level}_a", 1)
                                                    for level in ("simple", "medium", "hard")})
        self.path.write_text(json.dumps(assembled))
        self.assertEqual(read_plan(self.path)["workflow_version"], "candidate_probe_v1")
        old_report = json.loads(json.dumps(report))
        old_report[0].pop("probe_protocol_version")
        with self.assertRaisesRegex(ValueError, "old camera protocol"):
            validate_selection(plan, pool, old_report)
        plan["variants"][0]["copies"][0]["xy"][0] += 0.5
        with self.assertRaisesRegex(ValueError, "changed after probe"):
            validate_selection(plan, pool, report)
        plan["variants"][0]["copies"][0]["xy"][0] -= 0.5
        plan["variants"][0]["start_camera"]["azimuth_deg"] = 120
        with self.assertRaisesRegex(ValueError, "did not pass"):
            validate_selection(plan, pool, report)
        pool["candidates"][0]["question"]["text"] = "A changed proposal"
        with self.assertRaisesRegex(ValueError, "changed since probe"):
            validate_selection(assembled, pool, report)


if __name__ == "__main__":
    unittest.main()
