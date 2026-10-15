"""Batch keep/reject screening; v4 can request extra 3D views on K8s."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import time

from .gallery import gallery
from .judge import ScreenConfig, failed_record, judge_scene, write_record
from .policy import DEFAULT_MODEL, DEFAULT_URL, DEFAULT_VERSION, VERSIONS, prompt_text


def summarize(results: list[dict], *, version: str, model: str,
              queue: Path, started: float) -> dict:
    token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    totals = {key: sum(usage.get(key, 0) for row in results for usage in row.get("usage", []))
              for key in token_fields}
    summary = {
        "version": version, "model": model, "queue": str(queue),
        "scene_count": len(results), "machine_only": True, "acceptance_written": False,
        "elapsed_seconds": round(time.time() - started, 2),
        "counts": {decision: sum(row.get("judgment", {}).get("decision") == decision
                                 for row in results) for decision in ("keep", "reject")},
        "errors": sum(row["status"] != "ok" for row in results),
        "token_totals": totals, "cost_usd": None, "results": results,
    }
    cached_count = sum(row.get("cached", False) for row in results)
    summary.update(cached_scene_count=cached_count,
                   new_scene_count=len(results) - cached_count,
                   local_cache_hit_ratio=cached_count / len(results) if results else 0,
                   historical_token_totals=totals,
                   new_token_totals={key: sum(usage.get(key, 0) for row in results
                                             if not row.get("cached", False)
                                             for usage in row.get("usage", []))
                                     for key in token_fields})
    provider_usage = [usage["prompt_tokens_details"]["cached_tokens"]
                      for row in results if not row.get("cached", False)
                      for usage in row.get("usage", [])
                      if "cached_tokens" in usage.get("prompt_tokens_details", {})]
    if provider_usage:
        summary["provider_cached_tokens"] = sum(provider_usage)
    served_models = sorted({row["served_model"] for row in results if row.get("served_model")})
    summary["served_models"] = {model: sum(row.get("served_model") == model for row in results)
                                for model in served_models}
    return summary


def run_batch(uids: list[str], config: ScreenConfig, secret: str,
              sources: dict, *, workers: int, queue: Path) -> dict:
    output = config.output / config.version
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    renderer = None
    if config.version in ("v4", "v5"):
        from .inspection import InspectionRenderer
        renderer = InspectionRenderer(config.output, pod=config.render_pod)

    def safe_run(uid: str) -> dict:
        try:
            return judge_scene(uid, config, secret, sources.get(uid), renderer=renderer)
        except Exception as exc:
            row = failed_record(uid, sources.get(uid, {}), type(exc).__name__ + ": " + str(exc)[:200])
            folder = output / uid
            folder.mkdir(exist_ok=True)
            write_record(folder, row)
            return row

    results = []
    cleanup_error = None
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(safe_run, uid): uid for uid in uids}
            for future in as_completed(futures):
                row = future.result()
                results.append(row)
                progress = {
                "total": len(uids), "completed": len(results), "pending": len(uids) - len(results),
                "keep": sum(x.get("judgment", {}).get("decision") == "keep" for x in results),
                "reject": sum(x.get("judgment", {}).get("decision") == "reject" for x in results),
                "failed": sum(x["status"] != "ok" for x in results),
                "elapsed_seconds": round(time.time() - started, 2),
            }
                temporary = output / "progress.tmp"
                temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(output / "progress.json")
                print(json.dumps({"uid": row["uid"], "status": row["status"],
                                  "judgment": row.get("judgment"), "attempts": row.get("attempts"),
                                  "completed": len(results), "total": len(uids)}, ensure_ascii=False), flush=True)
    finally:
        if renderer:
            try:
                renderer.close()
            except Exception as exc:
                cleanup_error = type(exc).__name__
    order = {uid: index for index, uid in enumerate(uids)}
    results.sort(key=lambda row: order[row["uid"]])
    summary = summarize(results, version=config.version, model=config.model,
                        queue=queue, started=started)
    if config.version in ("v4", "v5"):
        from .rich import settings
        summary["trial_thresholds"] = settings(config)
        summary["local_3d_views"] = sum(len(row.get("inspection_views", [])) for row in results)
    if cleanup_error:
        summary["cleanup_error"] = cleanup_error
    for decision in ("keep", "reject", "failed"):
        selected = [row for row in results
                    if (row["status"] != "ok" if decision == "failed" else
                        row["status"] == "ok" and row.get("judgment", {}).get("decision") == decision)]
        (output / f"{decision}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    gallery(summary, output, config.captures, sources)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--version", choices=VERSIONS, default=DEFAULT_VERSION)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--captures", type=Path, default=Path("captures"))
    parser.add_argument("--output", type=Path, default=Path("scene-screen-results"))
    parser.add_argument("--url", default=os.getenv("LUNA_CHAT_URL", DEFAULT_URL))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-objects", type=int, default=20)
    parser.add_argument("--min-categories", type=int, default=8)
    parser.add_argument("--min-regions", type=int, default=3)
    parser.add_argument("--inspection-rounds", type=int, default=2)
    parser.add_argument("--views-per-round", type=int, default=2)
    parser.add_argument("--render-pod", help="reuse a caller-owned K8s worker; caller handles its cleanup")
    args = parser.parse_args(argv)
    prompt = args.prompt_file.read_text() if args.prompt_file else prompt_text(args.version)
    sources = {row["uid"]: row for line in args.sources.read_text().splitlines()
               if line.strip() for row in [json.loads(line)]} if args.sources else {}
    uids = list(dict.fromkeys(args.queue.read_text().split()))
    secret = args.key_file.read_text().strip() if args.key_file else os.getenv("LUNA_API_KEY")
    if not secret:
        raise ValueError("Set LUNA_API_KEY or --key-file")
    config = ScreenConfig(captures=args.captures.resolve(), output=args.output.resolve(),
                          model=args.model, url=args.url, version=args.version,
                          prompt=prompt, force=args.force, min_objects=args.min_objects,
                          min_categories=args.min_categories, min_regions=args.min_regions,
                          inspection_rounds=args.inspection_rounds, views_per_round=args.views_per_round,
                          render_pod=args.render_pod)
    run_batch(uids, config, secret, sources, workers=args.workers, queue=args.queue)


if __name__ == "__main__":
    main()
