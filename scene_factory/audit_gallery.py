"""Local, read-only overview of source scenes selected for calibration."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
from collections import Counter
from urllib.parse import urlparse

from .suitability import screen


def render_audit_gallery(catalog: Path, runs_root: Path, output: Path) -> dict:
    rows = [json.loads(line) for line in catalog.read_text().splitlines() if line.strip()]
    output.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    prepared = 0
    screening = Counter()
    for row in rows:
        work = runs_root / row["uid"]
        status_path = work / "status.json"
        status = json.loads(status_path.read_text()).get("status", "unknown") if status_path.exists() else "not_started"
        inventory_path = work / "inspect/inventory.json"
        if inventory_path.is_file():
            verdict = screen(json.loads(inventory_path.read_text()))
            screening_label = ("geometry_gate" if verdict["definitive"] else
                               "agent_visual_review" if verdict["status"] == "likely_unsuitable"
                               else "needs_agent_review")
            screening[screening_label] += 1
        else:
            screening_label = "not_screened"
        pictures = []
        for view in ("front", "back", "side", "top"):
            path = work / "inspect" / f"source_{view}.png"
            if path.is_file():
                relative = os.path.relpath(path.resolve(), output.parent.resolve())
                pictures.append(f'<figure><img src="{html.escape(relative, quote=True)}" '
                                f'alt="{html.escape(view, quote=True)}"><figcaption>{view}</figcaption></figure>')
        if len(pictures) == 4:
            prepared += 1
        raw_url = str(row.get("source_url", ""))
        safe_url = html.escape(raw_url, quote=True) if urlparse(raw_url).scheme in ("http", "https") else "#"
        cards.append(f'<article><h2>{html.escape(str(row.get("name", row["uid"])))}</h2>'
                     f'<p>{html.escape(row["tier"])} · {html.escape(row.get("audit_environment", "unclassified"))}'
                     f' · {html.escape(row.get("category", ""))} · {row["bytes"] / 1e6:.1f} MB'
                     f' · {html.escape(status)} · {screening_label}</p>'
                     f'<div class="views">{"".join(pictures) or "尚无预览"}</div>'
                     f'<p><a href="{safe_url}">原始来源</a> · {html.escape(row["uid"])}</p></article>')
    page = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>场景审核抽样</title>
<style>body{font:16px system-ui;background:#eef1f5;color:#18222d;margin:2rem}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:1rem}
article{background:white;padding:1rem;border-radius:12px;box-shadow:0 2px 12px #0001}
h2{font-size:1.1rem}.views{display:grid;grid-template-columns:1fr 1fr;gap:.4rem}
figure{margin:0}img{width:100%;aspect-ratio:1;object-fit:contain;background:#ddd}
figcaption{text-align:center;color:#566}</style>
<h1>场景审核抽样</h1><p>此页只展示原始资产预览；可下载不等于适合出题，尚需人工筛选。</p>
<p>geometry_gate：缺少两个可独立复制的小网格；agent_visual_review：仅命名稀疏，交给模型看图；needs_agent_review：结构初筛未发现明确障碍。</p>
<main>""" + "\n".join(cards) + "</main></html>"
    output.write_text(page)
    return {"output": str(output), "scenes": len(rows), "previews_complete": prepared,
            "screening": dict(screening)}
