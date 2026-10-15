"""Sequential, durable production of reviewed user-followup tasks using Codex."""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from .automation import Campaign, REPO, STAGE_FILES, rows, save, write_rows
from .sources import DEFAULT_PROVIDER, PROVIDERS, source_config
from .model_backend import configured_model, model_identity as login_status
from .rendering import render_settings
from .followup import digest, evidence, review_accepted, run_case, validate_answers, validate_plan
from .screening.inspection import InspectionRenderer

EXTRA_FILES = ["scene_factory/followup_production.py", "scene_factory/followup.py",
               "scene_factory/prompts/followup_plan_zh.txt",
               "scene_factory/prompts/followup_answer_zh.txt",
               "scene_factory/prompts/followup_audit_zh.txt"]
MODES = ("bundle", "chain")
ENVIRONMENTS = ("outdoor", "indoor")


class SafetyStop(RuntimeError):
    pass


def quotas(target):
    """Four balanced cells: (bundle/chain) × (outdoor/indoor)."""
    cells = [(m, e) for m in MODES for e in ENVIRONMENTS]
    return {m + ":" + e: target // 4 + (i < target % 4) for i, (m, e) in enumerate(cells)}


def native_seed(row, source_hash):
    """Accept old native re-exports only with the original, hash-bound empty plan."""
    delivered = Path(row["folder"]) / "scene.glb"
    if digest(delivered) == source_hash:
        return True
    try:
        result = json.loads(Path(row["result_path"]).read_text())
        item, = [r for r in result["items"] if r["level"] == "medium"]
        if item.get("scene_mode") != "native":
            return False
        glb = Path(item["glb"])
        if not glb.is_absolute():
            glb = Path(row["result_path"]).parent / glb
        marker = json.loads((glb.parent.parent / "fingerprint.json").read_text())
        plan_file = glb.parent.parent.parent / "plan.json"
        plan = json.loads(plan_file.read_text())
        medium, = [r for r in plan["items"] if r["level"] == "medium"]
        original = json.loads((Path(row["folder"]) / "question.json").read_text())
        return (marker.get("source_sha256") == source_hash
                and marker.get("plan_sha256") == digest(plan_file)
                and medium.get("copies") == []
                and medium.get("question") == item["question"] == original["question"]
                and item.get("initial_camera") == original["initial_camera"]
                and digest(glb) == digest(delivered))
    except (ValueError, OSError, KeyError, TypeError):
        return False


def verify_delivery(record):
    """Check files and the model's complete verdict; do not judge scene facts."""
    delivery = Path(record["delivery"])
    names = ("scene.glb", "start.png", "task.json", "answers.json", "audit.json")
    if not all((delivery / n).is_file() for n in names):
        raise ValueError("A kept delivery has missing files")
    checksums = {n: digest(delivery / n) for n in names}
    if checksums["scene.glb"] != record["scene_sha256"]:
        raise ValueError("Delivery scene hash changed")
    task = json.loads((delivery / "task.json").read_text())
    plan, answers, judgment = record["plan"], record["answers"], record["audit"]
    known = {v["label"]: v for v in record["evidence"]}
    validate_plan(plan, record["original"]["question"], record["mode"], known)
    validate_answers(answers, plan, known)
    if not review_accepted(judgment, plan, answers):
        raise ValueError("Kept task does not pass every model review gate")
    expected = (["original", "extra_1", "extra_2"] if record["mode"] == "bundle"
                else ["original", "h1", "h2", "final"])
    if (task.get("required_answer_ids") != expected
            or task.get("original_question") != record["original"]["question"]
            or task.get("initial_camera") != record["original"]["initial_camera"]
            or task.get("messages") != record["conversation"]
            or task.get("grading") != "all_requested_answers_and_selections_correct"):
        raise ValueError("Delivery changed the original or omitted required selections")
    for filename, key in (("answers.json", "answers"), ("audit.json", "audit")):
        actual = json.loads((delivery / filename).read_text())
        if any(actual.get(k) != v for k, v in record[key].items()):
            raise ValueError("Delivered answers/audit differ from the reviewed record")
    for v in known.values():
        if not Path(v["path"]).is_file() or (v.get("image_sha256") and digest(v["path"]) != v["image_sha256"]):
            raise ValueError("A kept evidence image is missing or changed")
    return checksums


class OriginalStream(Campaign):
    """Reuse online discovery/screening/QA, consuming each original immediately."""
    def __init__(self, args, owner):
        self.owner = owner
        super().__init__(args)

    def target_met(self):
        self.owner.check_safety()
        return self.owner.target_met()

    def invoke(self, *args, **kwargs):
        self.owner.check_safety()
        self.owner.report("running", phase="original_" + args[0])
        return super().invoke(*args, **kwargs)

    def after_qa(self, row):
        if row["status"] == "keep":
            self.owner.consume(row)


class Production:
    def __init__(self, args, *, case_runner=run_case, identity=None, model=None,
                 renderer=None, stream_factory=OriginalStream):
        self.args, self.case_runner = args, case_runner
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.identity = identity or login_status()
        self.model = model
        self.renderer = renderer
        self.stream_factory = stream_factory
        self.quotas = quotas(args.target)
        self.seeds = []
        seed_inputs = []
        for source in args.seed_campaign:
            manifest = Path(source).resolve() / "selected.jsonl"
            seed_inputs.append({"path": str(manifest), "sha256": digest(manifest)})
            for row in rows(manifest):
                if row["uid"] not in {r["uid"] for r in self.seeds}:
                    self.seeds.append(row)
        protocol = {"schema": 1, "target": args.target, "quotas": self.quotas,
            "account_fingerprint": self.identity["account_fingerprint"],
            "model": args.model, "reasoning": os.getenv("SCENEQA_CODEX_REASONING", "xhigh"),
            "endpoint": os.getenv("LUNA_CHAT_URL"), "seed_inputs": seed_inputs,
            "model_backend": os.getenv("SCENEQA_MODEL_BACKEND", "codex"), "render": render_settings(),
            "max_repairs": args.max_repairs, "inspection_rounds": args.inspection_rounds,
            "native_only": True, "max_bytes": args.max_bytes,
            "source": source_config(provider=getattr(args, "provider", DEFAULT_PROVIDER),
                queries=getattr(args, "queries", []), max_bytes=args.max_bytes,
                max_scan=1_000_000, max_pages=100_000, page_size=60),
            "files": {f: digest(REPO / f) for f in sorted(set(STAGE_FILES + EXTRA_FILES))},
            "historical_exclusion_sha256": [digest(p) for p in args.exclude_history]}
        self.fingerprint = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
        marker = self.out / "protocol.json"
        if marker.exists() and json.loads(marker.read_text()) != protocol:
            raise ValueError("Batch protocol or account changed; use a new output directory")
        save(marker, protocol)
        self.state_path = self.out / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "created_at": time.time(), "fingerprint": self.fingerprint}
        self.jobs = {r["task_id"]: r for r in rows(self.out / "registry.jsonl")} if (self.out / "registry.jsonl").exists() else {}
        self.stream = None
        self.source_summary = {}

    def counts(self):
        return Counter(j["cell"] for j in self.jobs.values() if j["status"] == "keep")

    def target_met(self):
        counts = self.counts()
        return all(counts[k] >= n for k, n in self.quotas.items())

    def check_safety(self):
        if (self.out / "STOP").exists():
            raise SafetyStop("STOP文件存在；已保留进度，移除后可续跑")
        available = shutil.disk_usage(self.out).free / 2**30
        if available < self.args.min_free_gib:
            raise SafetyStop(f"磁盘可用空间{available:.1f}GiB，低于保留{self.args.min_free_gib}GiB；没有删除资产")
        if login_status()["account_fingerprint"] != self.identity["account_fingerprint"]:
            raise SafetyStop("当前Codex账号已改变，停止避免使用其他账号额度")

    def report(self, status, reason=None, **extra):
        if self.stream is not None:
            self.source_summary = {"counts": dict(Counter(r["status"] for r in self.stream.registry.values())),
                                   "discovered": len(self.stream.registry), "batches": len(self.stream.batches)}
        entries = list(self.jobs.values())
        write_rows(self.out / "registry.jsonl", entries)
        for verdict in ("keep", "reject", "failed"):
            write_rows(self.out / (verdict + ".jsonl"), [r for r in entries if r["status"] == verdict])
        kept = [r for r in entries if r["status"] == "keep"]
        write_rows(self.out / "delivery-manifest.jsonl", kept)
        summary = {"status": status, "reason": reason, "target": self.args.target,
            "kept": len(kept), "counts": dict(Counter(r["status"] for r in entries)),
            "cells": dict(self.counts()), "quotas": self.quotas,
            "unique_scenes": len({r["uid"] for r in kept}),
            "model": self.args.model, "reasoning": os.getenv("SCENEQA_CODEX_REASONING", "xhigh"),
            "model_backend": os.getenv("SCENEQA_MODEL_BACKEND", "codex"),
            "auth_method": self.identity.get("auth_method", "configured"), "account_fingerprint": self.identity["account_fingerprint"],
            "render_location": render_settings()["backend"], "gold": False, "blind_solver_tested": False,
            "upstream": self.source_summary,
            "elapsed_seconds": round(time.time() - self.state["created_at"], 1),
            "updated_at": time.time(), **extra}
        self.state.update(summary)
        save(self.state_path, self.state)
        save(self.out / "summary.json", summary)
        self.write_gallery(kept, summary)
        return summary

    def write_gallery(self, kept, summary):
        # A bounded, image-only sample keeps the dashboard small at 1,000 tasks.
        selected = []
        for cell in self.quotas:
            selected.extend([r for r in kept if r["cell"] == cell][:5])
        body = ["<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
            "<title>批量增难进度</title><style>body{font:16px/1.8 system-ui;max-width:1100px;margin:30px auto;padding:20px;background:#eef2f7}article{background:white;margin:20px 0;padding:20px}img{width:49%;vertical-align:top}pre{white-space:pre-wrap}a{overflow-wrap:anywhere}</style>",
            "<h1>基于原题，追加用户需求</h1><p>在线素材 → 模型筛选 → 原题 → 用户追加需求 → 作答 → 独立上下文审核。任意要求错误，整题不通过。图片全部由K8s生成；答案为模型标注，尚未盲测探索步数。</p>",
            "<pre>" + html.escape(json.dumps(summary, ensure_ascii=False, indent=2)) + "</pre>"]
        for job in selected:
            task = json.loads((Path(job["delivery"]) / "task.json").read_text())
            body.append("<article><h2>" + html.escape(job["name"] + " · " + job["cell"]) + "</h2>")
            image = Path(job["delivery"]) / "start.png"
            body.append('<img loading="lazy" src="' + html.escape(os.path.relpath(image, self.out), quote=True) + '">')
            body.append("<p><b>用户原题：</b>" + html.escape(task["original_question"]) + "</p><p><b>用户追加需求：</b>" + html.escape(task["user_followup"]) + "</p>")
            body.append('<a href="' + html.escape(os.path.relpath(Path(job["delivery"]) / "answers.json", self.out), quote=True) + '">评估侧答案</a></article>')
        (self.out / "gallery.html").write_text("\n".join(body) + "</html>")

    def consume(self, row):
        environment = Campaign.environment(row)
        if environment not in ENVIRONMENTS:
            # Mixed scenes stay in upstream results; they cannot satisfy these quotas.
            return
        self.check_safety()
        # Followups use byte-identical native scenes, never a mismatched enhancement.
        scene_hash = digest(row["source_glb"])
        if not native_seed(row, scene_hash):
            save(self.out / "incompatible-seeds" / (row["uid"] + ".json"), {
                "uid": row["uid"], "reason": "原题场景不是原始GLB；不能混用原景证据"})
            return
        if any(j.get("scene_sha256") == scene_hash and j["uid"] != row["uid"]
               for j in self.jobs.values()):
            save(self.out / "incompatible-seeds" / (row["uid"] + ".json"), {
                "uid": row["uid"], "reason": "不同UID对应相同GLB，不重复出题"})
            return
        for mode in MODES:
            cell = mode + ":" + environment
            if self.counts()[cell] >= self.quotas[cell]:
                continue
            key = row["uid"] + "__" + mode
            old = self.jobs.get(key, {})
            if old.get("status") in ("keep", "reject"):
                continue
            job = {"task_id": key, "uid": row["uid"], "name": row["name"], "cell": cell,
                   "mode": mode, "environment": environment, "status": "running",
                   "scene_sha256": scene_hash,
                   "failures": old.get("failures", []), "source_url": row.get("source_url"),
                   "license_slug": row.get("license_slug"), "author": row.get("author_display_name")}
            self.jobs[key] = job
            self.report("running", phase="followup", current_task=key)
            print(json.dumps({"event": "followup_started", "task_id": key, "kept": sum(self.counts().values())}, ensure_ascii=False), flush=True)
            directory = self.out / "tasks" / row["uid"] / mode
            # Retries reuse request-bound model caches and verified K8s observations.
            for attempt in range(self.args.case_retries + 1):
                self.check_safety()
                try:
                    record = self.case_runner(row, mode, directory, self.args, self.renderer, self.model)
                    job.update(status=record["status"], reason=record.get("reason"), record=str(directory / "record.json"))
                    if job["status"] == "keep":
                        hashes = verify_delivery(record)
                        job.update(delivery=record["delivery"], sha256=hashes,
                            scene_sha256=record["scene_sha256"], gold=False,
                            answer_source="model_visual", review_source="independent_context",
                            required_answer_ids=json.loads((Path(record["delivery"]) / "task.json").read_text())["required_answer_ids"])
                    break
                except SafetyStop:
                    raise
                except Exception as exc:
                    job.update(status="failed", reason=type(exc).__name__ + ": " + str(exc))
                    job["failures"].append({"time": time.time(), "reason": job["reason"]})
                    self.report("running", phase="followup_retry", current_task=key)
                    if attempt < self.args.case_retries:
                        self.wait(min(60, 10 * (attempt + 1)))
            self.report("running", phase="followup", current_task=key)
            print(json.dumps({"event": "followup_finished", "task_id": key,
                "status": job["status"], "kept": sum(self.counts().values())}, ensure_ascii=False), flush=True)
            if job["status"] == "failed":
                # An outage must not consume an unlimited stream of new assets.
                raise SafetyStop("当前增难任务重试后仍处理失败，保留缓存；后台将等待后续跑：" + job["reason"])

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if (self.out / "STOP").exists():
                raise SafetyStop("STOP文件存在")
            time.sleep(min(30, max(0, until - time.monotonic())))

    def make_stream(self):
        excluded = self.out / "seed-exclusions.jsonl"
        write_rows(excluded, [{"uid": r["uid"]} for r in self.seeds])
        a = SimpleNamespace(out=self.out / "originals",
            provider=getattr(self.args, "provider", DEFAULT_PROVIDER), catalog=None,
            queries=getattr(self.args, "queries", []), max_bytes=self.args.max_bytes, max_scan=1_000_000,
            max_pages=100_000, page_size=60, model=self.args.model,
            url=os.environ["LUNA_CHAT_URL"], max_repairs=2, key_file=None,
            workers=1, single_worker=True, stage_timeout=7200,
            target=self.args.max_scenes, max_scenes=self.args.max_scenes,
            max_failed_batches=3, batch_size=1, discovery_cache=None,
            min_indoor=0, min_outdoor=0, screen_version="v5", retry_failed=True,
            exclude_history=[*self.args.exclude_history, excluded])
        return self.stream_factory(a, self)

    def run(self):
        if self.model is None:
            spec = importlib.util.spec_from_file_location("sceneqa_batch_model", REPO / "experiments/sol_augment/run.py")
            self.model = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.model)
        if self.renderer is None:
            self.renderer = InspectionRenderer(self.out)
        os.environ["SCENEQA_NATIVE_ONLY"] = "1"
        try:
            # Revalidate accepted files once at resume, not once per seed (no O(n²) GLB reads).
            for job in self.jobs.values():
                if job["status"] == "keep":
                    try:
                        record = json.loads(Path(job["record"]).read_text())
                        if verify_delivery(record) != job["sha256"]:
                            raise ValueError("Delivery checksums changed")
                    except (ValueError, OSError, KeyError) as exc:
                        job.update(status="failed", reason="交付完整性错误：" + str(exc))
                        raise SafetyStop("已保留交付文件缺失或被修改；已取消计数，保留原件等待处理") from exc
            for row in self.seeds:
                if self.target_met():
                    break
                self.consume(row)
            if self.target_met():
                return self.report("complete", "达到审核通过的目标数量与各项配额")
            self.stream = self.make_stream()
            # Registry commits may precede a crash in the callback; replay those originals first.
            for row in list(self.stream.registry.values()):
                if row["status"] == "keep" and not self.target_met():
                    self.consume(row)
            result = self.stream.run()
            self.source_summary = {k: result.get(k) for k in ("status", "reason", "counts", "discovered", "batches")}
            if self.target_met():
                return self.report("complete", "达到审核通过的目标数量与各项配额")
            return self.report(result["status"], result.get("reason"))
        except SafetyStop as exc:
            return self.report("waiting", str(exc))
        except KeyboardInterrupt:
            self.report("interrupted", "已保存进度，可用同一命令续跑")
            raise
        except Exception as exc:
            return self.report("failed", type(exc).__name__ + ": " + str(exc))
        finally:
            if self.stream:
                self.stream.cleanup()
            self.renderer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--seed-campaign", type=Path, action="append", default=[])
    parser.add_argument("--exclude-history", type=Path, action="append", default=[])
    parser.add_argument("--max-scenes", type=int, default=10000)
    parser.add_argument("--max-bytes", type=int, default=64 * 1048576)
    parser.add_argument("--provider", choices=[p for p in PROVIDERS if p != "catalog"],
                        default=os.getenv("SCENEQA_SOURCE_PROVIDER", DEFAULT_PROVIDER))
    parser.add_argument("--query", dest="queries", action="append", default=[],
                        help="Optional metadata keywords (OR); applies to each source")
    parser.add_argument("--max-repairs", type=int, default=1)
    parser.add_argument("--inspection-rounds", type=int, default=2)
    parser.add_argument("--case-retries", type=int, default=2)
    parser.add_argument("--min-free-gib", type=float, default=25)
    parser.add_argument("--model", default=configured_model())
    parser.add_argument("--watch", action="store_true", help="Retry operational failures after a cooldown, without changing quality gates")
    parser.add_argument("--cooldown", type=int, default=300)
    args = parser.parse_args(argv)
    if min(args.target, args.max_scenes, args.max_bytes, args.cooldown) < 1 or min(args.max_repairs, args.inspection_rounds, args.case_retries, args.min_free_gib) < 0:
        parser.error("Targets/budgets must be positive; retry/inspection/space budgets nonnegative")
    if os.getenv("SCENEQA_MODEL_BACKEND", "codex") != "relay" and not os.getenv("SCENEQA_CODEX_ACTIVE"):
        parser.error("Run this batch through sceneqa workflow followup-batch for its model gateway")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("This batch output directory is already running")
        while True:
            production = Production(args)
            result = production.run()
            print(json.dumps({k: v for k, v in result.items() if k != "upstream"}, ensure_ascii=False), flush=True)
            if result["status"] == "complete":
                return 0
            if (args.out / "STOP").exists() or not args.watch or result["status"] not in ("failed", "waiting"):
                return 1
            production.report("waiting", result["reason"], retry_at=time.time() + args.cooldown)
            try:
                production.wait(args.cooldown)
            except SafetyStop:
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
