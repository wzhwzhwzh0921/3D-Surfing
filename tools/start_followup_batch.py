"""Freeze production code and start a persistent, resumable Codex batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scene_factory.automation import save
from scene_factory.model_backend import configured_model, configure_relay, model_identity
from scene_factory.render_policy import render_backend
from scene_factory.rendering import render_settings
from scene_factory.sources import DEFAULT_PROVIDER, PROVIDERS


def snapshot(destination):
    """Only executable sources/prompts/config templates; no private .env or assets."""
    files = {}
    for directory in ("scene_factory", "tools", "experiments"):
        for source in sorted((ROOT / directory).rglob("*")):
            if not source.is_file() or source.suffix not in {".py", ".txt"} or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(ROOT)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            files[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    save(destination / "source-manifest.json", files)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--seed-campaign", type=Path, action="append", default=[])
    parser.add_argument("--exclude-history", type=Path, action="append", default=[])
    parser.add_argument("--port", type=int, default=int(os.getenv("SCENEQA_CODEX_PORT", "18773")))
    parser.add_argument("--model", default=configured_model())
    parser.add_argument("--backend", choices=("codex", "relay", "plugin"), default=os.getenv("SCENEQA_MODEL_BACKEND", "codex"))
    parser.add_argument("--reasoning", default=os.getenv("SCENEQA_CODEX_REASONING", "xhigh"))
    parser.add_argument("--render-backend", choices=("local", "k8s"), default=render_backend())
    parser.add_argument("--max-scenes", type=int, default=5000)
    parser.add_argument("--provider", choices=[p for p in PROVIDERS if p != "catalog"],
                        default=os.getenv("SCENEQA_SOURCE_PROVIDER", DEFAULT_PROVIDER))
    parser.add_argument("--query", dest="queries", action="append", default=[])
    parser.add_argument("--max-bytes", type=int, default=64 * 1048576)
    args = parser.parse_args(argv)
    if min(args.target, args.max_scenes, args.max_bytes) < 1 or not 1 <= args.port <= 65535:
        parser.error("Target/max-scenes/max-bytes must be positive and port must be between 1 and 65535")
    os.environ["SCENEQA_MODEL_BACKEND"] = args.backend
    os.environ["SCENEQA_RENDER_BACKEND"] = args.render_backend
    os.environ["SCENEQA_MODEL"] = args.model
    if args.backend == "relay":
        configure_relay()
    identity = model_identity()
    settings = {"model": args.model, "model_backend": args.backend, "reasoning": args.reasoning,
                "port": args.port, "render_configuration": render_settings(), "max_scenes": args.max_scenes,
                "source_provider": args.provider, "queries": sorted(set(args.queries)), "max_bytes": args.max_bytes,
                "seed_campaign": [str(p.resolve()) for p in args.seed_campaign],
                "exclude_history": [str(p.resolve()) for p in args.exclude_history]}
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    receipt = out / "launch.json"
    if receipt.exists():
        old = json.loads(receipt.read_text())
        try:
            os.kill(old["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            parser.error("The recorded batch process is still running")
        if old["account_fingerprint"] != identity["account_fingerprint"]:
            parser.error("The original batch belongs to a different model account/provider configuration")
        command = old["command"]
        if old["target"] != args.target:
            parser.error("Target changed; use a new output directory")
        if any(old.get(key) != value for key, value in settings.items()):
            parser.error("Model/render settings changed; use a new output directory. Legacy batches must use their frozen launcher.")
    else:
        runtime = out / "runtime"
        snapshot(runtime)
        command = [sys.executable, "-m", "scene_factory", "workflow", "followup-batch",
                   "--out", str(out), "--target", str(args.target), "--model", args.model,
                   "--max-scenes", str(args.max_scenes), "--max-bytes", str(args.max_bytes),
                   "--provider", args.provider, "--watch"]
        for query in args.queries:
            command += ["--query", query]
        for source in args.seed_campaign:
            command += ["--seed-campaign", str(source.resolve())]
        for source in args.exclude_history:
            command += ["--exclude-history", str(source.resolve())]
    runtime = out / "runtime"
    manifest = json.loads((runtime / "source-manifest.json").read_text())
    if any(hashlib.sha256((runtime / name).read_bytes()).hexdigest() != digest for name, digest in manifest.items()):
        parser.error("Frozen runtime changed; refusing to resume")
    env = dict(os.environ, PYTHONUNBUFFERED="1", SCENEQA_MODEL_BACKEND=args.backend,
               SCENEQA_CODEX_MODEL=args.model, SCENEQA_CODEX_REASONING=args.reasoning,
               SCENEQA_RENDER_BACKEND=args.render_backend,
               SCENEQA_CODEX_PORT=str(args.port), PYTHONPATH=str(runtime))
    reset = ["SCENEQA_CODEX_ACTIVE", "SCENEQA_MODEL_ACTIVE"]
    if args.backend == "codex":
        reset += ["LUNA_API_KEY", "LUNA_KEY_FILE", "LUNA_CHAT_URL"]
    for key in reset:
        env.pop(key, None)
    with (out / "production.log").open("a") as log:
        process = subprocess.Popen(command, cwd=runtime, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    result = {"pid": process.pid, "created_at": time.time(), "target": args.target,
        "command": command, "cwd": str(runtime), **settings,
        "auth_method": identity.get("auth_method", "configured"), "account_fingerprint": identity["account_fingerprint"],
        "log": str(out / "production.log"), "render_location": args.render_backend}
    save(receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
