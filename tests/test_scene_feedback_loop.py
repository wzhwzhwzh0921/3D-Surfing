"""Exercise feedback decisions without model calls or 3D processing."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("feedback_loop", ROOT / "tools/scene_feedback_loop.py")
loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop)


class FeedbackLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        seed_dir = self.root / "seed"
        seed_dir.mkdir()
        # Only header integrity is inspected by this controller; no mesh is parsed.
        (seed_dir / "scene.glb").write_bytes(b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little"))
        for name in ("start.png", "reveal.png"):
            (seed_dir / name).write_bytes(b"image fixture")
        self.seed = seed_dir / "result.json"
        loop.save(self.seed, {"terminal": True, "items": [{
            "level": "medium", "question": "Observation question?", "answer": "Model answer",
            "glb": str(seed_dir / "scene.glb"), "initial_camera": {"azimuth": 45},
            "start_image": "start.png", "reveal_image": "reveal.png"}]})
        self.outcomes, self.audits, self.repairs = [], [], []
        self.change = True
        for owner, name, replacement in (
            (loop.reviewer, "audit", self.audit),
            (loop.repair_medium, "repair", self.repair),
            (loop.augment, "key", lambda: "unused-test-key"),
            (loop.augment, "command", self.forbid_remote),
        ):
            mock = patch.object(owner, name, side_effect=replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def forbid_remote(self, *args, **kwargs):
        raise AssertionError("Tests must not launch rendering")

    def audit(self, uid, args, key):
        self.audits.append(str(args.baseline))
        passed = self.outcomes.pop(0)
        if passed == "error":
            result = {"status": "failed", "errors": ["mock network or JSON failure"]}
        else:
            result = {"status": "ok", "judgment": {
                "uid": uid, "pass": passed, "bad_levels": [] if passed else ["medium"],
                "repair_action": "none" if passed else "question", "reason": "Mock opinion"}}
        loop.save(args.output / uid / "record.json", result)
        return result

    def repair(self, previous, audit_path, directory, **kwargs):
        self.repairs.append(str(directory))
        result = json.loads(Path(previous).read_text())
        if self.change:
            result["items"][0]["question"] += " Revised."
        loop.finish_generation(directory, result)
        return directory / "result.json"

    def run_scene(self, name="run", max_repairs=2):
        args = SimpleNamespace(out=self.root / name, max_bytes=35 * 1048576,
                               max_repairs=max_repairs, review_cache=self.root / "cache")
        return loop.process({"uid": "scene", "result": str(self.seed)}, args, None)

    def test_failed_question_is_repaired_then_reviewed(self):
        self.outcomes[:] = [False, True]
        result = self.run_scene()
        self.assertEqual((result["status"], result["repairs"]), ("keep", 1))
        self.assertEqual(len(self.audits), 2)
        self.assertEqual(len(self.repairs), 1)
        self.assertTrue((Path(result["folder"]) / "question.json").is_file())
        self.assertEqual(json.loads(self.seed.read_text())["items"][0]["question"], "Observation question?")

    def test_exhausted_repairs_reject_instead_of_keep(self):
        self.outcomes[:] = [False, False, False]
        result = self.run_scene()
        self.assertEqual((result["status"], result["repairs"]), ("reject", 2))
        self.assertNotIn("folder", result)

    def test_audit_failure_is_operational_failure(self):
        self.outcomes[:] = ["error"]
        result = self.run_scene()
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(result.get("category"), "quality")
        self.assertFalse(self.repairs)

    def test_unchanged_repair_does_not_get_another_judgment(self):
        self.change = False
        self.outcomes[:] = [False]
        result = self.run_scene()
        self.assertEqual((result["status"], result["category"]), ("reject", "no_change"))
        self.assertEqual(len(self.audits), 1)

    def test_completed_run_reuses_result_without_calls(self):
        self.outcomes[:] = [True]
        self.run_scene()
        result = self.run_scene()
        self.assertEqual(result["status"], "keep")
        self.assertTrue(result["cached"])
        self.assertEqual(len(self.audits), 1)

    def test_changed_configuration_failure_cannot_restore_old_keep(self):
        self.outcomes[:] = [True]
        self.run_scene()
        self.outcomes[:] = ["error"]
        result = self.run_scene(max_repairs=1)
        pointer = json.loads((self.root / "run/scene/current_loop.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(pointer["status"], "failed")

    def test_intake_failure_invalidates_previous_delivery(self):
        output = self.root / "main"
        loop.save(output / "scene/current_loop.json", {"run_dir": "old_success", "status": "keep"})
        invalid = self.root / "invalid"
        loop.save(invalid / "current_run.json", {"run_dir": "new", "status": "error"})
        sources = self.root / "sources.jsonl"
        sources.write_text(json.dumps({"uid": "scene", "result": str(invalid / "result.json")}) + "\n")
        with patch.object(sys, "argv", ["loop", "--sources", str(sources), "--out", str(output)]), \
             patch.object(loop, "LazyRemote", return_value=SimpleNamespace(created=False)), \
             contextlib.redirect_stdout(io.StringIO()):
            loop.main()
        pointer = json.loads((output / "scene/current_loop.json").read_text())
        self.assertEqual(pointer, {"run_dir": None, "status": "failed", "reason": "ValueError: Seed's current version is not complete"})
        self.assertFalse(self.audits)

    def test_source_views_can_be_reused_without_mutating_the_cache(self):
        cache = self.root / "source_views"
        cache.mkdir()
        glb = self.root / "seed/scene.glb"
        loop.save(cache / "fingerprint.json", {"source_sha256":loop.sha(glb),"worker_sha256":"worker-sha","mode":"screen", "render":loop.augment.render_settings()})
        loop.save(cache / "cameras.json", {})
        for view in loop.augment.VIEWS:
            (cache / (view+".png")).write_bytes(b"remote view")
        target = self.root / "new_inspect"
        self.assertTrue(loop.reuse_source_inspect(cache,target,glb,"worker-sha"))
        (target / "fingerprint.json").write_text("changed destination")
        self.assertEqual(json.loads((cache / "fingerprint.json").read_text())["mode"],"screen")
        self.assertFalse(loop.reuse_source_inspect(cache,target,glb,"worker-sha"))

    def test_wrong_source_views_are_ignored_and_will_be_rendered_remotely(self):
        cache = self.root / "wrong_views"
        loop.save(cache / "fingerprint.json", {"source_sha256":"wrong","worker_sha256":"worker-sha"})
        target = self.root / "ignored_inspect"
        self.assertFalse(loop.reuse_source_inspect(cache,target,self.root/"seed/scene.glb","worker-sha"))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
