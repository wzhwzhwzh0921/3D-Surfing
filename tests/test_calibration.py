import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scene_factory.calibration import evaluate_labels, make_label_template
from scene_factory.supervisor import LEVELS, VIEWS, evidence_hashes


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

    def test_legacy_answer_aware_supervisor_is_separate(self):
        self._fill()
        path = self.work / "supervisor_round_0.json"
        old = json.loads(path.read_text())
        old["schema_version"] = "sceneqa_supervisor_v1"
        path.write_text(json.dumps(old))
        report = evaluate_labels(self.labels, self.root)
        self.assertEqual(report["legacy_supervisor"], 3)
        self.assertEqual(report["gold_binary"], 0)

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


if __name__ == "__main__":
    unittest.main()
