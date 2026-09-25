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
from .audit_sample import write_sample
from .audit_gallery import render_audit_gallery
from .calibration import evaluate_labels, make_label_template
from .audit_review import render_label_page
from .runner import ROOT, blender, run_batch, run_one, supervise_existing
from .question_types import QUESTION_TYPES, type_order
from .suitability import screen
from .schema import read_candidate_pool, read_plan, select_candidates
from .supervisor import current_review
from .validate import validate


def catalog_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render_report(work: Path, output: Path) -> None:
    validation = json.loads((work / "output/validation.json").read_text())
    quality_path = work / "visual_quality.json"
    visual_quality = json.loads(quality_path.read_text()) if quality_path.exists() else {}
    visual_status = visual_quality.get("status", "needs_review")
    status_path = work / "status.json"
    delivery_status = (json.loads(status_path.read_text()).get("status")
                       if status_path.exists() else
                       {"rejected": "visual_rejected", "needs_review": "visual_needs_review"}.get(
                           visual_status, validation["status"]))
    quality_tiers = visual_quality.get("tiers", {})
    supervisor_review = current_review(work, validation) or {}
    captions = {"reveal": "全景揭示视角", "detail": "单目标局部视角",
                "answer_view": "按题型生成的同框证据"}
    cards = []
    for r in validation["results"]:
        level = r["level"]
        answer_label = ("模型观察答案" if r["answer"].get("source") == "model_visual"
                        else "程序计算答案" if r.get("answer_verified", True) else "待审核模型答案")
        img = os.path.relpath((work / "output" / level / "start.png").resolve(), output.parent.resolve())
        scene = os.path.relpath((work / "output" / level / "scene.glb").resolve(), output.parent.resolve())
        extra_images = "".join(
            f'<figure><img src="{html.escape(os.path.relpath((work / "output" / level / f"{name}.png").resolve(), output.parent.resolve()))}" alt="{name}"><figcaption>{captions[name]}</figcaption></figure>'
            for name in ("reveal", "detail", "answer_view") if (work / "output" / level / f"{name}.png").is_file())
        tier_quality = quality_tiers.get(level, {})
        quality_note = (f'<p><b>视觉自评：{html.escape(str(tier_quality.get("status", "未审核")))}</b> '
                        f'{html.escape(str(tier_quality.get("reason", "")))}</p>')
        supervisor_tier = supervisor_review.get("tiers", {}).get(level)
        if supervisor_tier:
            version = html.escape(str(supervisor_review.get("schema_version", "unknown")))
            quality_note += (f'<p><b>独立监督：{html.escape(supervisor_tier["verdict"])}'
                             f'（{version}）</b> '
                             f'{html.escape(supervisor_tier["reason"])}</p>')
            if "independent_answer" in supervisor_tier:
                quality_note += (f'<p>独立答案：{html.escape(str(supervisor_tier["independent_answer"]))}'
                                 f'；与生成器答案：{html.escape(str(supervisor_tier.get("answer_agreement", "unknown")))}</p>')
        cards.append(f'<article><h2>{html.escape(level)}</h2><model-viewer src="{html.escape(scene)}" camera-controls auto-rotate shadow-intensity="1" alt="{level} 3D scene"></model-viewer><img src="{html.escape(img)}" alt="起始视角">'
                     f'{extra_images}'
                     f'<p><b>题目：</b>{html.escape(r["question"])}</p>'
                     f'<p><b>{answer_label}：</b>{html.escape(str(r["answer"]["value"]))}</p>'
                     f'{quality_note}'
                     f'<p>起始视角完全隐藏副本 {len(r["hidden_copy_ids_at_start"])} 个，'
                     f'局部遮挡副本 {len(r.get("partly_obscured_copy_ids_at_start", []))} 个；'
                     f'全局或局部换焦可发现：{"是" if r["all_copies_discoverable"] else "否"}</p>'
                     f'<p><a href="{scene}">下载 GLB</a></p></article>')
    page = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>Scene QA</title>
