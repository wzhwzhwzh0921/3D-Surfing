"""Controller recovery tests with real offline discovery and fake stage artifacts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scene_factory.automation import Campaign, file_sha as actual_sha, rows, save, write_rows
from scene_factory.sources import DiscoveryError, discover_batch


class SceneAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "sources.jsonl"
        write_rows(self.catalog, [self.source(i) for i in range(1, 7)])
        self.calls = []
        # Changing stage implementations in another agent must not invalidate a
        # recovery test which is specifically about controller/source state.
        self.sha_patch = patch("scene_factory.automation.file_sha", return_value="stable-stage-code")
        self.sha_patch.start()
        self.network_patch = patch("scene_factory.sources._fetch", side_effect=AssertionError("offline test"))
        self.network_patch.start()

    def tearDown(self):
        self.network_patch.stop()
        self.sha_patch.stop()
        self.temp.cleanup()

    def source(self, i):
        return dict(uid=f"scene-{i}", name=f"Garden diorama {i}",
                    url=f"https://example.org/{i}.glb", category="DESIGNED_ROOM")

    def args(self, **overrides):
        values = dict(out=self.root/"campaign", provider="catalog", catalog=self.catalog,
                      queries=[], max_bytes=100_000_000, max_faces=300000, max_scan=1000,
                      max_pages=100, page_size=3, model="gpt-6.1-sol", url="https://example.org/v1/chat/completions",
                      max_repairs=2, key_file=None, workers=1, stage_timeout=30,
                      target=2, max_scenes=10, max_failed_batches=3, batch_size=2,
                      discovery_cache=self.root/"cache")
        values.update(overrides)
        return SimpleNamespace(**values)

    def runner(self, *, screen_status=None, qa_status=None, missing_delivery=None):
        screen_status, qa_status = screen_status or {}, qa_status or {}
        missing_delivery = set(missing_delivery or ())

        def run(stage, sources, output, exclude=None):
            inputs = rows(sources)
            self.calls.append((stage, [r["uid"] for r in inputs]))
            output.mkdir(parents=True, exist_ok=True)
            if stage == "screen":
                decisions = {name: [] for name in ("keep", "reject", "failed")}
                for row in inputs:
                    status = screen_status.get(row["uid"], "keep")
                    decisions[status].append({**row, "status": status,
                                              "screen_reason": "screen decision",
                                              "glb_sha256": hashlib.sha256(row["uid"].encode()).hexdigest()})
                for status, group in decisions.items():
                    write_rows(output/(status+".jsonl"), group)
                save(output/"summary.json", {status:len(group) for status,group in decisions.items()})
            else:
                row = inputs[0]
                status = qa_status.get(row["uid"], "keep")
                result = dict(uid=row["uid"], status=status, reason="independent mock review",
                              quality_source="independent_model", gold=False)
                if status == "keep":
                    folder = output/row["uid"] / "delivery"
                    folder.mkdir(parents=True, exist_ok=True)
                    for name in ("scene.glb", "start.png", "reveal.png", "audit.json"):
                        (folder/name).write_bytes(b"fixture")
                    save(folder/"question.json", dict(question="Which object is visible?", answer="Cup"))
                    if row["uid"] in missing_delivery:
                        (folder/"audit.json").unlink()
                    result["folder"] = str(folder)
                save(output/"summary.json", dict(results=[result]))
            return 0
        return run

    def test_only_final_valid_keep_counts_and_batches_continue(self):
        runner = self.runner(screen_status={"scene-1":"reject"}, qa_status={"scene-2":"failed"})
        summary = Campaign(self.args(), runner=runner).run()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["counts"], dict(keep=2, reject=1, failed=1, pending=0))
        self.assertEqual(summary["discovered"], 4)
        self.assertEqual([ids for stage,ids in self.calls if stage=="qa"], [["scene-2"],["scene-3"],["scene-4"]])

    def test_enough_indoor_questions_does_not_complete_outdoor_quota(self):
        base = self.runner()
        def runner(stage,sources,output,exclude=None):
            result=base(stage,sources,output,exclude)
            if stage=='screen':
                entries=rows(output/'keep.jsonl')
                for row in entries:
                    row['screen_assessment']={'environment':'indoor' if row['uid'] in ('scene-1','scene-2') else 'outdoor'}
                write_rows(output/'keep.jsonl',entries)
            return result
        result=Campaign(self.args(target=2,min_indoor=1,min_outdoor=1),runner=runner).run()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['counts']['keep'],3)
        self.assertEqual(result['selected_environments'],{'outdoor':1,'indoor':1})
        self.assertEqual({r['uid'] for r in rows(self.root/'campaign/selected.jsonl')},{'scene-1','scene-3'})

    def test_target_growth_processes_pending_qa_without_rescreen_or_rediscovery(self):
        runner = self.runner()
        first = Campaign(self.args(target=1), runner=runner).run()
        self.assertEqual(first["counts"], dict(keep=1, reject=0, failed=0, pending=1))
        def no_discovery(*args, **kwargs):
            raise AssertionError("Existing pending QA must be completed first")
        second = Campaign(self.args(target=2), runner=runner, discover=no_discovery).run()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(self.calls, [("screen",["scene-1","scene-2"]),
                                      ("qa",["scene-1"]),("qa",["scene-2"])])

    def test_selected_journal_reconstructs_input_after_interruption(self):
        runner = self.runner()
        def interrupt_screen(stage, sources, output, exclude=None):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            Campaign(self.args(target=1), runner=interrupt_screen).run()
        batch = self.root/"campaign/batches/batch_00001"
        journal = json.loads((batch/"batch.json").read_text())
        self.assertEqual(journal["phase"], "selected")
        (batch/"sources.jsonl").unlink()
        def no_discovery(*args, **kwargs):
            raise AssertionError("Selection journal is already durable")
        resumed = Campaign(self.args(target=1), runner=runner, discover=no_discovery).run()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(rows(batch/"sources.jsonl"), journal["sources"])

    def test_source_error_retains_cursor_and_resume_retries_same_window(self):
        args = self.args(target=1)
        def fail_discovery(*args, **kwargs):
            raise DiscoveryError("source outage")
        first = Campaign(args, runner=self.runner(), discover=fail_discovery).run()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["batches"], 0)
        self.assertEqual(first["discovered"], 0)
        error = json.loads((args.out/"discovery_error.json").read_text())
        self.assertIsNone(error["previous_cursor"])
        second = Campaign(args, runner=self.runner()).run()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(rows(args.out/"keep.jsonl")[0]["uid"], "scene-1")

    def test_source_end_and_scan_budget_report_distinct_terminal_states(self):
        write_rows(self.catalog, [self.source(1),self.source(2)])
        for max_scan, expected in ((1000,"source_exhausted"),(1,"discovery_budget_exhausted")):
            with self.subTest(max_scan=max_scan):
                args = self.args(out=self.root/f"campaign-{max_scan}", max_scan=max_scan, target=1)
                result = Campaign(args, runner=self.runner(screen_status={"scene-1":"reject","scene-2":"reject"})).run()
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["counts"]["keep"], 0)

    def test_duplicate_uids_and_decided_failures_are_not_randomly_retried(self):
        write_rows(self.catalog, [self.source(1),self.source(1),self.source(2),self.source(1),self.source(3)])
        args = self.args(target=1,batch_size=1)
        result = Campaign(args, runner=self.runner(screen_status={"scene-1":"failed","scene-2":"reject"})).run()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["discovered"], 3)
        self.assertEqual([ids for stage,ids in self.calls if stage=="screen"],
                         [["scene-1"],["scene-2"],["scene-3"]])

    def test_qa_keep_without_delivery_is_failed_and_target_requires_next_source(self):
        result = Campaign(self.args(target=1), runner=self.runner(missing_delivery={"scene-1"})).run()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"], dict(keep=1,reject=0,failed=1,pending=0))
        self.assertEqual(rows(self.root/"campaign/keep.jsonl")[0]["uid"], "scene-2")

    def test_source_configuration_change_rejected_before_processing(self):
        Campaign(self.args(target=1), runner=self.runner()).run()
        before = deepcopy(self.calls)
        with self.assertRaisesRegex(ValueError, "protocol changed"):
            Campaign(self.args(target=1,queries=["forest"]), runner=self.runner()).run()
        self.assertEqual(self.calls, before)

    def test_durable_old_keep_is_reconciled_if_qa_done_checkpoint_was_interrupted(self):
        args = self.args(target=1,batch_size=1)
        Campaign(args, runner=self.runner()).run()
        journal_path = args.out/"batches/batch_00001/batch.json"
        journal = json.loads(journal_path.read_text())
        journal.update(phase="screened", qa_done=[])
        save(journal_path,journal)
        calls_before = len(self.calls)
        regular = self.runner()
        def runner(stage, sources, output, exclude=None):
            if stage == "qa" and rows(sources)[0]["uid"] == "scene-1":
                raise RuntimeError("Old success must never be re-evaluated or downgraded")
            return regular(stage,sources,output,exclude)
        result = Campaign(self.args(target=2,batch_size=1), runner=runner).run()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["failed"], 0)
        self.assertEqual(rows(args.out/"keep.jsonl")[0]["uid"], "scene-1")
        self.assertEqual(self.calls[calls_before:], [("screen",["scene-2"]),("qa",["scene-2"])])

    def test_retry_failed_resumes_qa_without_reasking_quality_rejects(self):
        args = self.args(target=1, max_scenes=2)
        first = Campaign(args, runner=self.runner(
            screen_status={"scene-1":"reject"}, qa_status={"scene-2":"failed"})).run()
        self.assertEqual(first["status"], "scene_budget_exhausted")
        before = len(self.calls)
        resumed = Campaign(self.args(target=1, max_scenes=2, retry_failed=True),
                           runner=self.runner()).run()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["counts"], dict(keep=1,reject=1,failed=0,pending=0))
        self.assertEqual(self.calls[before:], [("qa",["scene-2"])])

    def test_all_failed_batches_stop_before_consuming_more_sources(self):
        runner = self.runner(screen_status={f"scene-{i}":"failed" for i in range(1,7)})
        result = Campaign(self.args(target=1, batch_size=1, max_failed_batches=3),
                          runner=runner).run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["discovered"], 3)
        self.assertEqual(result["counts"]["failed"], 3)
        self.assertEqual(len(self.calls), 3)

    def test_downstream_outage_does_not_overwrite_successful_original(self):
        class ConsumerOutage(Campaign):
            def after_qa(self, row):
                raise RuntimeError("downstream outage")
        args = self.args(target=2, batch_size=1)
        with self.assertRaisesRegex(RuntimeError, "downstream outage"):
            ConsumerOutage(args, runner=self.runner()).run()
        original = rows(args.out / "registry.jsonl")[0]
        self.assertEqual(original["status"], "keep")
        self.assertEqual(original["phase"], "qa")

    def test_changed_delivery_is_not_counted_or_silently_regenerated_on_resume(self):
        # Use real content hashes for both runs, rather than the stable stage
        # hashes used by the controller recovery fixtures.
        with patch("scene_factory.automation.file_sha", side_effect=actual_sha):
            args = self.args(target=1)
            Campaign(args, runner=self.runner()).run()
            delivery = Path(rows(args.out/"keep.jsonl")[0]["folder"])
            (delivery/"scene.glb").write_bytes(b"changed")
            before = len(self.calls)
            with self.assertRaisesRegex(ValueError, "Existing delivery missing"):
                Campaign(args, runner=self.runner()).run()
            result = json.loads((args.out/"summary.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["counts"]["keep"], 0)
            self.assertEqual(self.calls[before:], [])


if __name__ == "__main__":
    unittest.main()
