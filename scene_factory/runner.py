"""One Claude Code + DeepSeek session per input scene, with resumable artifacts."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .download import copy_local, download
from .gateway import gateway, summarize_usage
from .schema import read_plan
from .validate import validate

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("blender_worker.py")


def blender_exe() -> str:
    return os.environ.get("SCENEQA_BLENDER", "/home/ubuntu/blender/tools/blender-5.2.1-linux-x64/blender")


def blender(mode: str, source: Path, output: Path, plan: Path | None = None, *, timeout=1800) -> None:
    command = [blender_exe(), "--background", "--threads", "2", "--python", str(WORKER),
               "--", mode, str(source), str(output)]
    if plan:
        command.append(str(plan))
    log = output / f"blender_{mode}.log"
    output.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        result = subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode or "Traceback (most recent call last)" in log.read_text(errors="replace"):
        raise RuntimeError(f"Blender {mode} failed; inspect {log}")


def prompt_for(scene_id: str, work: Path) -> str:
    return f"""You are preparing one 3D scene QA benchmark item in ONE session.
Scene ID: {scene_id}. Work directory: {work}.
The source is {work / 'source.glb'}. Inventory: {work / 'inspect/inventory.json'}.
Four color previews are in {work / 'inspect'}; use Read on the PNGs.
Create {work / 'plan.json'} following {ROOT / 'docs/PLAN_SCHEMA.md'}.
There must be exactly three independently augmented variants from the SAME original source:
simple, medium, hard. Use only native visible scene objects as copy templates.
No imported plush toys, props, color changes, invisible labels or unrelated decorations.
Copy an object to natural physical support (table, shelf, floor, bench), usually behind
existing occluders. Explicitly list all relevant ORIGINAL instances in entities;
include every matching original, even hidden ones. Keep source geometry and materials.
The three questions must have different target kinds and answers. Use Chinese natural
questions. Simple is a count, medium a conditional count using pose/region, hard a
comparison between two kinds. Place enough copies to meet the visibility gate:
at least 1 / 2 / 3 hidden from start for simple / medium / hard. Choose an explicit
start camera so the answer is not directly available. All copies must be discoverable
from at least one of 24 orbit probes. Hard need not claim 30 actions without blind test.
Your tools: Bash, Read, Write, Edit, Glob, Grep. You can call:
{sys.executable} -m scene_factory.cli build --source {work / 'source.glb'} --plan {work / 'plan.json'} --output {work / 'output'}
{sys.executable} -m scene_factory.cli verify --plan {work / 'plan.json'} --output {work / 'output'}
Iterate the plan/build/verify inside this same session until verify succeeds.
No second Claude session will be launched. Finally write {work / 'agent_notes.md'}
with naturalness reasoning and any uncertainties. Do not assert an untested 30-step claim.
"""


def run_one(row: dict, output_root: Path, *, source_path: Path | None = None,
            upstream: str | None = None, model: str | None = None,
            max_bytes=100_000_000, timeout=7200, force=False, dry_run=False) -> dict:
    scene_id = row.get("uid") or "adhoc"
    work = (output_root / scene_id).resolve()
    output = work / "output"
    validation_path = output / "validation.json"
    if validation_path.exists() and not force:
        old = json.loads(validation_path.read_text())
        if old.get("status") == "candidate":
            return {"scene_id": scene_id, "status": "skipped", "work": str(work)}
    work.mkdir(parents=True, exist_ok=True)
    status_file = work / "status.json"
    state = {"scene_id": scene_id, "status": "running", "started": time.time(),
             "source": {k: row.get(k) for k in ("uid", "name", "tier", "category", "author", "license", "source_url", "download_url")}}
    status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    try:
        source = work / "source.glb"
        if source_path:
            asset = copy_local(source_path, source, max_bytes)
        else:
            asset = download(row["download_url"], source, max_bytes=max_bytes,
                             expected_sha256=row.get("sha256"))
        state["asset"] = asset
        inspect = work / "inspect"
        if not (inspect / "inventory.json").exists():
            blender("inspect", source, inspect)
        prompt = prompt_for(scene_id, work)
        (work / "task.txt").write_text(prompt)
        if dry_run:
            state["status"] = "prepared"
            return state
        key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY or ANTHROPIC_AUTH_TOKEN is required for a live run")
        claude = shutil.which("claude")
        if not claude:
            raise RuntimeError("Claude Code CLI not found in PATH")
        model = model or os.environ.get("SCENEQA_MODEL", "deepseek-flash")
        upstream = upstream or os.environ.get("SCENEQA_UPSTREAM", "https://api.deepseek.com/anthropic")
        usage_log = work / "api_usage.jsonl"
        if usage_log.exists():
            usage_log.unlink()
        with gateway(upstream=upstream, key=key, model=model, output=usage_log,
                     max_tokens=65536, retry_delays=(20, 40, 60, 120)) as proxy:
            env = os.environ.copy()
            env.update({"ANTHROPIC_BASE_URL": proxy.url, "ANTHROPIC_API_KEY": proxy.token,
                        "ANTHROPIC_AUTH_TOKEN": proxy.token, "ANTHROPIC_MODEL": model,
                        "SCENEQA_BLENDER": blender_exe(),
                        "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")})
            command = [claude, "--bare", "--safe-mode", "--print", "--verbose",
                       "--output-format", "stream-json", "--model", model, "--effort", "max",
                       "--tools", "Bash,Read,Write,Edit,Glob,Grep",
                       "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
                       "--permission-mode", "acceptEdits", "--strict-mcp-config",
                       "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                       "--no-session-persistence", "--disable-slash-commands", prompt]
            with (work / "claude_stream.jsonl").open("w") as out, (work / "claude_stderr.log").open("w") as err:
                result = subprocess.run(command, cwd=work, env=env, stdout=out, stderr=err,
                                        timeout=timeout)
            state["claude_exit_code"] = result.returncode
            state["usage"] = summarize_usage(usage_log)
            if result.returncode:
                raise RuntimeError(f"Claude session failed with exit code {result.returncode}")
        plan_path = work / "plan.json"
        plan = read_plan(plan_path)
        plan["scene_id"] = scene_id
        plan["source"] = state["source"]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        # Build/verify once more outside the agent session so agent claims are not trusted.
        blender("build", source, output, plan_path)
        report = validate(plan_path, output)
        state["status"] = report["status"]
        state["completed"] = time.time()
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["completed"] = time.time()
    finally:
        status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def run_batch(rows: list[dict], output_root: Path, *, workers=1, **kwargs) -> list[dict]:
    if workers < 1 or workers > 4:
        raise ValueError("workers must be 1..4")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, row, output_root, **kwargs) for row in rows]
        for future in as_completed(futures):
            results.append(future.result())
    return results
