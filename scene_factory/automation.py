"""Durable discovery -> screening -> medium QA production, with finite budgets."""
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from collections import Counter

from .rendering import cleanup_workers, render_settings
from .sources import discover_batch, source_config, source_fingerprint
from .screening.policy import DEFAULT_VERSION

REPO = Path(__file__).resolve().parents[1]
STAGE_FILES = ["scene_factory/automation.py", "scene_factory/workflow.py", "scene_factory/codex_backend.py",
               "tools/scene_auto_pipeline.py",
               "scene_factory/sources.py", "tools/scene_pipeline.py",
               "scene_factory/live_sources.py",
               "scene_factory/multi_sources.py",
               "scene_factory/download.py", "scene_factory/render_policy.py",
               "scene_factory/rendering.py", "scene_factory/model_backend.py",
               "tools/scene_screen_batch.py", "tools/prompts/scene_screen_zh.txt",
               "scene_factory/screening/__init__.py", "scene_factory/screening/intake.py",
               "scene_factory/screening/judge.py", "scene_factory/screening/batch.py",
               "scene_factory/screening/gallery.py", "scene_factory/screening/policy.py",
               "scene_factory/screening/prompts/v1.txt", "scene_factory/screening/prompts/v2.txt",
               "scene_factory/screening/prompts/v3.txt",
               "scene_factory/screening/prompts/v4.txt", "scene_factory/screening/rich.py",
               "scene_factory/screening/prompts/v5.txt",
               "scene_factory/screening/inspection.py", "experiments/sol_augment/screen_view_worker.py",
               "tools/scene_feedback_loop.py", "tools/independent_qa_audit.py",
               "experiments/sol_augment/run.py", "experiments/sol_augment/repair_medium.py",
               "experiments/sol_augment/worker.py"]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_rows(path, entries):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in entries))
    temporary.replace(path)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Campaign:
    def __init__(self, args, *, discover=discover_batch, runner=None):
        self.args, self.discover, self.runner = args, discover, runner
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.discovery_options = dict(provider=args.provider, catalog=args.catalog,
                                      queries=args.queries, max_bytes=args.max_bytes,
                                      max_scan=args.max_scan,
                                      max_pages=args.max_pages, page_size=args.page_size)
        self.configuration = dict(schema_version=1, source=source_config(**self.discovery_options),
                                  model=args.model, endpoint=args.url, render=render_settings(), max_repairs=args.max_repairs,
                                  stages={name: file_sha(REPO / name) for name in STAGE_FILES})
        self.environment_minimums = {"outdoor":getattr(args,"min_outdoor",0),
                                     "indoor":getattr(args,"min_indoor",0)}
        self.historical_uids = set()
        exclusions = []
        for source in getattr(args, "exclude_history", []):
            self.historical_uids.update(str(r.get("uid") or r.get("scene_id")) for r in rows(source)
                                        if r.get("uid") or r.get("scene_id"))
            exclusions.append(file_sha(source))
        self.configuration["selection"] = dict(environment_minimums=self.environment_minimums,
            historical_exclusion_sha256=sorted(exclusions),
            screen_version=getattr(args,"screen_version",DEFAULT_VERSION))
        self.fingerprint = hashlib.sha256(json.dumps(self.configuration, sort_keys=True).encode()).hexdigest()
        self.state_path = self.out / "campaign.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "configuration": self.configuration, "fingerprint": self.fingerprint,
            "created_at": time.time(), "status": "running"}
        if self.state.get("fingerprint") != self.fingerprint:
            raise ValueError("Source or evaluation protocol changed; use a new --out directory")
        self.registry = {row["uid"]: row for row in rows(self.out / "registry.jsonl")} if (self.out / "registry.jsonl").exists() else {}
        self.batches = []
        for path in sorted((self.out / "batches").glob("batch_*/batch.json")):
            batch = json.loads(path.read_text())
            if batch.get("fingerprint") != self.fingerprint:
                raise ValueError("Batch configuration differs from campaign")
            self.batches.append((path.parent, batch))
        self.pod_prefix = "scene-auto-" + hashlib.sha256(str(self.out).encode()).hexdigest()[:10]
        self.screen_pod = self.pod_prefix + ("-work" if getattr(args,"single_worker",False) else "-screen")
        self.qa_pod = self.pod_prefix + ("-work" if getattr(args,"single_worker",False) else "-qa")
        self.worker_names = sorted({self.screen_pod,self.qa_pod})
        self.owns_workers = False
        self.secret = None

    def invoke(self, stage, sources, output, exclude=None):
        if self.runner:
            return self.runner(stage, Path(sources), Path(output), exclude)
        if self.secret is None:
            self.secret = Path(self.args.key_file).read_text().strip() if self.args.key_file else os.getenv("LUNA_API_KEY")
            key_file = os.getenv("LUNA_KEY_FILE")
            if not self.secret and key_file:
                self.secret = Path(key_file).read_text().strip()
            if not self.secret:
                sys.path.insert(0, str(REPO / "experiments/sol_augment"))
                import run as augment
                self.secret = augment.key()
            if not self.secret:
                raise ValueError("Provide LUNA_API_KEY or --key-file")
        env = dict(os.environ, LUNA_API_KEY=self.secret, LUNA_MODEL=self.args.model,
                   LUNA_CHAT_URL=self.args.url)
        output.mkdir(parents=True, exist_ok=True)
        if stage == "screen":
            command = [sys.executable, str(REPO / "tools/scene_pipeline.py"),
                       "--sources", str(sources), "--out", str(output),
                       "--pod", self.screen_pod, "--workers", str(self.args.workers),
                       "--max-bytes", str(self.args.max_bytes), "--url", self.args.url,
                       "--model", self.args.model]
            command += ["--version", getattr(self.args,"screen_version",DEFAULT_VERSION)]
            if exclude:
                command += ["--exclude-shas", str(exclude)]
        else:
            command = [sys.executable, str(REPO / "tools/scene_feedback_loop.py"),
                       "--sources", str(sources), "--out", str(output),
                       "--pod", self.qa_pod, "--keep-pod",
                       "--max-bytes", str(self.args.max_bytes),
                       "--max-repairs", str(self.args.max_repairs),
                       "--review-cache", str(self.out / "review_cache")]
        self.owns_workers = True
        with (output / "process.log").open("a") as log:
            process = subprocess.Popen(command, cwd=REPO, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                return process.wait(timeout=self.args.stage_timeout)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if sys.exc_info()[0] is KeyboardInterrupt:
                    raise
                raise TimeoutError(f"{stage} exceeded {self.args.stage_timeout}s; see process.log")

    def cleanup(self):
        if self.owns_workers:
            try:
                cleanup_workers(self.worker_names)
                save(self.out / "worker_cleanup.json", {"status": "ok", "backend": render_settings()['backend'],
                     "workers": self.worker_names})
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
                save(self.out / "worker_cleanup.json", {"status":"failed","reason":type(error).__name__})

    def batch_save(self, path, batch):
        save(path / "batch.json", batch)

    def outcome(self, row, status, phase, reason, **extra):
        self.registry[row["uid"]] = {**row, "status": status, "phase": phase,
                                     "reason": reason, **extra}
        self.report("running")

    def after_qa(self, row):
        """Optional downstream consumer, called after committing the QA outcome."""

    def report(self, status, reason=None):
        entries = list(self.registry.values())
        write_rows(self.out / "registry.jsonl", entries)
        for outcome in ("keep", "reject", "failed"):
            write_rows(self.out / (outcome + ".jsonl"), [r for r in entries if r["status"] == outcome])
        selected_deliveries = self.selected_deliveries()
        write_rows(self.out / "selected.jsonl", selected_deliveries)
        selected = sum(len(batch["sources"]) for _, batch in self.batches)
        cursor = self.batches[-1][1]["discovery"]["cursor"] if self.batches else None
        summary = dict(status=status, reason=reason, target=self.args.target,
                       discovered=selected, metadata_scanned=(cursor or {}).get("scanned", 0),
                       counts={s:sum(r["status"] == s for r in entries) for s in ("keep", "reject", "failed", "pending")},
                       kept_categories=dict(Counter(r.get("category", "UNKNOWN") for r in entries if r["status"] == "keep")),
                       batches=len(self.batches), level="medium", model_opinion_not_gold=True,
                       elapsed_seconds=round(time.time()-self.state["created_at"], 2))
        summary.update(selected_count=len(selected_deliveries),
            minimum_environments=self.environment_minimums,
            reviewed_environments=dict(Counter(self.environment(r) for r in entries if r["status"]=="keep")),
            selected_environments=dict(Counter(self.environment(r) for r in selected_deliveries)))
        self.state.update(status=status, reason=reason, updated_at=time.time(), target=self.args.target)
        save(self.state_path, self.state)
        save(self.out / "summary.json", summary)
        save(self.out / "progress.json", summary)
        page = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>自动场景问答生产</title>',
                '<style>body{font-family:system-ui;max-width:1200px;margin:24px auto;padding:0 16px;background:#eef2f6;line-height:1.7}article{padding:20px;background:white;margin:20px 0;border-radius:12px}.views{display:grid;grid-template-columns:1fr 1fr;gap:12px}img{width:100%}figure{margin:0}small{overflow-wrap:anywhere}@media(max-width:700px){.views{grid-template-columns:1fr}}</style>',
                '<h1>在线搜集 → 场景筛选 → 出题 → 审核返修</h1><p>最终留删以中等题模型审核为准。下载成功或场景初筛通过不计作交付。题目、答案与历次意见均保留。</p>',
                '<p>'+html.escape(json.dumps(summary, ensure_ascii=False))+'</p>']
        for row in entries:
            page.append('<article><h2>'+html.escape(str(row.get("name") or row["uid"]))+' · '+
                        {"keep":"已交付","reject":"剔除","failed":"处理失败","pending":"待出题"}[row["status"]]+'</h2>')
            folder = Path(row["folder"]) if row.get("folder") else None
            if folder and (folder / "question.json").exists():
                try:
                    q = json.loads((folder / "question.json").read_text())
                    if not isinstance(q.get("question"),str) or "answer" not in q:
                        raise ValueError("Incomplete question JSON")
                except (OSError,ValueError,TypeError,AttributeError):
                    page.append('<p>交付题目文件损坏，查看失败记录。</p></article>')
                    continue
                page.append('<div class="views">')
                for name,label in (("start.png","初始视角"),("reveal.png","揭示视角")):
                    page.append('<figure><img loading="lazy" src="'+html.escape(os.path.relpath(folder/name, self.out), quote=True)+'"><figcaption>'+label+'</figcaption></figure>')
                page.append('</div><p><strong>题目：</strong>'+html.escape(q["question"])+
                            '<br><strong>模型答案：</strong>'+html.escape(str(q["answer"]))+'</p>')
                page.append('<a href="'+html.escape(os.path.relpath(folder/"scene.glb",self.out),quote=True)+'">增强GLB</a> · <a href="'+html.escape(os.path.relpath(folder/"question.json",self.out),quote=True)+'">问题、答案及相机</a>')
            page.append('<p>'+html.escape(str(row.get("reason", "")))+'</p><small>'+html.escape(row["uid"])+'</small></article>')
        for path, batch in self.batches:
            for phase, gallery in (("筛选",path/"screen/screen"/DEFAULT_VERSION/"gallery.html"),("审核返修",path/"qa/gallery.html")):
                if gallery.exists():
                    page.append('<p><a href="'+html.escape(os.path.relpath(gallery,self.out),quote=True)+'">'+path.name+' · '+phase+'记录</a></p>')
        (self.out / "gallery.html").write_text("\n".join(page)+"</html>")
        return summary

    def keep_count(self):
        return sum(r["status"] == "keep" for r in self.registry.values())

    @staticmethod
    def environment(row):
        return row.get("screen_assessment",{}).get("environment", "unknown")

    def target_met(self):
        kept = [r for r in self.registry.values() if r["status"] == "keep"]
        counts = Counter(self.environment(r) for r in kept)
        return len(kept) >= self.args.target and all(counts[k]>=n for k,n in self.environment_minimums.items())

    def selected_deliveries(self):
        kept = [r for r in self.registry.values() if r["status"] == "keep"]
        selected = []
        for environment, minimum in self.environment_minimums.items():
            selected.extend([r for r in kept if self.environment(r)==environment][:minimum])
        reserved = sum(max(0,minimum-sum(self.environment(r)==environment for r in selected))
                       for environment,minimum in self.environment_minimums.items())
        ids = {r["uid"] for r in selected}
        available = max(0,self.args.target-reserved-len(selected))
        selected.extend([r for r in kept if r["uid"] not in ids][:available])
        return selected

    def process_batch(self, path, batch):
        if batch["phase"] == "selected":
            # Reconstructing sources from the journal also recovers a interrupted write.
            write_rows(path / "sources.jsonl", batch["sources"])
            excluded = path / "exclude_shas.txt"
            excluded.write_text("".join(r["glb_sha256"]+"\n" for r in self.registry.values()
                                        if r.get("glb_sha256") and r.get("batch") != path.name))
            batch["phase"] = "selected"
            self.batch_save(path, batch)
            try:
                code = self.invoke("screen", path/"sources.jsonl", path/"screen", excluded)
                summary = json.loads((path / "screen/summary.json").read_text())
                if code != 0:
                    raise RuntimeError("Screening process failed; see screen/process.log")
                decisions = {s: rows(path / "screen" / (s+".jsonl")) for s in ("keep","reject","failed")}
                expected = {r["uid"] for r in batch["sources"]}
                actual = [r["uid"] for group in decisions.values() for r in group]
                if set(actual) != expected or len(actual) != len(expected):
                    raise ValueError("Screening outputs do not cover this batch exactly")
                batch["screen_summary"] = summary
                batch["screen_keep"] = decisions["keep"]
                for status in ("reject","failed"):
                    for row in decisions[status]:
                        prior = self.registry.get(row["uid"], {})
                        if prior.get("phase")=="qa" and prior.get("status") in ("keep","reject"):
                            continue
                        self.outcome(row,status,"screen",row.get("reason") or row.get("screen_reason",""),batch=path.name)
                for row in decisions["keep"]:
                    prior = self.registry.get(row["uid"], {})
                    if prior.get("phase")=="qa" and prior.get("status") in ("keep","reject"):
                        continue
                    self.outcome(row,"pending","qa",row.get("screen_reason",""),batch=path.name)
                batch["phase"] = "screened"
                self.batch_save(path,batch)
            except Exception as error:
                for row in batch["sources"]:
                    prior = self.registry.get(row["uid"],{})
                    if prior.get("phase")=="qa" and prior.get("status") in ("keep","reject"):
                        continue
                    self.outcome(row,"failed","screen",type(error).__name__+": "+str(error),batch=path.name)
                batch.update(phase="done", operational_failure=True, error=str(error))
                self.batch_save(path,batch)
                return
        if batch["phase"] == "screened":
            done = set(batch.get("qa_done", []))
            for row in batch["screen_keep"]:
                prior = self.registry.get(row["uid"], {})
                if (prior.get("batch")==path.name and prior.get("phase")=="qa"
                        and prior.get("status") in ("keep","reject","failed")):
                    # Registry may have committed immediately before a crash in batch_save.
                    done.add(row["uid"])
                    batch["qa_done"] = sorted(done)
                    self.batch_save(path,batch)
                if row["uid"] in done:
                    continue
                if self.target_met():
                    return
                input_path = path / "qa_inputs" / (row["uid"]+".jsonl")
                write_rows(input_path,[row])
                qa = path / "qa"
                (qa / "summary.json").unlink(missing_ok=True)
                try:
                    code = self.invoke("qa",input_path,qa)
                    report = json.loads((qa/"summary.json").read_text())
                    items = report["results"]
                    if code != 0 or len(items) != 1 or items[0]["uid"] != row["uid"]:
                        raise ValueError("QA process failed or returned the wrong source")
                    result = items[0]
                    if result["status"] not in ("keep","reject","failed"):
                        raise ValueError("QA did not reach a terminal outcome")
                    extra = {k:result[k] for k in ("folder","result_path","audit_path","repairs","quality_source","gold") if k in result}
                    if result["status"] == "keep":
                        folder = Path(extra.get("folder", ""))
                        if not all((folder/name).is_file() for name in ("scene.glb","question.json","start.png","reveal.png","audit.json")):
                            raise ValueError("QA passed but its delivery files are missing")
                        extra["delivery_sha256"] = {name:file_sha(folder/name) for name in
                            ("scene.glb","question.json","start.png","reveal.png","audit.json")}
                    self.outcome(row,result["status"],"qa",result.get("reason",""),batch=path.name,**extra)
                except Exception as error:
                    self.outcome(row,"failed","qa",type(error).__name__+": "+str(error),batch=path.name)
                done.add(row["uid"])
                batch["qa_done"] = sorted(done)
                self.batch_save(path,batch)
                print(json.dumps({"event":"scene_finished","uid":row["uid"],"status":self.registry[row["uid"]]["status"],"keep":self.keep_count(),"target":self.args.target},ensure_ascii=False),flush=True)
                # Downstream failures must not overwrite the successful original
                # as a QA failure. The persisted registry supports replay on resume.
                self.after_qa(self.registry[row["uid"]])
            batch["phase"] = "done"
            batch["operational_failure"] = bool(batch["sources"]) and all(self.registry[r["uid"]]["status"] == "failed" for r in batch["sources"])
            self.batch_save(path,batch)

    def validate_existing_deliveries(self):
        missing = []
        for row in self.registry.values():
            if row["status"] == "keep":
                folder = Path(row["folder"])
                names = ("scene.glb","question.json","start.png","reveal.png","audit.json")
                intact = all((folder/name).is_file() for name in names)
                if intact and row.get("delivery_sha256"):
                    intact = all(file_sha(folder/name)==expected for name,expected in row["delivery_sha256"].items())
                if not intact:
                    row.update(status="failed",phase="delivery_integrity",reason="Existing delivery files are missing")
                    missing.append(row["uid"])
        if missing:
            raise ValueError("Existing delivery missing; cannot count previous keep: "+", ".join(missing))

    def retry_failed(self):
        if not getattr(self.args,"retry_failed",False):
            return
        for path,batch in self.batches:
            failed = [r["uid"] for r in batch["sources"]
                      if self.registry.get(r["uid"],{}).get("status")=="failed"]
            if not failed:
                continue
            screen_failed = any(self.registry[uid].get("phase") in ("screen","delivery_integrity") for uid in failed)
            batch["phase"] = "selected" if screen_failed or not batch.get("screen_keep") else "screened"
            batch["qa_done"] = [uid for uid in batch.get("qa_done",[]) if uid not in failed]
            batch["operational_failure"] = False
            for uid in failed:
                self.registry[uid].update(status="pending",reason="重试先前处理失败",phase="retry")
            self.batch_save(path,batch)

    def run(self):
        try:
            self.validate_existing_deliveries()
            self.retry_failed()
            self.report("running")
            while not self.target_met():
                pending = next(((p,b) for p,b in self.batches if b["phase"] != "done"),None)
                if pending:
                    self.process_batch(*pending)
                    self.report("running")
                    continue
                consecutive = 0
                for _, batch in reversed(self.batches):
                    if not batch.get("operational_failure"):
                        break
                    consecutive += 1
                if consecutive >= self.args.max_failed_batches:
                    return self.report("failed", "连续批次全部发生处理错误，请检查接口或集群；没有把这些错误算作质量剔除")
                selected = sum(len(b["sources"]) for _, b in self.batches)
                if selected >= self.args.max_scenes:
                    return self.report("scene_budget_exhausted", "达到素材下载处理数量预算，最终交付仍不足目标")
                if self.batches and self.batches[-1][1]["discovery"]["exhausted"]:
                    why=self.batches[-1][1]["discovery"]["stats"].get("exhaustion_reason")
                    return self.report("source_exhausted" if why=="end_of_source" else "discovery_budget_exhausted",why)
                cursor = self.batches[-1][1]["discovery"]["cursor"] if self.batches else None
                try:
                    discovered = self.discover(Path(self.args.discovery_cache or self.out/"discovery_cache"),
                        min(self.args.batch_size,self.args.max_scenes-selected), cursor,
                        excluded_uids=set(self.registry) | self.historical_uids, **self.discovery_options)
                except Exception as error:
                    save(self.out/"discovery_error.json",{"error":type(error).__name__+": "+str(error),"previous_cursor":cursor})
                    return self.report("failed", "素材发现请求失败；游标未推进，可用同一命令续跑："+str(error))
                if discovered["source_fingerprint"] != source_fingerprint(**self.discovery_options):
                    raise ValueError("Discovery source fingerprint mismatch")
                path = self.out / "batches" / f"batch_{len(self.batches)+1:05d}"
                batch = dict(fingerprint=self.fingerprint, phase="selected", sources=discovered["rows"],
                             discovery=discovered, qa_done=[])
                if not batch["sources"]:
                    if not discovered["exhausted"]:
                        raise ValueError("Discovery made no progress without exhaustion")
                    batch["phase"]="done"
                # One atomic selection journal preserves both sources and the advanced cursor.
                self.batch_save(path,batch)
                self.batches.append((path,batch))
                self.report("running")
            return self.report("complete", "达到最终审核通过题目的目标数量")
        except KeyboardInterrupt:
            self.report("interrupted", "已保存当前批次和游标，同一命令可续跑")
            raise
        except Exception as error:
            self.report("failed",type(error).__name__+": "+str(error))
            raise
        finally:
            self.cleanup()


def locked_campaign(args, **kwargs):
    out = Path(args.out).resolve()
    out.mkdir(parents=True,exist_ok=True)
    with (out / ".campaign.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("This output campaign is already running") from error
        return Campaign(args,**kwargs).run()
