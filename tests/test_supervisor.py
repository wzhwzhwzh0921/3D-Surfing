import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scene_factory.cli import render_report
from scene_factory.runner import combined_usage, run_model_phase, supervise_and_repair
from scene_factory.supervisor import (LEVELS, VIEWS, checked_image_reads,
                                      evidence_hashes, extract_result, review_outcome, validate_review)


def verdicts(simple="pass", medium="pass", hard="pass"):
    rows = {}
    for level, verdict in zip(LEVELS, (simple, medium, hard)):
        rows[level] = {"verdict": verdict, "reason": f"Visible evidence for {level}",
                       "evidence_views": [f"{level}/start.png", f"{level}/answer_view.png"]}
        if verdict == "revise":
            rows[level]["repair_instruction"] = "Move the target and re-probe"
    return {"schema_version": "sceneqa_supervisor_v1", "tiers": rows}


def validation_fixture():
    return {"results": [{"level": level, "question": f"Question for {level}?",
                         "answer": {"value": level}} for level in LEVELS]}


def write_review_stream(path, work, report, *, extra_read=None):
    events = []
    for level in LEVELS:
        for view in VIEWS:
            events.append({"type": "assistant", "message": {"content": [{"type": "tool_use",
                "name": "Read", "input": {"file_path": str(work / "output" / level / f"{view}.png")}}]}})
    if extra_read:
        events.append({"type": "assistant", "message": {"content": [{"type": "tool_use",
            "name": "Read", "input": {"file_path": str(work / extra_read)}}]}})
    events.append({"type": "result", "is_error": False, "result": json.dumps(report)})
    path.write_text("\n".join(json.dumps(event) for event in events))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        for level in LEVELS:
            folder = self.work / "output" / level
            folder.mkdir(parents=True)
            for view in VIEWS:
                (folder / f"{view}.png").write_bytes(b"PNG fixture")
        (self.work / "source.glb").write_bytes(b"source fixture")
        self.original_plan = {"variants": [{"level": level, "version": 0} for level in LEVELS]}
        (self.work / "plan.json").write_text(json.dumps(self.original_plan))
        (self.work / "candidates.json").write_text("original candidates")
        (self.work / "probe").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_review_reads_images_only_and_requires_all_tiers(self):
        stream = self.work / "review.jsonl"
        report = verdicts(medium="revise")
        write_review_stream(stream, self.work, report)
        self.assertEqual(len(checked_image_reads(stream, self.work)), 12)
        self.assertEqual(review_outcome(validate_review(extract_result(stream))), "revise")
        write_review_stream(stream, self.work, report, extra_read="plan.json")
        with self.assertRaisesRegex(ValueError, "outside the rendered evidence"):
            checked_image_reads(stream, self.work)

    def test_reviewer_cli_has_read_only_tools_and_no_upstream_secret(self):
        @contextmanager
        def fake_bridge(**_kwargs):
            yield SimpleNamespace(url="http://127.0.0.1:1234", token="loopback-token")

        captured = {}
        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return SimpleNamespace(returncode=0)

        with patch("scene_factory.runner.bridge", fake_bridge), \
             patch("scene_factory.runner.shutil.which", return_value="/usr/bin/claude"), \
             patch("scene_factory.runner.subprocess.run", side_effect=fake_run), \
             patch.dict("os.environ", {"TOKENHUB_API_KEY": "private-key"}):
            run_model_phase(self.work, prompt="Review evidence", system="Supervisor",
                            tools="Read", stream_path=self.work / "stream.jsonl",
                            stderr_path=self.work / "stderr.log",
                            usage_path=self.work / "usage.jsonl", provider="tokenhub",
                            key="private-key", model="judge", upstream="https://example.test",
                            timeout=30)
        command = captured["command"]
        self.assertEqual(command[command.index("--tools") + 1], "Read")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read")
        self.assertNotIn("private-key", str(command))
        self.assertNotIn("TOKENHUB_API_KEY", captured["env"])
        self.assertEqual(captured["env"]["ANTHROPIC_AUTH_TOKEN"], "loopback-token")

    def test_phase_usage_preserves_cache_hit_rate(self):
        state = {"usage": {"completed_api_calls": 2, "input_tokens": 100,
                           "cached_tokens": 80, "output_tokens": 10},
                 "supervisor": {"rounds": [{"usage": {"completed_api_calls": 1,
                     "input_tokens": 50, "cached_tokens": 20, "output_tokens": 5},
                     "repair_usage": {"completed_api_calls": 1, "input_tokens": 50,
                                      "cached_tokens": 50, "output_tokens": 5}}]}}
        usage = combined_usage(state)
        self.assertEqual(usage["completed_api_calls"], 4)
        self.assertEqual(usage["input_tokens"], 200)
        self.assertEqual(usage["cache_hit_rate"], 0.75)

    def test_repair_rechecks_and_keeps_improvement(self):
        reports = [verdicts(medium="revise"), verdicts()]
        calls = []

        def phase(work, **kwargs):
            name = kwargs["stream_path"].name
            calls.append((name, kwargs["model"]))
            if name.startswith("supervisor"):
                write_review_stream(kwargs["stream_path"], work, reports.pop(0))
            else:
                changed = json.loads((work / "plan.json").read_text())
                changed["variants"][1]["version"] = 1
                (work / "plan.json").write_text(json.dumps(changed))
                kwargs["stream_path"].write_text('{"type":"result","result":"done"}')
            return {"input_tokens": 10}

        state = {"status": "candidate", "source": {}}
        with patch("scene_factory.runner.run_model_phase", side_effect=phase), \
             patch("scene_factory.runner.finalize_attempt", return_value=(validation_fixture(), "accepted", {})), \
             patch("scene_factory.runner.read_plan", side_effect=lambda p: json.loads(p.read_text())):
            supervise_and_repair(self.work, "scene", state, validation_fixture(), provider="tokenhub",
                                 key="dummy", model="dummy", upstream="dummy", timeout=30,
                                 max_repairs=1, supervisor_model="independent-judge")
        self.assertEqual(state["status"], "candidate")
        self.assertEqual(state["supervisor"]["status"], "passed")
        self.assertEqual(json.loads((self.work / "plan.json").read_text())["variants"][1]["version"], 1)
        self.assertEqual(calls, [("supervisor_round_0.jsonl", "independent-judge"),
                                 ("repair_round_1.jsonl", "dummy"),
                                 ("supervisor_round_1.jsonl", "independent-judge")])

    def test_unimproved_repair_restores_prior_artifacts(self):
        reports = [verdicts(medium="revise"), verdicts(medium="revise")]

        def phase(work, **kwargs):
            name = kwargs["stream_path"].name
            if name.startswith("supervisor"):
                write_review_stream(kwargs["stream_path"], work, reports.pop(0))
            else:
                changed = json.loads((work / "plan.json").read_text())
                changed["variants"][1]["version"] = 1
                (work / "plan.json").write_text(json.dumps(changed))
                kwargs["stream_path"].write_text('{"type":"result","result":"done"}')
            return {}

        state = {"status": "candidate", "source": {}}
        with patch("scene_factory.runner.run_model_phase", side_effect=phase), \
             patch("scene_factory.runner.finalize_attempt", return_value=(validation_fixture(), "accepted", {})), \
             patch("scene_factory.runner.read_plan", side_effect=lambda p: json.loads(p.read_text())):
            supervise_and_repair(self.work, "scene", state, validation_fixture(), provider="tokenhub",
                                 key="dummy", model="dummy", upstream="dummy", timeout=30,
                                 max_repairs=1)
        self.assertEqual(state["status"], "supervisor_rejected")
        self.assertEqual(json.loads((self.work / "plan.json").read_text()), self.original_plan)

    def test_repair_must_preserve_passing_tiers(self):
        def phase(work, **kwargs):
            name = kwargs["stream_path"].name
            if name.startswith("supervisor"):
                write_review_stream(kwargs["stream_path"], work, verdicts(medium="revise"))
            else:
                changed = json.loads((work / "plan.json").read_text())
                changed["variants"][0]["version"] = 1
                (work / "plan.json").write_text(json.dumps(changed))
                kwargs["stream_path"].write_text('{"type":"result","result":"done"}')
            return {}

        state = {"status": "candidate", "source": {}}
        with patch("scene_factory.runner.run_model_phase", side_effect=phase), \
             patch("scene_factory.runner.finalize_attempt", return_value=(validation_fixture(), "accepted", {})), \
             patch("scene_factory.runner.read_plan", side_effect=lambda p: json.loads(p.read_text())):
            supervise_and_repair(self.work, "scene", state, validation_fixture(), provider="tokenhub",
                                 key="dummy", model="dummy", upstream="dummy", timeout=30,
                                 max_repairs=1)
        self.assertEqual(state["supervisor"]["status"], "repair_invalid")
        self.assertEqual(json.loads((self.work / "plan.json").read_text()), self.original_plan)

    def test_repair_cannot_change_source_glb(self):
        def phase(work, **kwargs):
            name = kwargs["stream_path"].name
            if name.startswith("supervisor"):
                write_review_stream(kwargs["stream_path"], work, verdicts(medium="revise"))
            else:
                (work / "source.glb").write_bytes(b"mutated")
                kwargs["stream_path"].write_text('{"type":"result","result":"done"}')
            return {}

        state = {"status": "candidate", "source": {}}
        with patch("scene_factory.runner.run_model_phase", side_effect=phase), \
             patch("scene_factory.runner.finalize_attempt", return_value=(validation_fixture(), "accepted", {})), \
             patch("scene_factory.runner.read_plan", side_effect=lambda p: json.loads(p.read_text())):
            supervise_and_repair(self.work, "scene", state, validation_fixture(), provider="tokenhub",
                                 key="dummy", model="dummy", upstream="dummy", timeout=30,
                                 max_repairs=1)
        self.assertEqual(state["supervisor"]["status"], "repair_invalid")
        self.assertEqual((self.work / "source.glb").read_bytes(), b"source fixture")

    def test_backup_failure_does_not_delete_original_artifacts(self):
        def phase(work, **kwargs):
            write_review_stream(kwargs["stream_path"], work, verdicts(medium="revise"))
            return {}

        state = {"status": "candidate", "source": {}}
        with patch("scene_factory.runner.run_model_phase", side_effect=phase), \
             patch("scene_factory.runner.snapshot_attempt", side_effect=OSError("disk full")), \
             patch("scene_factory.runner.read_plan", side_effect=lambda p: json.loads(p.read_text())):
            supervise_and_repair(self.work, "scene", state, validation_fixture(), provider="tokenhub",
                                 key="dummy", model="dummy", upstream="dummy", timeout=30,
                                 max_repairs=1)
        self.assertEqual(state["supervisor"]["status"], "backup_failed")
        self.assertEqual(json.loads((self.work / "plan.json").read_text()), self.original_plan)

    def test_review_page_uses_only_report_matching_current_assets(self):
        rows = []
        for level in LEVELS:
            (self.work / "output" / level / "scene.glb").write_bytes(b"GLB")
            rows.append({"level": level, "sha256": level,
                         "question": f"Question {level}?",
                         "answer": {"value": level, "source": "model_visual"},
                         "hidden_copy_ids_at_start": [],
                         "all_copies_discoverable": True})
        (self.work / "output/validation.json").write_text(
            json.dumps({"status": "candidate", "results": rows}))
        report = verdicts(medium="revise")
        report["artifact_sha256"] = {level: level for level in LEVELS}
        report["evidence_sha256"] = evidence_hashes(self.work)
        (self.work / "supervisor_round_0.json").write_text(json.dumps(report))
        page = self.work / "review.html"
        render_report(self.work, page)
        self.assertIn("独立监督：revise", page.read_text())
        report["artifact_sha256"]["medium"] = "stale"
        (self.work / "supervisor_round_1.json").write_text(json.dumps(report))
        render_report(self.work, page)
        self.assertEqual(page.read_text().count("独立监督：revise"), 1)
        (self.work / "output/medium/answer_view.png").write_bytes(b"different pixels")
        render_report(self.work, page)
        self.assertNotIn("独立监督：revise", page.read_text())


if __name__ == "__main__":
    unittest.main()
