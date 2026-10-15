"""Prepare downloadable scenes, render with the configured backend, and run the short screening step."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
from scene_factory.download import download, glb_header
from scene_factory.sources import SIZE_FIELDS, source_size_bytes  # retain the shared alias export
from .policy import DEFAULT_MODEL, DEFAULT_URL, DEFAULT_VERSION, VERSIONS, VIEWS, prompt_path


def stream_sha256(path: Path) -> str:
    """Bound memory usage and support Python 3.10."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def key_environment(key_file: Path | None) -> dict[str, str]:
    """Pass the secret only in subprocess environments, never on the command line."""
    env = dict(os.environ)
    path = key_file or (Path(env["LUNA_KEY_FILE"]) if env.get("LUNA_KEY_FILE") else None)
    if path:
        secret = path.expanduser().read_text().strip()
        # A plain key and a single shell-style assignment are both supported; do not eval.
        assignment = re.fullmatch(r"(?:export\s+)?LUNA_API_KEY\s*=\s*(.*)", secret)
        if assignment:
            parts = shlex.split(assignment.group(1))
            if len(parts) != 1:
                raise ValueError("key file must contain one API key")
            secret = parts[0]
        if not secret or any(char.isspace() for char in secret):
            raise ValueError("key file must contain one API key")
        env["LUNA_API_KEY"] = secret
        env.pop("LUNA_KEY_FILE", None)
    return env