<script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
<style>body{font:16px system-ui;max-width:1200px;margin:30px auto;background:#eef1f5;color:#16212c}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}article{background:white;padding:18px;border-radius:12px;box-shadow:0 2px 12px #0001}img,model-viewer{display:block;width:100%;aspect-ratio:1;object-fit:contain;background:#ddd}img{aspect-ratio:16/9}</style>
<h1>场景增强审查页</h1><p>交付状态：""" + html.escape(str(delivery_status)) + """；结构状态：""" + html.escape(validation["status"]) + """；模型视觉自评：""" + html.escape(str(visual_status)) + """。标记「模型观察答案」的题由模型看图作答，程序不核对答案；独立监督也不是人工 gold，实际操作数仍需盲测。</p><main>""" + "\n".join(cards) + "</main></html>"
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
    audit = sub.add_parser("audit-sample", help="Select a balanced pilot for human review")
    audit.add_argument("--catalog", type=Path, default=ROOT / "catalog/qualified.jsonl")
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--per-tier", type=int, default=10)
    audit.add_argument("--seed", type=int, default=0)
    audit.add_argument("--max-bytes", type=int, default=100_000_000)
    audit.add_argument("--exclude-catalog", type=Path, action="append", default=[],
                       help="Previously sampled JSONL; may be repeated for fresh audit waves")
    audit_gallery = sub.add_parser("audit-gallery", help="Show source previews for an audit sample")
    audit_gallery.add_argument("--catalog", type=Path, required=True)
    audit_gallery.add_argument("--runs-root", type=Path, required=True)
    audit_gallery.add_argument("--output", type=Path, required=True)
    audit_template = sub.add_parser("audit-template", help="Export fingerprinted human QA labels")
    audit_template.add_argument("--runs-root", type=Path, required=True)
    audit_template.add_argument("--output", type=Path, required=True)
    audit_template.add_argument("--force", action="store_true", help="Replace an existing label CSV")
    audit_evaluate = sub.add_parser("audit-evaluate", help="Compare supervisor to human labels")
    audit_evaluate.add_argument("--labels", type=Path, required=True)
    audit_evaluate.add_argument("--runs-root", type=Path, required=True)
    audit_evaluate.add_argument("--output", type=Path)
    audit_review = sub.add_parser("audit-review", help="Create a visual human-label page")
    audit_review.add_argument("--labels", type=Path, required=True)
    audit_review.add_argument("--runs-root", type=Path, required=True)
    audit_review.add_argument("--output", type=Path, required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--inventory", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--source", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    build = sub.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--plan", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    probe = sub.add_parser("probe", help="Score candidate camera views before full GLB export")
    probe.add_argument("--source", type=Path, required=True)
    probe.add_argument("--candidates", type=Path, required=True)
    probe.add_argument("--output", type=Path, required=True)
    select = sub.add_parser("select", help="Assemble a final plan from three probed candidates")
    select.add_argument("--candidates", type=Path, required=True)
    select.add_argument("--probe-report", type=Path, required=True)
    for level in ("simple", "medium", "hard"):
        select.add_argument(f"--{level}", required=True)
        select.add_argument(f"--{level}-view", type=int, choices=(1, 2), default=1)
    select.add_argument("--output", type=Path, required=True)
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
    run.add_argument("--supervisor-rounds", type=int, choices=(0, 1, 2), default=0,
                     help="Independent image review plus up to N targeted repairs (default: off)")
    run.add_argument("--supervisor-model", help="Optional different judge model on the same provider")
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
    batch.add_argument("--supervisor-rounds", type=int, choices=(0, 1, 2), default=0)
    batch.add_argument("--supervisor-model")
    supervisor = sub.add_parser("supervise", help="Review an existing built run; optionally repair it")
    supervisor.add_argument("--work", type=Path, required=True)
    supervisor.add_argument("--repairs", type=int, choices=(0, 1, 2), default=0)
    supervisor.add_argument("--provider", choices=("tokenhub", "deepseek"), default="tokenhub")
    supervisor.add_argument("--key-file", type=Path)
    supervisor.add_argument("--upstream")
    supervisor.add_argument("--model")
    supervisor.add_argument("--supervisor-model")
    supervisor.add_argument("--timeout", type=int, default=7200)
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
                              "ordered_candidates": [{"id": kind, "name": QUESTION_TYPES[kind]}
                                                     for kind in order]}, ensure_ascii=False, indent=2))
        elif a.command == "list":
            rows = catalog_rows(a.catalog)
            if a.tier:
                rows = [r for r in rows if r["tier"] == a.tier]
            for r in rows[:a.limit]:
                print(json.dumps(r, ensure_ascii=False))
        elif a.command == "audit-sample":
            print(json.dumps(write_sample(a.catalog, a.output, per_tier=a.per_tier,
                                          seed=a.seed, max_bytes=a.max_bytes,
                                          exclude_catalogs=a.exclude_catalog),
                             ensure_ascii=False, indent=2))
        elif a.command == "audit-gallery":
            print(json.dumps(render_audit_gallery(a.catalog, a.runs_root, a.output),
                             ensure_ascii=False, indent=2))
        elif a.command == "audit-template":
            print(json.dumps(make_label_template(a.runs_root, a.output, overwrite=a.force),
                             ensure_ascii=False, indent=2))
        elif a.command == "audit-evaluate":
            report = evaluate_labels(a.labels, a.runs_root)
            if a.output:
                a.output.parent.mkdir(parents=True, exist_ok=True)
                a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif a.command == "audit-review":
            print(json.dumps(render_label_page(a.labels, a.runs_root, a.output),
                             ensure_ascii=False, indent=2))
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
        elif a.command == "probe":
            read_candidate_pool(a.candidates)
            glb_header(a.source)
            blender("probe", a.source.resolve(), a.output.resolve(), a.candidates.resolve())
            print(a.output / "probe_report.json")
        elif a.command == "select":
            pool = read_candidate_pool(a.candidates)
            report = json.loads(a.probe_report.read_text())
            choices = {level: (getattr(a, level), getattr(a, f"{level}_view"))
                       for level in ("simple", "medium", "hard")}
            plan = select_candidates(pool, report, choices)
            a.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
            read_plan(a.output)
            print(a.output)
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
                             force=a.force, dry_run=a.dry_run, preflight=not a.no_preflight,
                             supervisor_rounds=a.supervisor_rounds,
                             supervisor_model=a.supervisor_model)
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
                               preflight=not a.no_preflight,
                               supervisor_rounds=a.supervisor_rounds,
                               supervisor_model=a.supervisor_model)
            tier_statuses = [tier.get("status") for item in result
                             for tier in ((item.get("visual_quality") or {}).get("tiers") or {}).values()]
            print(json.dumps({"total": len(result), "candidate": sum(x["status"] == "candidate" for x in result),
                              "needs_review": sum(x["status"] == "needs_review" for x in result),
                              "visual_needs_review": sum(x["status"] == "visual_needs_review" for x in result),
                              "visual_rejected": sum(x["status"] == "visual_rejected" for x in result),
                              "supervisor_needs_review": sum(x["status"] == "supervisor_needs_review" for x in result),
                              "supervisor_rejected": sum(x["status"] == "supervisor_rejected" for x in result),
                              "tier_pass": tier_statuses.count("pass"),
                              "tier_review": tier_statuses.count("review"),
                              "tier_fail": tier_statuses.count("fail"),
                              "unsuitable": sum(x["status"] == "unsuitable" for x in result),
                              "failed": sum(x["status"] == "failed" for x in result),
                              "results": result}, ensure_ascii=False, indent=2))
            if any(x["status"] == "failed" for x in result):
                sys.exit(1)
        elif a.command == "supervise":
            result = supervise_existing(a.work, provider=a.provider, key_file=a.key_file,
                                        model=a.model, supervisor_model=a.supervisor_model,
                                        upstream=a.upstream, max_repairs=a.repairs,
                                        timeout=a.timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] == "failed":
                sys.exit(1)
        elif a.command == "report":
            render_report(a.work, a.output)
            print(a.output)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
