"""Inspect real online source discovery without rendering or model calls."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .automation import rows, save, write_rows
from .sources import DEFAULT_PROVIDER, PROVIDERS, discover_batch, source_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=40, help="Total source rows, including this run's saved rows")
    parser.add_argument("--provider", choices=PROVIDERS,
                        default=os.getenv("SCENEQA_SOURCE_PROVIDER", DEFAULT_PROVIDER))
    parser.add_argument("--query", dest="queries", action="append", default=[])
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--exclude-history", type=Path, action="append", default=[])
    parser.add_argument("--max-bytes", type=int, default=64 * 1048576)
    parser.add_argument("--max-scan", type=int, default=100_000)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--page-size", type=int, default=60)
    args = parser.parse_args(argv)
    if min(args.limit, args.max_bytes, args.max_scan, args.max_pages) < 1 or not 1 <= args.page_size <= 100:
        parser.error("Positive budgets and page-size 1–100 required")
    if args.provider == "catalog" and args.catalog is None:
        parser.error("catalog provider requires --catalog")
    options = {key: getattr(args, key) for key in
               ("provider", "catalog", "queries", "max_bytes", "max_scan", "max_pages", "page_size")}
    excluded = set()
    exclusion_hashes = []
    for path in args.exclude_history:
        exclusion_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        excluded.update(str(row.get("uid") or row.get("scene_id")) for row in rows(path)
                        if row.get("uid") or row.get("scene_id"))
    config = dict(source=source_config(**options), exclusions=sorted(exclusion_hashes))
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".discovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = args.out / "discovery.json"
        state = json.loads(journal.read_text()) if journal.exists() else {
            "configuration": config, "rows": [], "cursor": None, "exhausted": False, "stats": {}}
        if state["configuration"] != config:
            raise ValueError("Discovery configuration changed; use a new output directory")
        if len(state["rows"]) < args.limit and not state["exhausted"]:
            result = discover_batch(args.out / "cache", args.limit - len(state["rows"]),
                state["cursor"], excluded_uids=excluded, **options)
            state.update(rows=state["rows"] + result["rows"], cursor=result["cursor"],
                         exhausted=result["exhausted"], stats=result["stats"])
            # Selection + cursor commit together. JSONL is a derived convenience file.
            save(journal, state)
        write_rows(args.out / "sources.jsonl", state["rows"])
        print(json.dumps({"source_count": len(state["rows"]), "exhausted": state["exhausted"],
            "stats": state["stats"], "sources": str(args.out / "sources.jsonl"),
            "note": "Discovered sources only; not downloaded or quality-approved."}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
