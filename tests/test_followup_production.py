"""Offline tests of quotas, real delivery contracts, outage recovery and refill."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scene_factory.automation import save, write_rows
from scene_factory.followup import digest, task_contract
from scene_factory.followup_production import Production, quotas, verify_delivery


IDENTITY = {"account_fingerprint": "test-account"}


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"LUNA_CHAT_URL": "http://localhost/test-account",
            "SCENEQA_CODEX_REASONING": "xhigh"})
        self.environment.start()
        self.login = patch("scene_factory.followup_production.login_status", return_value=IDENTITY)
        self.login.start()
        self.seeds = []
        for i, environment in enumerate(("outdoor", "indoor", "outdoor", "indoor")):
            self.seeds.append(self.seed(str(i), environment))
        self.campaign = self.root / "seeds"
        write_rows(self.campaign / "selected.jsonl", self.seeds)
        self.calls = []
        self.renderer = SimpleNamespace(close=lambda: None)

    def tearDown(self):
        self.login.stop()
        self.environment.stop()
        self.temp.cleanup()

    def seed(self, uid, environment):
        folder = self.root / ("original-" + uid)
        folder.mkdir()
        source = folder / "scene.glb"
        source.write_bytes(("scene-" + uid).encode())
        (folder / "start.png").write_bytes(b"existing-k8s-image-fixture")
        return {"uid": uid, "name": "Scene " + uid, "source_glb": str(source),
                "folder": str(folder), "screen_assessment": {"environment": environment}}

    def args(self, **changes):
        fields = dict(out=self.root / "batch", target=8, seed_campaign=[self.campaign],
            model="gpt-6.1-sol", max_repairs=1, inspection_rounds=2, case_retries=0,
            max_bytes=64 * 1048576, exclude_history=[], min_free_gib=0, max_scenes=20)
        fields.update(changes)
        return SimpleNamespace(**fields)

    def case(self, row, mode, out, args, renderer, model):
        self.calls.append((row["uid"], mode))
        out.mkdir(parents=True, exist_ok=True)
        original = {"question": "箱子里是什么？", "initial_camera": {"position": [1, 2, 3]}}
        plan = {"action": "propose", "mode": mode, "followup": "也请回答附加要求。",
                "requirements": [], "hops": []}
        for key in (["original", "extra_1", "extra_2"] if mode == "bundle" else ["original", "final"]):
            plan["requirements"].append({"id": key, "question": original["question"] if key == "original" else "追加问题",
                "target": key, "type": "identity" if key == "original" else "color",
                "blocked_by": "墙", "views": ["reveal"]})
        if mode == "chain":
            for key, depends in (("h1", "original"), ("h2", "h1")):
                plan["hops"].append({"id": key, "depends_on": depends, "instruction": "按空间关系选择",
                    "candidates": ["物件A", "物件B"], "views": ["reveal"]})
            plan["requirements"][-1]["depends_on"] = "h2"
        answer = {"answers": [{"id": r["id"], "answer": "模型视觉答案", "clear": True,
                    "evidence_views": ["reveal"]} for r in plan["requirements"]],
                  "hop_answers": [{"id": h["id"], "selected": "选中的物件", "evidence_views": ["reveal"]} for h in plan["hops"]]}
        review = {"pass": True, "followup_natural": True, "original_preserved": True,
            "additional_work": True, "cross_view_required": True, "reason": "模型看图通过",
            "checks": [{"id": r["id"], "pass": True, "start_hidden": True,
                "observed_answer": "模型视觉答案", "reason": "确有遮挡"} for r in plan["requirements"]],
            "hop_checks": [{"id": h["id"], "pass": True, "linked_to_previous": True,
                "ablation_keeps_ambiguous": True, "candidate_count": 2,
                "reason": "必须根据上一参照选择"} for h in plan["hops"]]}
        conversation = [{"role": "user", "content": original["question"]},
                        {"role": "user", "content": plan["followup"]}]
        delivery = out / "delivery"
        delivery.mkdir(exist_ok=True)
        for name in ("scene.glb", "start.png"):
            (delivery / name).write_bytes((Path(row["folder"]) / name).read_bytes())
        source_hash = digest(delivery / "scene.glb")
        save(delivery / "task.json", task_contract(plan, original, conversation, source_hash))
        save(delivery / "answers.json", answer)
        save(delivery / "audit.json", review)
        record = {"uid": row["uid"], "mode": mode, "status": "keep", "reason": "通过",
            "scene_sha256": source_hash, "delivery": str(delivery), "original": original,
            "plan": plan, "answers": answer, "audit": review, "conversation": conversation,
            "evidence": [{"label": "reveal", "path": str(delivery / "start.png")}]}
        save(out / "record.json", record)
        return record

    def production(self, args=None, **overrides):
        return Production(args or self.args(), identity=IDENTITY, model=SimpleNamespace(),
            renderer=self.renderer, case_runner=overrides.pop("case_runner", self.case), **overrides)

    def test_exact_target_balanced_and_resume_does_not_call_model(self):
        result = self.production().run()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["kept"], 8)
        self.assertEqual(result["unique_scenes"], 4)
        self.assertEqual(result["cells"], {c: 2 for c in quotas(8)})
        before = self.calls[:]
        self.assertEqual(self.production().run()["status"], "complete")
        self.assertEqual(self.calls, before)
        self.assertEqual(quotas(200), {c: 50 for c in quotas(8)})

    def test_chain_solver_contract_requires_hop_answers_but_hides_candidates(self):
        self.production().run()
        task = json.loads((self.root / "batch/tasks/0/chain/delivery/task.json").read_text())
        self.assertEqual(task["required_answer_ids"], ["original", "h1", "h2", "final"])
        self.assertNotIn("candidates", json.dumps(task))
        self.assertNotIn("answer", task)

    def test_downstream_failure_is_resumable_without_turning_into_quality_reject(self):
        def fail(*args):
            raise TimeoutError("account/network temporarily unavailable")
        first = self.production(case_runner=fail).run()
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["counts"], {"failed": 1})
        self.assertEqual(first["kept"], 0)
        self.assertEqual(self.production().run()["kept"], 8)

    def test_modified_keep_is_not_silently_counted_on_resume(self):
        self.production().run()
        (self.root / "batch/tasks/0/bundle/delivery/answers.json").write_text("{}")
        result = self.production().run()
        self.assertIn(result["status"], ("waiting", "failed"))
        self.assertNotEqual(result["status"], "complete")

    def test_missing_originals_trigger_stream_refill(self):
        write_rows(self.campaign / "selected.jsonl", self.seeds[:2])
        all_seeds = self.seeds
        class FakeStream:
            def __init__(self, args, owner):
                self.owner = owner
                self.registry = {}
                self.batches = []
            def run(self):
                for row in all_seeds[2:]:
                    self.owner.consume(row)
                return {"status": "complete"}
            def cleanup(self):
                pass
        result = self.production(stream_factory=FakeStream).run()
        self.assertEqual(result["kept"], 8)
        self.assertEqual(result["status"], "complete")

    def test_source_selection_and_keywords_reach_the_original_stream(self):
        received = []
        def stream_factory(args, owner):
            received.append(args)
            return object()
        production = self.production(args=self.args(provider="scansceneverse-search",
            queries=["courtyard", "grocery"]), stream_factory=stream_factory)
        production.make_stream()
        self.assertEqual(received[0].provider, "scansceneverse-search")
        self.assertEqual(received[0].queries, ["courtyard", "grocery"])
        with self.assertRaisesRegex(ValueError, "protocol"):
            self.production(args=self.args(provider="prosceneverse-search"))

    def test_source_exhaustion_is_not_success(self):
        write_rows(self.campaign / "selected.jsonl", [])
        class EmptyStream:
            def __init__(self, args, owner):self.registry = {};self.batches = []
            def run(self):return {"status": "source_exhausted", "reason": "No more sources"}
            def cleanup(self):pass
        result = self.production(stream_factory=EmptyStream).run()
        self.assertEqual(result["status"], "source_exhausted")
        self.assertEqual(result["kept"], 0)

    def test_no_duplicate_scene_hash_under_different_uids(self):
        p = self.production()
        p.consume(self.seeds[0])
        other = self.seed("duplicate", "outdoor")
        Path(other["source_glb"]).write_bytes(Path(self.seeds[0]["source_glb"]).read_bytes())
        p.consume(other)
        self.assertEqual(len(p.jobs), 2)

    def test_review_failure_cannot_be_promoted_to_keep_by_file_checks(self):
        p = self.production()
        p.consume(self.seeds[0])
        record = json.loads((self.root / "batch/tasks/0/chain/record.json").read_text())
        record["audit"]["hop_checks"][0]["ablation_keeps_ambiguous"] = False
        with self.assertRaisesRegex(ValueError, "review gate"):
            verify_delivery(record)


if __name__ == "__main__":
    unittest.main()
