import json
from pathlib import Path
import tempfile
import unittest

from scene_factory.schema import answer_from_registry, read_plan
from scene_factory.download import glb_header
from scene_factory.gateway import normalize_usage


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

    def test_deepseek_cache_usage(self):
        usage = normalize_usage({"input_tokens": 100, "prompt_cache_hit_tokens": 80,
                                 "prompt_cache_miss_tokens": 20, "output_tokens": 9}, "anthropic")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_tokens"], 80)
        self.assertAlmostEqual(usage["cache_hit_rate"], 0.8)


if __name__ == "__main__":
    unittest.main()
