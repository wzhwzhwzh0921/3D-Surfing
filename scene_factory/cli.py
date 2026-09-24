"""Command-line interface for reproducible URL-to-three-scene production."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sys

from .download import glb_header
from .runner import ROOT, blender, run_batch, run_one
from .question_types import QUESTION_TYPES, AUTO_VERIFIED, type_order
from .suitability import screen
from .schema import read_plan
from .validate import validate


def catalog_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render_report(work: Path, output: Path) -> None:
    validation = json.loads((work / "output/validation.json").read_text())
    cards = []
    for r in validation["results"]:
        level = r["level"]
        answer_label = "程序计算答案" if r.get("answer_verified", True) else "待审核模型答案"
        img = os.path.relpath((work / "output" / level / "start.png").resolve(), output.parent.resolve())
        scene = os.path.relpath((work / "output" / level / "scene.glb").resolve(), output.parent.resolve())
        cards.append(f'<article><h2>{html.escape(level)}</h2><model-viewer src="{html.escape(scene)}" camera-controls auto-rotate shadow-intensity="1" alt="{level} 3D scene"></model-viewer><img src="{html.escape(img)}" alt="起始视角">'
                     f'<p><b>题目：</b>{html.escape(r["question"])}</p>'
                     f'<p><b>{answer_label}：</b>{html.escape(str(r["answer"]["value"]))}</p>'
                     f'<p>起始视角隐藏副本 {len(r["hidden_copy_ids_at_start"])} 个；'
                     f'全局或局部换焦可发现：{"是" if r["all_copies_discoverable"] else "否"}；'
                     f'局部证据 {len(r.get("local_view_probes", []))} 个</p>'
                     f'<p><a href="{scene}">下载 GLB</a></p></article>')
    page = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>Scene QA</title>
<script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
<style>body{font:16px system-ui;max-width:1200px;margin:30px auto;background:#eef1f5;color:#16212c}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}article{background:white;padding:18px;border-radius:12px;box-shadow:0 2px 12px #0001}img,model-viewer{display:block;width:100%;aspect-ratio:1;object-fit:contain;background:#ddd}img{aspect-ratio:16/9}</style>
<h1>场景增强审查页</h1><p>状态：""" + html.escape(validation["status"]) + """。标记「待审核模型答案」的题尚未自动核答案；自然性与实际操作数仍需人工/盲测复核。</p><main>""" + "\n".join(cards) + "</main></html>"
    output.write_text(page)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sceneqa")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    types = sub.add_parser("types", help="Preview reproducible 10-type order for one scene")
    types.add_argument("--scene-id", required=True)
    types.add_argument("--seed", type=int, default=0)
    listing = sub.add_parser("list")
    listing.add_argument("--catalog", type=Path, default=ROOT / "catalog/qualified.jsonl")
    listing.add_argument("--tier", choices=["simple", "complex"])
    listing.add_argument("--limit", type=int, default=20)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--inventory", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--source", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    build = sub.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--plan", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--url")
    run.add_argument("--input-path", type=Path)
    run.add_argument("--uid")
    run.add_argument("--catalog", type=Path, default=ROOT / "catalog/qualified.jsonl")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--max-bytes", type=int, default=100_000_000)
    run.add_argument("--timeout", type=int, default=7200)
    run.add_argument("--upstream")
    run.add_argument("--model")
    run.add_argument("--provider", choices=["tokenhub", "deepseek"], default="tokenhub")
    run.add_argument("--key-file", type=Path)
    run.add_argument("--force", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-preflight", action="store_true")
    batch = sub.add_parser("batch")
    batch.add_argument("--catalog", type=Path, default=ROOT / "catalog/qualified.jsonl")
    batch.add_argument("--tier", choices=["simple", "complex"])
    batch.add_argument("--limit", type=int, default=20)
    batch.add_argument("--offset", type=int, default=0)
    batch.add_argument("--workers", type=int, default=1)
    batch.add_argument("--output-root", type=Path, required=True)
    batch.add_argument("--max-bytes", type=int, default=100_000_000)
    batch.add_argument("--timeout", type=int, default=7200)
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--no-preflight", action="store_true")
    batch.add_argument("--provider", choices=["tokenhub", "deepseek"], default="tokenhub")
    batch.add_argument("--key-file", type=Path)
    batch.add_argument("--upstream")
    batch.add_argument("--model")
    gallery = sub.add_parser("report")
    gallery.add_argument("--work", type=Path, required=True)
    gallery.add_argument("--output", type=Path, required=True)
    return p


def main() -> None:
    a = parser().parse_args()
    try:
        if a.command == "doctor":
            from .runner import blender_exe
            info = {"claude": shutil.which("claude"), "blender": blender_exe(),
                    "blender_exists": Path(blender_exe()).exists(),
                    "api_key_configured": bool(os.environ.get("TOKENHUB_API_KEY") or os.environ.get("TENCENT_ENDPOINT_API_KEY") or
                                               os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
                    "catalog_rows": len(catalog_rows(ROOT / "catalog/qualified.jsonl")),
                    "free_disk_bytes": shutil.disk_usage(Path.cwd()).free}
            print(json.dumps(info, ensure_ascii=False, indent=2))
        elif a.command == "types":
            order = type_order(a.scene_id, a.seed)
            print(json.dumps({"scene_id": a.scene_id, "seed": a.seed,
                              "preferred": order[:3],
                              "ordered_candidates": [{"id": kind, "name": QUESTION_TYPES[kind],
                                                      "auto_verified": kind in AUTO_VERIFIED}
                                                     for kind in order]}, ensure_ascii=False, indent=2))
        elif a.command == "list":
            rows = catalog_rows(a.catalog)
            if a.tier:
                rows = [r for r in rows if r["tier"] == a.tier]
            for r in rows[:a.limit]:
                print(json.dumps(r, ensure_ascii=False))
        elif a.command == "preflight":
            print(json.dumps(screen(json.loads(a.inventory.read_text())), ensure_ascii=False, indent=2))
        elif a.command == "inspect":
            glb_header(a.source)
            blender("inspect", a.source.resolve(), a.output.resolve())
            print(a.output / "inventory.json")
        elif a.command == "build":
            read_plan(a.plan)
            glb_header(a.source)
            blender("build", a.source.resolve(), a.output.resolve(), a.plan.resolve())
            print(a.output / "build_report.json")
        elif a.command == "verify":
            print(json.dumps(validate(a.plan, a.output), ensure_ascii=False, indent=2))
        elif a.command == "run":
            if sum(bool(x) for x in (a.uid, a.url, a.input_path)) != 1:
                raise ValueError("Choose exactly one of --uid, --url, --input-path")
            rows = catalog_rows(a.catalog)
            row = next((r for r in rows if r["uid"] == a.uid), None) if a.uid else None
            if a.uid and row is None:
                raise ValueError(f"UID absent from catalog: {a.uid}")
            if row is None:
                locator = a.url or str(a.input_path.resolve())
                row = {"uid": "adhoc-" + hashlib.sha256(locator.encode()).hexdigest()[:12],
                       "download_url": a.url, "source_url": a.url}
            result = run_one(row, a.output_root, source_path=a.input_path, upstream=a.upstream,
                             model=a.model, provider=a.provider, key_file=a.key_file,
                             max_bytes=a.max_bytes, timeout=a.timeout,
                             force=a.force, dry_run=a.dry_run, preflight=not a.no_preflight)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] == "failed":
                sys.exit(1)
        elif a.command == "batch":
            rows = catalog_rows(a.catalog)
            if a.tier:
                rows = [r for r in rows if r["tier"] == a.tier]
            rows = rows[a.offset:a.offset + a.limit]
            result = run_batch(rows, a.output_root, workers=a.workers, max_bytes=a.max_bytes,
                               timeout=a.timeout, dry_run=a.dry_run, provider=a.provider,
                               key_file=a.key_file, upstream=a.upstream, model=a.model,
                               preflight=not a.no_preflight)
            print(json.dumps({"total": len(result), "candidate": sum(x["status"] == "candidate" for x in result),
                              "needs_review": sum(x["status"] == "needs_review" for x in result),
                              "unsuitable": sum(x["status"] == "unsuitable" for x in result),
                              "failed": sum(x["status"] == "failed" for x in result),
                              "results": result}, ensure_ascii=False, indent=2))
            if any(x["status"] == "failed" for x in result):
                sys.exit(1)
        elif a.command == "report":
            render_report(a.work, a.output)
            print(a.output)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
