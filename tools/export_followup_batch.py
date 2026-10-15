"""Publish an image-only, paginated snapshot of reviewed followup tasks."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
from urllib.parse import urlparse

from PIL import Image

ESC = html.escape
MODE = {"bundle": "追加两个独立问题", "chain": "两跳空间定位"}
ENV = {"indoor": "室内", "outdoor": "室外"}
TYPES = {"color": "颜色", "count": "数量", "identity": "物件识别", "shape": "形状",
         "material": "材质", "text": "文字图案", "spatial": "空间关系",
         "association": "关联", "state": "姿态状态", "comparison": "比较"}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def safe_link(value):
    return value if isinstance(value, str) and urlparse(value).scheme in ("http", "https") else None


class Assets:
    def __init__(self, target, base):
        self.target, self.base = target, base
        self.paths = {}
        self.hashes = set()
        (target / "images").mkdir(exist_ok=True)

    def add(self, source):
        source = str(source)
        if source in self.paths:
            return self.paths[source]
        path = Path(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        name = digest + ".webp"
        destination = self.target / "images" / name
        if not destination.exists():
            # Full dimensions, no cropping. High quality previews keep the public
            # archive small; original GLBs remain available through source links.
            with Image.open(path) as im:
                im.convert("RGBA").save(destination, format="WEBP", quality=95, method=4)
        self.hashes.add(digest)
        self.paths[source] = self.base + "/images/" + name
        return self.paths[source]


def export(run, site, slug="followup-200"):
    run, site = run.resolve(), site.resolve()
    target = site / "codex-source" / slug
    target.mkdir(parents=True, exist_ok=True)
    base = "/" + slug
    progress = json.loads((run / "summary.json").read_text())
    jobs = [json.loads(s) for s in (run / "keep.jsonl").read_text().splitlines() if s.strip()]
    assets = Assets(target, base)
    cards, public, prompts_seen = [], [], set()
    scene_names = {}
    for job in jobs:
        record = json.loads(Path(job["record"]).read_text())
        if record["status"] != "keep":
            raise ValueError("Only reviewed keep records may be exported")
        delivery = Path(job["delivery"])
        task = json.loads((delivery / "task.json").read_text())
        answers = json.loads((delivery / "answers.json").read_text())
        audit = json.loads((delivery / "audit.json").read_text())
        if not audit.get("pass") or not all(c["pass"] for c in audit["checks"]):
            raise ValueError("Delivery lacks a complete passing model review")
        selected_ids = {a["id"] for a in answers["answers"] + answers["hop_answers"]}
        if set(task["required_answer_ids"]) != selected_ids:
            raise ValueError("Incomplete answers/selections")
        key, mode, environment = job["task_id"], job["mode"], job["environment"]
        folder = target / "cases" / key
        folder.mkdir(parents=True, exist_ok=True)
        source = record.get("source", {})
        original_url = safe_link(source.get("source_url") or job.get("source_url"))
        glb_url = safe_link(source.get("download_url"))
        learner = {**task, "uid": job["uid"], "name": job["name"],
                   "download_url": glb_url, "source_url": original_url}
        dump(folder / "task.json", learner)
        dump(folder / "answers.json", answers)
        dump(folder / "audit.json", audit)
        # Publish exact prompt text without account, endpoint or absolute paths.
        protocol = json.loads((Path(job["record"]).parent / "protocol.json").read_text())
        prompt_blob = json.dumps(protocol["prompts"], ensure_ascii=False, sort_keys=True)
        prompt_id = hashlib.sha256(prompt_blob.encode()).hexdigest()[:20]
        if prompt_id not in prompts_seen:
            for name, prompt in protocol["prompts"].items():
                path = target / "prompts" / prompt_id / (name + ".txt")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(prompt)
            prompts_seen.add(prompt_id)
        used = {"initial"}
        for req in record["plan"]["requirements"]:used.update(req["views"])
        for hop in record["plan"]["hops"]:used.update(hop["views"])
        for answer in answers["answers"] + answers["hop_answers"]:used.update(answer["evidence_views"])
        pictures = []
        for ordinal, view in enumerate(record["evidence"], 1):
            if view["label"] in used:
                pictures.append({"label": view["label"], "ordinal": ordinal,
                    "url": assets.add(view["path"]),
                    "caption": f"图{ordinal} · " + ("初始视角" if view["label"] == "initial" else view["label"])})
        initial, = [v for v in pictures if v["label"] == "initial"]
        reveals = [v for v in pictures if v["label"] != "initial"]
        if not reveals:
            raise ValueError("Kept question has no actual revealing evidence")
        a_by_id = {a["id"]: a for a in answers["answers"] + answers["hop_answers"]}
        req_by_id = {q["id"]: q for q in record["plan"]["requirements"]}
        hop_by_id = {h["id"]: h for h in record["plan"]["hops"]}
        final_id = "final" if mode == "chain" else "extra_2"
        best_labels = a_by_id[final_id]["evidence_views"]
        reveal = next((v for v in reveals if v["label"] in best_labels), reveals[0])
        repaired = len(record.get("attempts", [])) > 1
        question_types = sorted({TYPES.get(q["type"], q["type"]) for q in record["plan"]["requirements"]})
        scene_names[job["uid"]] = job["name"]
        details = []
        titles = {"original": "原题", "extra_1": "追加问题一", "extra_2": "追加问题二",
                  "h1": "定位第一步", "h2": "定位第二步", "final": "最终观察"}
        for answer_id in task["required_answer_ids"]:
            specification = (req_by_id[answer_id]["question"] if answer_id in req_by_id
                             else hop_by_id[answer_id]["instruction"])
            value = a_by_id[answer_id].get("answer") or a_by_id[answer_id]["selected"]
            refs = "、".join("图" + str(v["ordinal"]) for v in pictures if v["label"] in a_by_id[answer_id]["evidence_views"])
            details.append(f'<div class="qa"><span class="q-label">{titles[answer_id]}</span><p>{ESC(specification)}</p><div class="model-answer"><span>模型答案</span><p>{ESC(value)}</p><small>证据：{ESC(refs)}</small></div></div>')
        thumbs = ''.join(f'<button class="thumb" type="button" data-view="{ESC(v["url"], quote=True)}" data-caption="{ESC(v["caption"], quote=True)}" aria-label="查看{ESC(v["caption"], quote=True)}"><img data-src="{v["url"]}" alt=""><span>{ESC(v["caption"])}</span></button>' for v in reveals)
        footer = [f'<a href="{base}/cases/{key}/task.json" target="_blank" rel="noopener">任务 JSON</a>',
                  f'<a href="{base}/cases/{key}/answers.json" target="_blank" rel="noopener">答案 JSON</a>',
                  f'<a href="{base}/cases/{key}/audit.json" target="_blank" rel="noopener">完整审核</a>',
                  f'<a href="{base}/prompts/{prompt_id}/plan.txt" target="_blank" rel="noopener">出题 Prompt</a>',
                  f'<a href="?scene={job["uid"]}">同场景题目</a>']
        if original_url: footer.append(f'<a href="{ESC(original_url, quote=True)}" target="_blank" rel="noopener">素材来源 ↗</a>')
        if glb_url: footer.append(f'<a href="{ESC(glb_url, quote=True)}" target="_blank" rel="noopener">下载原始 GLB ↗</a>')
        cards.append(f'''<article class="case" id="{key}" data-mode="{mode}" data-environment="{environment}" data-uid="{job['uid']}" data-repaired="{str(repaired).lower()}" hidden>
<header class="case-header"><div><div class="tags"><span class="tag {mode}">{MODE[mode]}</span><span class="tag">{ENV[environment]}</span><span class="pass">✓ 模型审核通过{' · 返修后保留' if repaired else ''}</span></div><h2>{ESC(job['name'])}</h2><p class="types">{' · '.join(question_types)}</p></div><a class="permalink" href="#{key}" aria-label="此题链接">↗</a></header>
<div class="pictures"><figure><div class="image-title"><b>解题起点</b><span>原始初始视角，保持不变</span></div><img class="large-image" data-src="{initial['url']}" alt="{ESC(job['name'], quote=True)}的初始视角"><figcaption>{ESC(initial['caption'])}</figcaption></figure><figure><div class="image-title"><b>探索后的证据</b><span>下方可切换真实视角</span></div><img class="large-image reveal" data-src="{reveal['url']}" alt="{ESC(job['name'], quote=True)}的答案证据"><figcaption class="reveal-caption">{ESC(reveal['caption'])}</figcaption></figure></div>
<div class="thumbs" aria-label="切换证据视角">{thumbs}</div>
<div class="conversation"><div class="speaker">USER · 原题</div><p>{ESC(task['original_question'])}</p><div class="speaker additional">USER · 追加需求</div><p>{ESC(task['user_followup'])}</p></div>
<div class="answer-grid">{''.join(details)}</div>
<div class="review"><b>独立上下文审核</b><p>{ESC(audit['reason'])}</p></div>
<footer><div class="links">{' '.join(footer)}</div><p>素材作者：{ESC(str(job.get('author') or source.get('author_display_name') or '未标注'))} · 许可：{ESC(str(job.get('license_slug') or '见来源页'))} · 每项均须正确</p></footer></article>''')
        public.append({"task_id": key, "uid": job["uid"], "name": job["name"],
            "mode": mode, "environment": environment, "repaired": repaired,
            "task": learner, "answers": answers, "audit": audit, "images": pictures,
            "author": job.get("author"), "license_slug": job.get("license_slug"),
            "source_url": original_url, "prompt_id": prompt_id, "gold": False})
    counts = Counter(r["mode"] for r in public)
    environments = Counter(r["environment"] for r in public)
    now = datetime.now(timezone.utc)
    snapshot = {"exported_at": now.isoformat(), "target": progress["target"], "kept": len(public),
        "modes": dict(counts), "environments": dict(environments), "scenes": len(scene_names),
        "production_status_at_export": progress["status"], "quality_rejects": progress.get("counts", {}).get("reject", 0),
        "repaired_kept": sum(r["repaired"] for r in public), "model": progress["model"],
        "reasoning": progress["reasoning"], "render_location": progress.get("render_location", "unknown"),
        "model_backend": progress.get("model_backend", "codex"), "gold": False,
        "blind_solver_tested": False, "auto_updates": False}
    dump(target / "summary.json", snapshot)
    dump(target / "data.json", public)
    (target / "tasks.jsonl").write_text(''.join(json.dumps(r["task"], ensure_ascii=False) + '\n' for r in public))
    (target / "answers.jsonl").write_text(''.join(json.dumps({"task_id": r["task_id"], **r["answers"]}, ensure_ascii=False) + '\n' for r in public))
    css = (Path(__file__).with_name("followup-gallery.css")).read_text()
    js = (Path(__file__).with_name("followup-gallery.js")).read_text()
    (target / "gallery.css").write_text(css)
    (target / "gallery.js").write_text(js)
    # A static review page: images only, no 3D engine or automatic GLB requests.
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="3D Surfing 增难题库：原题、用户追加需求、多视角证据和模型答案。"><title>3D Surfing · 200题增难实验</title><link rel="stylesheet" href="gallery.css"><script src="gallery.js" defer></script></head><body>
<nav><a class="brand" href="/">3D <span>SURFING</span></a><div><a class="active" href="{base}/">200题增难实验</a><a href="/deepseek/">DeepSeek 解题回放</a><a href="/followup-20261014/">前期 8 题对照</a></div></nav>
<main><section class="hero"><div class="eyebrow">SCENE EXPLORATION / QUESTION SET</div><h1>看同一个世界，<br>多找几处答案。</h1><p class="lead">保留原题与初始视角，让用户追加需求。下面每一份都包含真实场景图片、完整题目、模型观察答案和审核依据。</p><div class="hero-meta"><span>{ESC(progress.get('model_backend', 'codex'))} · {ESC(progress['model'])} / {ESC(progress['reasoning'])}</span><span>图片渲染：{ESC(progress.get('render_location', '未记录'))}</span><span>原场景不增删物件</span></div></section>
<section class="metrics" aria-label="本次快照统计"><div><span>审核通过 / 总目标</span><strong>{len(public)}<small> / {progress['target']}</small></strong></div><div><span>追加两个独立问题</span><strong>{counts['bundle']}<small> / {progress['target']//2}</small></strong></div><div><span>两跳空间定位</span><strong>{counts['chain']}<small> / {progress['target']//2}</small></strong></div><div><span>不同场景</span><strong>{len(scene_names)}<small> 个</small></strong></div></section>
<section class="explanation"><div><b>01 · 多目标，全答对</b><p>原题加两个独立问题，观察不同区域；三项中任何一项错误，整题不通过。</p></div><div><b>02 · 找到它，再定位</b><p>从原题目标出发，连续进行两次空间选择，再观察最终属性；中间选择也需报告。</p></div></section>
<p class="snapshot">本页为 {now.strftime('%Y-%m-%d %H:%M')} UTC 导出快照 · 室外 {environments['outdoor']} 题 / 室内 {environments['indoor']} 题。生产队列继续运行，本页面不会自动更新。模型审核与作答为同一模型的独立上下文，未经人工标注；尚未盲测真实操作轮数。</p>
<section class="toolbar" aria-label="筛选题目"><label>增难方式<select id="mode"><option value="all">全部方式</option><option value="bundle">追加两个独立问题</option><option value="chain">两跳空间定位</option></select></label><label>场景<select id="environment"><option value="all">室内 + 室外</option><option value="outdoor">只看室外</option><option value="indoor">只看室内</option></select></label><label class="search">搜索场景或题目<input id="search" type="search" placeholder="例如：市场、杯子、颜色、Tavern"></label><label class="checkbox"><input id="answers" type="checkbox" checked>显示模型答案</label></section>
<div class="result-bar"><p id="result-count" role="status" aria-live="polite"></p><div class="downloads"><a href="tasks.jsonl" download>下载全部任务</a><a href="answers.jsonl" download>下载模型答案</a></div></div>
<section id="cases" aria-label="题目列表">{''.join(cards)}</section><p id="empty" hidden>没有匹配的题目。请调整筛选条件。</p><div class="pagination"><button id="previous" type="button">← 上一页</button><span id="page-number"></span><button id="next" type="button">下一页 →</button></div>
<footer class="site-footer">3D Surfing · 场景图片与问题均来自已保存的生产结果。每个素材保留作者、许可和原始下载链接。<a href="summary.json">快照统计</a> · <a href="data.json">完整展示数据</a></footer></main>
<dialog id="image-dialog" aria-label="放大查看证据"><button id="close-dialog" type="button" autofocus>关闭 ×</button><img id="dialog-image" alt="放大证据"><p id="dialog-caption"></p></dialog><noscript><p>请启用JavaScript以浏览分页题目；也可以<a href="data.json">下载完整题目和证据清单</a>。</p></noscript></body></html>'''
    (target / "index.html").write_text(page)
    shutil.copytree(target, site / "dist" / slug, dirs_exist_ok=True)
    return {**snapshot, "route": base + "/", "unique_images": len(assets.hashes),
            "public_bytes": sum(p.stat().st_size for p in target.rglob("*") if p.is_file())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--slug", default="followup-200")
    args = parser.parse_args()
    print(json.dumps(export(args.run, args.site, args.slug), ensure_ascii=False))
