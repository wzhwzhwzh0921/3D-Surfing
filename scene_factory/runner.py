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
from .openai_bridge import bridge
from .schema import read_plan
from .suitability import screen
from .validate import validate

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("blender_worker.py")
AGENT_SYSTEM = (
    "You are a production scene-QA planner. Use the supplied inventory, four previews, "
    "plan schema, and complete example as your interface. Decide suitability first. "
    "If fewer than three distinct native small-prop categories can be augmented "
    "naturally, write unsuitable.json and stop. Otherwise write plan.json promptly, "
    "then invoke build and verify and revise the plan if needed. Do not proactively "
    "inspect framework source, scan the filesystem, install packages, or write custom "
    "rendering scripts. If build or verify fails, inspect the concise error and "
    "output/validation.json, then revise plan.json. Never read package source code. "
    "Keep original geometry/materials and never invent unverifiable answer counts."
)


def blender_exe() -> str:
    return os.environ.get("SCENEQA_BLENDER", "/home/ubuntu/blender/tools/blender-5.2.1-linux-x64/blender")


def provider_key(provider: str, key_file: Path | None = None) -> str:
    if key_file:
        content = key_file.read_text().strip()
        if "\n" not in content and "=" not in content:
            return content
        names = (("TENCENT_ENDPOINT_API_KEY", "TOKENHUB_API_KEY") if provider == "tokenhub"
                 else ("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        entries = {}
        for line in content.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                entries[name.strip().removeprefix("export ")] = value.strip().strip('"\'')
        return next((entries[name] for name in names if entries.get(name)), "")
    names = ("TOKENHUB_API_KEY", "TENCENT_ENDPOINT_API_KEY") if provider == "tokenhub" else (
        "DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    return next((os.environ[name] for name in names if os.environ.get(name)), "")


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
Use {ROOT / 'examples/sample_plan.json'} only as a STRUCTURAL example; adapt every
object name, location and question to this source. Read inventory and four previews.
Do not inspect package source files, runner logs, task.txt, or status.json; the
schema and supplied commands are the complete interface. Start writing the plan
as soon as you have identified suitable original objects and support surfaces.
There must be exactly three independently augmented variants from the SAME original source:
simple, medium, hard. Use only native visible scene objects as copy templates.
No imported plush toys, props, color changes, invisible labels or unrelated decorations.
Copy an object to natural physical support (table, shelf, floor, bench), usually behind
existing occluders. Explicitly list all relevant ORIGINAL instances in entities;
include every matching original, even hidden ones. Keep source geometry and materials.
The three questions must have different target kinds and answers. Use Chinese natural
questions. Simple is a count, medium a conditional count using pose/region, and
hard should preferably identify an object under one named local occluder while
placing a different distractor under another; comparison is also allowed. Medium
difficulty must come from VISUAL OCCLUSION,
not from adding many targets: use at most two target copies, place at least one
behind a nearby native scene prop, and declare occlusion_targets with its exact
blocker mesh name(s). Use schema_version 1.1. The builder raycasts target surface
samples and requires at least 35% blocked by that named prop at the start view;
the blocker must be small relative to the scene and close to the target, so a
perimeter wall alone is not sufficient. Hard needs at least TWO distinct nearby
occluders and two hidden targets; the answer target must be tied to a named anchor.
Place enough copies to meet the visibility gate: at least 1 / 1 / 2 hidden from
start for simple / medium / hard. Choose an explicit
start camera so the answer is not directly available. All copies must be discoverable
from a global orbit or from a focused, zoomed local view. Hard need not claim
30 actions without blind test.
If fewer than THREE distinct, small, naturally repeatable native object categories
exist, do not invent kinds such as whole buildings, walls, structural beams,
ground or water merely to fill the quota. Instead write {work / 'unsuitable.json'}
with {{"status":"unsuitable","reasons":["..."],"available_native_categories":["..."]}}
and finish. This is an acceptable outcome for batch filtering.
Your tools: Bash, Read, Write, Edit, Glob, Grep. You can call:
{sys.executable} -m scene_factory.cli build --source {work / 'source.glb'} --plan {work / 'plan.json'} --output {work / 'output'}
{sys.executable} -m scene_factory.cli verify --plan {work / 'plan.json'} --output {work / 'output'}
Iterate the plan/build/verify inside this same session until verify succeeds.
No second Claude session will be launched. Finally write {work / 'agent_notes.md'}
with naturalness reasoning and any uncertainties. Do not assert an untested 30-step claim.
"""


def run_one(row: dict, output_root: Path, *, source_path: Path | None = None,
            upstream: str | None = None, model: str | None = None, provider: str = "tokenhub",
            key_file: Path | None = None, preflight: bool = True,
            max_bytes=100_000_000, timeout=7200, force=False, dry_run=False) -> dict:
    scene_id = row.get("uid") or "adhoc"
    work = (output_root / scene_id).resolve()
    output = work / "output"
    validation_path = output / "validation.json"
    if validation_path.exists() and not force:
        old = json.loads(validation_path.read_text())
        if old.get("status") == "candidate":
            return {"scene_id": scene_id, "status": "skipped", "work": str(work)}
    status_file = work / "status.json"
    if status_file.exists() and not force:
        old_status = json.loads(status_file.read_text())
        if old_status.get("status") == "unsuitable":
            return {"scene_id": scene_id, "status": "skipped_unsuitable", "work": str(work)}
    work.mkdir(parents=True, exist_ok=True)
    if force:
        (work / "unsuitable.json").unlink(missing_ok=True)
    state = {"scene_id": scene_id, "status": "running", "started": time.time(),
             "source": {k: row.get(k) for k in ("uid", "name", "tier", "category", "author", "license", "source_url", "download_url")}}
    status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    api_started = False
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
        if preflight:
            preflight_report = screen(json.loads((inspect / "inventory.json").read_text()))
            state["preflight"] = preflight_report
            (work / "preflight.json").write_text(json.dumps(preflight_report, ensure_ascii=False, indent=2))
            if preflight_report["status"] == "likely_unsuitable":
                unsuitable = {"status": "unsuitable", "source": "deterministic_name_screen",
                              "reasons": [preflight_report["rule"]],
                              "available_native_categories": preflight_report["named_prop_categories"]}
                (work / "unsuitable.json").write_text(json.dumps(unsuitable, ensure_ascii=False, indent=2))
                state["status"] = "unsuitable"
                state["unsuitable"] = unsuitable
                state["completed"] = time.time()
                return state
        key = provider_key(provider, key_file)
        if not key:
            raise RuntimeError(f"No {provider} key: set the matching environment variable or --key-file")
        claude = shutil.which("claude")
        if not claude:
            raise RuntimeError("Claude Code CLI not found in PATH")
        model = model or os.environ.get("SCENEQA_MODEL") or (
            "deepseek/deepseek-flash" if provider == "tokenhub" else "deepseek-flash")
        upstream = upstream or os.environ.get("SCENEQA_UPSTREAM") or (
            "https://tokenhub.tencentmaas.com/v1/chat/completions" if provider == "tokenhub"
            else "https://api.deepseek.com/anthropic")
        usage_log = work / "api_usage.jsonl"
        if usage_log.exists():
            usage_log.unlink()
        api_started = True
        proxy_factory = bridge if provider == "tokenhub" else gateway
        proxy_args = ({"endpoint": upstream} if provider == "tokenhub" else {"upstream": upstream})
        with proxy_factory(**proxy_args, key=key, model=model, output=usage_log,
                           max_tokens=65536, retry_delays=(20, 40, 60, 120), timeout=900) as proxy:
            env = os.environ.copy()
            # The model receives Bash. Only the opaque loopback proxy token may
            # enter that process; never expose the real upstream credential.
            for name in ("TOKENHUB_API_KEY", "TENCENT_ENDPOINT_API_KEY", "DEEPSEEK_API_KEY",
                         "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                env.pop(name, None)
            env.update({"ANTHROPIC_BASE_URL": proxy.url, "ANTHROPIC_API_KEY": proxy.token,
                        "ANTHROPIC_AUTH_TOKEN": proxy.token, "ANTHROPIC_MODEL": model,
                        "SCENEQA_BLENDER": blender_exe(),
                        "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                        "PIP_NO_CACHE_DIR": "1", "TMPDIR": str(work)})
            command = [claude, "--bare", "--safe-mode", "--print", "--verbose",
                       "--output-format", "stream-json", "--model", "sonnet" if provider == "tokenhub" else model,
                       "--effort", "low", "--append-system-prompt", AGENT_SYSTEM,
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
        unsuitable_path = work / "unsuitable.json"
        if unsuitable_path.exists():
            unsuitable = json.loads(unsuitable_path.read_text())
            if unsuitable.get("status") != "unsuitable" or not unsuitable.get("reasons"):
                raise ValueError("unsuitable.json must contain status and reasons")
            state["status"] = "unsuitable"
            state["unsuitable"] = unsuitable
            state["completed"] = time.time()
            return state
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
    except subprocess.TimeoutExpired:
        state["status"] = "failed"
        state["error"] = f"Claude session exceeded {timeout} seconds"
        state["completed"] = time.time()
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["completed"] = time.time()
    finally:
        usage_log = work / "api_usage.jsonl"
        if api_started and usage_log.exists() and "usage" not in state:
            state["usage"] = summarize_usage(usage_log)
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
