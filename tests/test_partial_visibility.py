"""Regression for questions whose answer surface, not whole object, is hidden."""
import json
from pathlib import Path
import tempfile
import unittest

from scene_factory.validate import validate


def blocker():
    return {"samples": 48, "blocked_samples": 16, "blocked_fraction": 1 / 3,
            "first_hit_objects": {"NativeBlocker": 16, "Other": 4},
            "mean_blocker_gap": 0.3, "max_allowed_gap": 0.8,
            "blocker_width_ratio": 0.2,
            "expected_blocker_objects": ["NativeBlocker"]}


class PartialVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture = Path(__file__).resolve().parents[1] / "examples/model_answer_fixture_plan.json"
        self.plan = json.loads(fixture.read_text())
        self.plan["workflow_version"] = "candidate_probe_v1"
        self.plan["variants"][0]["occlusion_targets"] = [
            {"target_copy_id": "bottle_0", "blocker_objects": ["Box"]}]
        self.plan_path = self.root / "plan.json"
        self.output = self.root / "output"
        builds = []
        for index, variant in enumerate(self.plan["variants"]):
            level = variant["level"]
            folder = self.output / level
            folder.mkdir(parents=True)
            asset = b"glTF" + (2).to_bytes(4, "little") + (16).to_bytes(4, "little") + index.to_bytes(4, "little")
            (folder / "scene.glb").write_bytes(asset)
            for name in ("start", "reveal", "detail", "answer_view"):
                (folder / f"{name}.png").write_bytes(b"fixture")
            copy_id = variant["copies"][0]["id"]
            builds.append({"level": level, "copy_mesh_names": {copy_id: [copy_id]},
                           "original_mesh_count": 3, "export_mesh_count": 4,
                           "start_visible_copies": {copy_id: True},
                           "view_probes": [{"azimuth_deg": 180, "elevation_deg": 35,
                                            "visible_copy_ids": [copy_id]}],
                           "local_view_probes": [], "copy_collisions": [],
                           "occlusion_evidence": {copy_id: blocker()},
                           "anchor_query_evidence": ({"resolved_anchor_object": "Cup",
                                                     "margin_ratio": 0.3} if level == "hard" else None),
                           "evidence_views": {"answer_view_coverage": {"all_visible": True}},
                           "start_camera": variant["start_camera"]})
        self.builds = builds
        self._write()

    def tearDown(self):
        self.temp.cleanup()

    def _write(self):
        self.plan_path.write_text(json.dumps(self.plan))
        (self.output / "build_report.json").write_text(json.dumps(self.builds))

    def test_partial_answer_surface_can_pass_all_three_tiers(self):
        report = validate(self.plan_path, self.output)
        self.assertEqual(report["status"], "candidate")
        for row in report["results"]:
            self.assertEqual(row["hidden_copy_ids_at_start"], [])
            self.assertEqual(row["obscuration_mode"], "local_partial_blocker")
            self.assertTrue(row["visibility_gate"])

    def test_unrelated_hidden_copy_cannot_make_answer_obscured(self):
        simple = self.plan["variants"][0]
        simple["copies"].append({"id": "irrelevant", "from": "bottle", "xy": [2, 2],
                                 "support_object": "Table"})
        build = self.builds[0]
        build["copy_mesh_names"]["irrelevant"] = ["irrelevant"]
        build["export_mesh_count"] = 5
        build["start_visible_copies"]["irrelevant"] = False
        build["view_probes"][0]["visible_copy_ids"].append("irrelevant")
        build["occlusion_evidence"] = {}
        self._write()
        report = validate(self.plan_path, self.output, strict=False)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["results"][0]["obscuration_mode"], "none")

    def test_start_image_is_required_for_visual_review(self):
        (self.output / "simple/start.png").unlink()
        report = validate(self.plan_path, self.output, strict=False)
        self.assertFalse(report["results"][0]["evidence_present"])
        self.assertEqual(report["status"], "failed")


if __name__ == "__main__":
    unittest.main()
