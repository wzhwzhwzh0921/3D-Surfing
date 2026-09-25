import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scene_factory.blind_review import render_blind_trial
from scene_factory.blind_trace import discover_trace_paths, summarize_traces
from scene_factory.challenge_gallery import render_challenge_gallery


class BlindTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "scene_a"
        folder = self.work / "output/simple"
        folder.mkdir(parents=True)
        self.asset = folder / "scene.glb"
        self.asset.write_bytes(b"test glb fixture")
        self.digest = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.question = "柜子背面写了什么？"
        self.camera = {"azimuth_deg": 315, "elevation_deg": 15}
        validation = {"scene_id": "scene_a", "results": [{
            "level": "simple", "question": self.question,
            "answer": {"value": "SECRET_BUILDER_ANSWER"},
            "sha256": self.digest, "start_camera": self.camera}]}
        (self.work / "output/validation.json").write_text(json.dumps(validation))
        self.page = self.work / "blind.html"
        self.config = render_blind_trial(self.work, "simple", self.page)
        self.traces = self.root / "traces"
        self.traces.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def trace(self, *, actions=None, answer="字母 A", status="observed"):
        actions = actions or []
        return {"schema_version": "sceneqa_blind_trial_v1",
                "render_protocol": "modelviewer_perspective_v1",
                "scene_id": "scene_a", "level": "simple",
                "question": self.question, "asset_sha256": self.digest,
                "start_camera_blender": self.camera,
                "answer": answer, "completion_status": status,
                "actions": actions, "successful_action_count": len(actions),
                "final_observation_id": f"frame_{len(actions)+1:04d}"}

    def test_page_withholds_builder_answer_and_maps_start_orbit(self):
        page = self.page.read_text()
        self.assertEqual(self.config["start_orbit"], "45deg 75deg auto")
        self.assertIn(self.question, page)
        self.assertNotIn("SECRET_BUILDER_ANSWER", page)
        self.assertIn('window.sceneqaAgent=', page)
        self.assertIn("stale_observation", page)
        self.asset.write_bytes(b"replaced")
        with self.assertRaisesRegex(ValueError, "GLB differs"):
            render_blind_trial(self.work, "simple", self.page)

    def test_summary_counts_only_current_sequential_traces(self):
        first = {"tool": "orbit", "args": {"yaw": 30, "pitch": 0},
                 "before_observation_id": "frame_0001", "after_observation_id": "frame_0002"}
        second = {"tool": "focus_at", "args": {"x": 0.5, "y": 0.5},
                  "before_observation_id": "frame_0002", "after_observation_id": "frame_0003"}
        good = self.traces / "good.json"
        good.write_text(json.dumps(self.trace(actions=[first, second])))
        guessed = self.traces / "guessed.json"
        guessed.write_text(json.dumps(self.trace(actions=[], status="guessed")))
        invalid = self.traces / "invalid.json"
        invalid.write_text(json.dumps(self.trace(actions=[second])))
        stale = self.traces / "stale.json"
        old = self.trace()
        old["asset_sha256"] = "old"
        stale.write_text(json.dumps(old))
        report = summarize_traces(sorted(self.traces.glob("*.json")), self.root)
        self.assertEqual(report["valid_traces"], 2)
        self.assertEqual(len(report["rejected"]["invalid"]), 1)
        self.assertEqual(len(report["rejected"]["stale"]), 1)
        self.assertEqual(report["by_level"]["simple"]["median_actions_observed"], 2)
        self.assertEqual(report["overall"]["tool_calls"], {"focus_at": 1, "orbit": 1})
        self.assertEqual(report["overall"]["unique_assets"], 1)

    def test_challenge_flags_do_not_claim_a_gold_answer(self):
        path = self.traces / "zero-action.json"
        path.write_text(json.dumps(self.trace()))
        with patch("scene_factory.blind_trace.current_review",
                   return_value={"tiers": {"simple": {"verdict": "pass"}}}):
            report = summarize_traces([path], self.root)
        self.assertEqual(report["challenge_counts"], {
            "blind_builder_answer_mismatch": 1,
            "supervisor_pass_zero_action_answer": 1})
        self.assertIn("not correctness gold", report["note"])

    def test_no_supervisor_still_flags_navigation_or_start_view_concerns(self):
        traces = []
        for status, answer in (("unsolved", ""), ("guessed", "猜的"),
                               ("observed", "SECRET_BUILDER_ANSWER")):
            path = self.traces / f"{status}.json"
            path.write_text(json.dumps(self.trace(status=status, answer=answer)))
            traces.append(path)
        report = summarize_traces(traces, self.root)
        self.assertEqual(report["challenge_counts"], {
            "blind_guessed_needs_review": 1,
            "blind_unsolved_needs_review": 1,
            "blind_zero_action_answer": 1,
        })

    def test_discovers_nested_completed_runs_but_not_partial_traces(self):
        manual = self.traces / "manual.json"
        manual.write_text(json.dumps(self.trace()))
        run = self.traces / "case_a-simple"
        run.mkdir()
        completed = run / "blind-trace.json"
        completed.write_text(json.dumps(self.trace()))
        (run / "partial-trace.json").write_text(json.dumps(self.trace(status="unsolved")))
        (run / "summary.json").write_text("{}")
        self.assertEqual(discover_trace_paths(self.traces), sorted([manual, completed]))
        report = summarize_traces(discover_trace_paths(self.traces), self.root)
        self.assertEqual(report["valid_traces"], 2)

    def test_challenge_gallery_shows_bound_actions_and_escapes_answers(self):
        run = self.traces / "agent-run"
        frames = run / "observations"
        frames.mkdir(parents=True)
        (frames / "frame_0001.png").write_bytes(b"first frame")
        (frames / "frame_0002.png").write_bytes(b"second frame")
        (self.work / "output/simple/start.png").write_bytes(b"start frame")
        action = {"tool": "orbit", "args": {"yaw": 35, "pitch": 0},
                  "before_observation_id": "frame_0001", "after_observation_id": "frame_0002"}
        (run / "blind-trace.json").write_text(json.dumps(self.trace(
            actions=[action], answer="<img onerror=alert(1)>")))
        output = self.root / "challenge.html"
        result = render_challenge_gallery(self.traces, self.root, output)
        page = output.read_text()
        self.assertEqual(result["challenges"], 1)
        self.assertIn("observations/frame_0002.png", page)
        self.assertIn("盲解答案", page)
        self.assertNotIn("<img onerror=alert(1)>", page)


if __name__ == "__main__":
    unittest.main()
