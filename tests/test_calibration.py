import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scene_factory.calibration import (evaluate_labels, make_label_template,
                                       _passes_needed_if_no_more_errors)
from scene_factory.audit_review import render_label_page
from scene_factory.supervisor import LEVELS, VIEWS, evidence_hashes, review_item_hashes


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "case_a"
        self.output = self.work / "output"
        results = []
        for level in LEVELS:
            folder = self.output / level
            folder.mkdir(parents=True)
            asset = f"GLB-{level}".encode()
            (folder / "scene.glb").write_bytes(asset)
            for view in VIEWS:
                (folder / f"{view}.png").write_bytes(f"{level}-{view}".encode())
            results.append({"level": level, "sha256": hashlib.sha256(asset).hexdigest(),
                            "question_type": "count", "question": f"How many {level}?",
                            "answer": {"value": 2}})
        self.validation = {"scene_id": "case_a", "status": "candidate", "results": results}
        (self.output / "validation.json").write_text(json.dumps(self.validation))
        report = {"schema_version": "sceneqa_supervisor_v2",
                  "artifact_sha256": {r["level"]: r["sha256"] for r in results},
                  "evidence_sha256": evidence_hashes(self.work),
                  "review_item_sha256": review_item_hashes(self.validation),
                  "tiers": {level: {"verdict": "revise" if level == "hard" else "pass"}
                            for level in LEVELS}}
        (self.work / "supervisor_round_0.json").write_text(json.dumps(report))
        self.labels = self.root / "labels.csv"
        make_label_template(self.root, self.labels)

    def tearDown(self):
        self.temp.cleanup()

    def _fill(self):
        with self.labels.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        for row in rows:
            row["human_verdict"] = "fail" if row["level"] == "simple" else "pass"
            row["human_answer_correct"] = "no" if row["level"] == "simple" else "yes"
        with self.labels.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_counts_false_accept_and_abstention_against_human_gold(self):
        self._fill()
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["gold_binary"], 3)
        self.assertEqual(report["supervisor_pass"], 2)
        self.assertEqual(report["supervisor_revise"], 1)
        self.assertEqual(report["false_accept"], 1)
        self.assertEqual(report["safe_accept"], 1)
        self.assertEqual(report["false_accept_rate_among_pass"], 0.5)
        self.assertGreater(report["false_accept_wilson95_upper"], 0.5)
        clustered = report["scene_cluster_audit"]
        self.assertEqual(clustered["scenes_with_supervisor_pass_and_gold"], 1)
        self.assertEqual(clustered["scenes_with_any_false_accept"], 1)
        self.assertFalse(clustered["pilot_target_met"])
        self.assertFalse(clustered["all_levels_target_met"])
        self.assertGreater(report["by_level"]["simple"]["audit_quota"]["minimum_scenes_if_no_more_errors"], 35)
        self.assertEqual(report["by_level"]["medium"]["audit_quota"]["additional_scenes_if_no_more_errors"], 34)
        self.assertEqual(report["by_level"]["hard"]["audit_quota"]["additional_scenes_if_no_more_errors"], 35)

    def test_pilot_target_uses_independent_scene_count(self):
        self.assertEqual(_passes_needed_if_no_more_errors(0, 0.10), 35)
        self.assertGreater(_passes_needed_if_no_more_errors(1, 0.10), 35)
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["scene_cluster_audit"]["additional_scenes_if_no_more_errors"], 35)

    def test_changed_image_or_asset_invalidates_human_label(self):
        self._fill()
        (self.output / "simple" / "answer_view.png").write_bytes(b"changed")
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["stale"], 1)
        self.assertEqual(report["gold_binary"], 0)  # Old supervisor report also became stale.
        self.assertEqual(report["missing_supervisor"], 2)

    def test_unfilled_template_is_not_counted_as_accuracy(self):
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["unlabeled"], 3)
        self.assertIsNone(report["false_accept_rate_among_pass"])
        with self.assertRaises(FileExistsError):
            make_label_template(self.root, self.labels)

    def test_browser_export_bom_is_accepted(self):
        self._fill()
        self.labels.write_bytes(b"\xef\xbb\xbf" + self.labels.read_bytes())
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["gold_binary"], 3)
        page = self.root / "review.html"
        self.assertEqual(render_label_page(self.labels, self.root, page)["questions"], 3)

    def test_legacy_answer_aware_supervisor_is_separate(self):
        self._fill()
        path = self.work / "supervisor_round_0.json"
        old = json.loads(path.read_text())
        old["schema_version"] = "sceneqa_supervisor_v1"
        path.write_text(json.dumps(old))
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["legacy_supervisor"], 3)
        self.assertEqual(report["gold_binary"], 0)

    def test_start_view_shortcut_counts_as_bad_question(self):
        self._fill()
        with self.labels.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        simple = next(row for row in rows if row["level"] == "simple")
        simple["human_verdict"] = "review"
        simple["human_answer_correct"] = "unknown"
        simple["human_start_leak"] = "yes"
        with self.labels.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["false_accept"], 1)
        self.assertEqual(report["start_leak_yes"], 1)

    def test_conflicting_human_start_and_pass_is_excluded(self):
        self._fill()
        with self.labels.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        simple = next(row for row in rows if row["level"] == "simple")
        simple["human_verdict"] = "pass"
        simple["human_answer_correct"] = "yes"
        simple["human_start_leak"] = "yes"
        with self.labels.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["human_inconsistent"], 1)
        self.assertEqual(report["start_leak_yes"], 1)
        self.assertEqual(report["gold_binary"], 2)

    def test_label_path_cannot_escape_runs_root(self):
        with self.labels.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        rows[0]["work_rel"] = "../other-work"
        with self.labels.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "escapes runs root"):
            evaluate_labels(self.labels, self.root)
        with self.assertRaisesRegex(ValueError, "escapes runs root"):
            render_label_page(self.labels, self.root, self.root / "review.html")

    def test_visual_review_page_shows_assets_and_escapes_questions(self):
        with self.labels.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        rows[0]["scene_id"] = "<img onerror=alert(1)>"
        with self.labels.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        page = self.root / "review.html"
        result = render_label_page(self.labels, self.root, page)
        markup = page.read_text()
        self.assertEqual(result["questions"], 3)
        self.assertIn('src="case_a/output/simple/scene.glb"', markup)
        self.assertIn('src="case_a/output/simple/start.png"', markup)
        self.assertNotIn("<img onerror=alert(1)>", markup)
        self.assertIn("human_answer", markup)
        self.assertIn("human_start_leak", markup)
        self.assertIn('<details class="explore">', markup)
        self.assertIn("导出标注 CSV", markup)

    def test_visual_review_rejects_stale_asset(self):
        (self.output / "simple" / "scene.glb").write_bytes(b"repaired")
        with self.assertRaisesRegex(ValueError, "Label row is stale"):
            render_label_page(self.labels, self.root, self.root / "review.html")


if __name__ == "__main__":
    unittest.main()