def write_json(path: Path, value: object, *, lines: bool = False) -> None:
    text = ("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value)
            if lines else json.dumps(value, ensure_ascii=False, indent=2))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def preflight_size(row: dict) -> int | None:
    return source_size_bytes(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True,
                        help="JSONL rows: uid + url or source_glb/path")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--captures", type=Path,
                        help="reuse remote images under captures/UID; otherwise render with the configured backend")
    parser.add_argument("--pod", default="scene-sol-screen-batch")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=35 * 1048576)
    parser.add_argument("--url", default=os.getenv("LUNA_CHAT_URL", DEFAULT_URL))
    parser.add_argument("--model", default=os.getenv("LUNA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--version", choices=VERSIONS, default=DEFAULT_VERSION)
    parser.add_argument("--min-objects", type=int, default=20)
    parser.add_argument("--min-categories", type=int, default=8)
    parser.add_argument("--min-regions", type=int, default=3)
    parser.add_argument("--inspection-rounds", type=int, default=2)
    parser.add_argument("--views-per-round", type=int, default=2)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--exclude-shas", type=Path,
                        help="one historical SHA256 per line; reject matching content before rendering")
    args = parser.parse_args(argv)
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")
    if any(getattr(args, name) < 1 for name in
           ("min_objects", "min_categories", "min_regions", "inspection_rounds", "views_per_round")):
        parser.error("richness thresholds and inspection budgets must be positive")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    sources, failures, keep, reject, seen = [], [], [], [], set()
    content_seen = set()
    input_count = 0
    env = dict(os.environ)
    # Current outputs always replace the preceding batch, including all-failed batches.
    screen_summary = out / "screen" / args.version / "summary.json"

    def reason(exc: Exception) -> str:
        detail = f"{type(exc).__name__}: {exc}"
        if env.get("LUNA_API_KEY"):
            detail = detail.replace(env["LUNA_API_KEY"], "[redacted]")
        return detail[:1000]

    def failure(row: dict, uid: str, stage: str, detail: str) -> None:
        if env.get("LUNA_API_KEY"):
            detail = detail.replace(env["LUNA_API_KEY"], "[redacted]")
        failures.append({**row, "uid": uid, "status": "failed", "stage": stage,
                         "reason": detail})

    def persist(stage: str, *, fatal: bool = False) -> dict:
        counts = {"keep": len(keep), "reject": len(reject), "failed": len(failures)}
        status = "running" if stage != "complete" else (
            "failed" if fatal or failures and not (keep or reject) else
            "partial" if failures else "complete")
        summary = {"schema_version": 1, "status": status, "stage": stage, "screen_version": args.version,
                   "input_count": input_count, "source_count": len(sources),
                   "counts": counts, "intake_failed": sum(r["stage"] == "intake" for r in failures),
                   "render_failed": sum(r["stage"] == "render" for r in failures),
                   "screen_failed": sum(r["stage"] == "screen" for r in failures),
                   "preflight_rejected": sum(r.get("stage") == "preflight" for r in reject),
                   "machine_only": True, "acceptance_written": False,
                   "next_step": str(out / "keep.jsonl"),
                   "outputs": {name: str(out / f"{name}.jsonl") for name in ("keep", "reject", "failed")}}
        for name, rows in (("keep", keep), ("reject", reject), ("failed", failures)):
            write_json(out / f"{name}.jsonl", rows, lines=True)
        write_json(out / "intake_failures.json", [r for r in failures if r["stage"] == "intake"])
        write_json(out / "render_failures.json", [r for r in failures if r["stage"] == "render"])
        write_json(out / "summary.json", summary)
        return summary

    persist("intake")
    try:
        for name in ("summary.json", "keep.jsonl", "reject.jsonl", "failed.jsonl"):
            screen_summary.with_name(name).unlink(missing_ok=True)
        env = key_environment(args.key_file)
        env.update(LUNA_CHAT_URL=args.url, LUNA_MODEL=args.model)
        if args.exclude_shas:
            for line in args.exclude_shas.read_text().splitlines():
                digest = line.strip().lower()
                if not digest:
                    continue
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("--exclude-shas requires one SHA256 per nonblank line")
                content_seen.add(digest)
        lines = args.sources.read_text().splitlines()
    except Exception as exc:
        failure({}, "__batch__", "intake", reason(exc))
        write_json(out / "sources.jsonl", [], lines=True)
        summary = persist("complete", fatal=True)
        print(json.dumps(summary, ensure_ascii=False))
        return 1
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        input_count += 1
        uid, row = f"row_{index}", {}
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("source row must be a JSON object")
            row = parsed
            original = row.get("source_glb") or row.get("path")
            url = row.get("url") or row.get("glb_url")
            candidate = row.get("uid") or hashlib.sha256(str(url or original).encode()).hexdigest()[:32]
            if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", candidate):
                raise ValueError("uid must be a safe, nonblank file name")
            uid = candidate
            if uid in seen:
                raise ValueError("duplicate uid (first occurrence owns this uid)")
            seen.add(uid)
            if not original and not url:
                raise ValueError("provide url or source_glb/path")
            declared = preflight_size(row)
            if declared is not None and declared > args.max_bytes:
                reject.append({**row, "uid": uid, "status": "reject", "stage": "preflight",
                               "screen_reason": f"metadata size {declared} exceeds {args.max_bytes} byte budget"})
                continue
            if original:
                model = Path(original).expanduser().resolve()
                glb_header(model)
                if model.stat().st_size > args.max_bytes:
                    reject.append({**row, "uid": uid, "status": "reject", "stage": "preflight",
                                   "screen_reason": f"source size exceeds {args.max_bytes} byte budget"})
                    continue
            else:
                # URL-specific cache paths prevent a changed URL for the same UID using an old GLB.
                cache_id = hashlib.sha256(str(url).encode()).hexdigest()[:16]
                model = out / "sources" / uid / f"{cache_id}.glb"
                download(url, model, max_bytes=args.max_bytes, expected_sha256=row.get("glb_sha256"))
                glb_header(model)
                if model.stat().st_size > args.max_bytes:
                    raise ValueError("downloaded source exceeds size budget")
            prepared = {**row, "uid": uid, "source_glb": str(model),
                        "glb_sha256": stream_sha256(model)}
            prepared.pop("source_inspect", None)
            if prepared["glb_sha256"] in content_seen:
                reject.append({**prepared, "status": "reject", "stage": "preflight",
                               "reason_code": "duplicate_content",
                               "screen_reason": "asset SHA256 already processed or present in this batch"})
                continue
            content_seen.add(prepared["glb_sha256"])
            sources.append(prepared)
        except Exception as exc:
            failure(row, uid, "intake", reason(exc))
    manifest = out / "sources.jsonl"
    write_json(manifest, sources, lines=True)
    persist("render")
    captures = args.captures.resolve() if args.captures else out / "captures"
    render_error = None
    worker_sha = None
    if sources and not args.captures:
        try:
            worker_sha = stream_sha256(REPO / "experiments/sol_augment/worker.py")
            subprocess.run([sys.executable, str(REPO / "experiments/sol_augment/run.py"),
                            "--queue", str(manifest), "--out", str(out / "remote"),
                            "--pod", args.pod, "--screen-only"], check=True, env=env)
        except Exception as exc:
            render_error = reason(exc)
    ready = []
    for row in sources:
        uid = row["uid"]
        target = captures / uid
        try:
            if not args.captures:
                inspect = out / "remote" / uid / "inspect"
                fingerprint = json.loads((inspect / "fingerprint.json").read_text())
                if (fingerprint.get("source_sha256") != row["glb_sha256"] or
                        fingerprint.get("worker_sha256") != worker_sha):
                    raise ValueError("remote source/worker fingerprint mismatch")
                if not all((inspect / f"{view}.png").is_file() and
                           (inspect / f"{view}.png").stat().st_size for view in VIEWS):
                    raise ValueError("remote evidence incomplete")
                target.mkdir(parents=True, exist_ok=True)
                for view in VIEWS:
                    shutil.copyfile(inspect / f"{view}.png", target / f"{view}.png")
                row["source_inspect"] = str(inspect)
            if not all((target / f"{view}.png").is_file() and
                       (target / f"{view}.png").stat().st_size for view in VIEWS):
                raise ValueError("missing or empty capture image")
            ready.append(row)
        except Exception as exc:
            detail = reason(exc)
            if not args.captures:
                for view in VIEWS:
                    try:
                        (target / f"{view}.png").unlink(missing_ok=True)
                    except OSError as cleanup:
                        detail += "; capture cleanup: " + reason(cleanup)
            failure(row, uid, "render", detail + (f"; render subprocess: {render_error}" if render_error else ""))
    queue = out / "uids.txt"
    queue.write_text("".join(row["uid"] + "\n" for row in ready))
    write_json(manifest, sources, lines=True)
    persist("screen")
    if ready:
        command = [sys.executable, str(REPO / "tools/scene_screen_batch.py"),
                   "--queue", str(queue), "--sources", str(manifest), "--captures", str(captures),
                   "--output", str(out / "screen"), "--workers", str(args.workers),
                   "--url", args.url, "--model", args.model,
                   "--version", args.version, "--prompt-file", str(prompt_path(args.version))]
        for name in ("min_objects", "min_categories", "min_regions", "inspection_rounds", "views_per_round"):
            command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
        if args.version in ("v4", "v5") and not args.captures:
            # Reuse the initial evidence worker; a single-worker campaign must
            # not request a second GPU while its first pod remains allocated.
            command.extend(["--render-pod", args.pod])
        try:
            subprocess.run(command, check=True, env=env)
            summary = json.loads(screen_summary.read_text())
            results = summary.get("results")
            if not isinstance(results, list):
                raise ValueError("screen summary requires results array")
        except Exception as exc:
            for row in ready:
                failure(row, row["uid"], "screen", reason(exc))
        else:
            decisions, duplicate = {}, set()
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("uid"), str):
                    continue
                uid = result["uid"]
                if uid in decisions:
                    duplicate.add(uid)
                decisions[uid] = result
            for row in ready:
                uid = row["uid"]
                result = decisions.get(uid, {})
                judgment = result.get("judgment")
                if (uid not in duplicate and result.get("status") == "ok" and isinstance(judgment, dict)
                        and judgment.get("uid") == uid and judgment.get("decision") in ("keep", "reject")
                        and isinstance(judgment.get("reason"), str) and judgment["reason"].strip()):
                    selected = {**row, "status": judgment["decision"], "stage": "screen",
                                "screen_reason": judgment["reason"], "screen_version": args.version,
                                "screen_assessment": judgment,
                                "screen_record": str(screen_summary.parent / uid / "record.json")}
                    (keep if judgment["decision"] == "keep" else reject).append(selected)
                else:
                    detail = "duplicate screen result" if uid in duplicate else (
                        str(result.get("errors")) if result.get("errors") else "missing or invalid screen result")
                    failure(row, uid, "screen", detail)
    summary = persist("complete")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
