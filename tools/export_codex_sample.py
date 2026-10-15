"""Publishable five-question gallery from current reviewed campaign deliveries."""
import argparse
import html
import json
from pathlib import Path
import shutil


def export(campaign, site, *, slug="codex-five-20261014", count=5, images_only=False):
    summary = json.loads((campaign / "summary.json").read_text())
    selected = campaign / "selected.jsonl"
    rows = [json.loads(line) for line in (selected if selected.exists() else campaign / "keep.jsonl").read_text().splitlines() if line.strip()]
    if len(rows) < count:
        raise ValueError(f"Need {count} reviewed deliveries; have {len(rows)}")
    target = site / "dist" / slug
    target.mkdir(parents=True, exist_ok=True)
    review_file = campaign / "inspection_review.json"
    review = json.loads(review_file.read_text()) if review_file.exists() else {}
    cards, questions = [], []
    for number, row in enumerate(rows[:count], 1):
        source = Path(row["folder"])
        destination = target / "delivery" / row["uid"]
        destination.mkdir(parents=True, exist_ok=True)
        if not (source / "scene.glb").is_file():
            raise ValueError("Missing complete scene delivery: " + str(source))
        files = ("question.json", "start.png", "reveal.png", "audit.json")
        if not images_only:
            files = ("scene.glb",) + files
        for name in files:
            if not (source / name).is_file():
                raise ValueError("Incomplete delivery: " + str(source / name))
            shutil.copy2(source / name, destination / name)
        question = json.loads((destination / "question.json").read_text())
        provenance = {key: row[key] for key in ("uid", "name", "category", "author", "author_username", "author_display_name",
                     "source_url", "download_url", "url", "license", "license_slug", "bytes", "sha256") if key in row}
        provenance.update(delivery_sha256=row.get("delivery_sha256",{}),
                          environment=row.get("screen_assessment",{}).get("environment","unknown"),
                          discovery=row.get("discovery",{}))
        (destination / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2))
        assessment = row.get("screen_assessment", {})
        (destination / "screening.json").write_text(json.dumps(assessment, ensure_ascii=False, indent=2))
        esc = html.escape
        overview_cards = []
        for angle in (45, 135, 225, 315):
            filename = f"az_{angle:03d}_closer.png"
            inspection = Path(row.get("source_inspect", "")) / filename
            if inspection.is_file():
                shutil.copy2(inspection, destination / filename)
                overview_cards.append(f'<figure><img loading="lazy" src="/{slug}/delivery/{row["uid"]}/{filename}" alt="场景方向 {angle} 度"><figcaption>方向 {angle}° · 场景内容概览</figcaption></figure>')
        inspection_cards = []
        record_path = Path(row.get("screen_record", ""))
        if record_path.is_file():
            for inspection in sorted(record_path.parent.glob("inspection_*/inspect_*.png")):
                shutil.copy2(inspection, destination / inspection.name)
                inspection_cards.append(f'<figure><img loading="lazy" src="/{slug}/delivery/{row["uid"]}/{inspection.name}" alt="模型主动观察的局部视角"><figcaption>{inspection.stem} · K8s 真实换焦取景</figcaption></figure>')
        categories = assessment.get("categories", [])
        regions = assessment.get("regions", [])
        complexity = f'''<div class="complexity"><strong>内容复杂度（模型看图估计）</strong><p>完整物件至少 {assessment.get('object_count_lower_bound', '未知')} 件 · {len(categories)} 类内容 · {len(regions)} 个内容区域</p><p>类别：{esc('、'.join(item['name'] for item in categories))}</p><p>区域：{esc('；'.join(item['name'] for item in regions))}</p><p>{esc(row.get('screen_reason', ''))}</p></div>'''
        camera = question.get("initial_camera") or question.get("camera") or question.get("start_camera")
        if not isinstance(camera, dict) or not camera.get("target"):
            raise ValueError("Missing usable initial camera: " + row["uid"])
        base = f"/{slug}/delivery/{row['uid']}"
        record = {**question, "uid": row["uid"], "name": row.get("name", row["uid"]),
                  "initial_camera": camera, "scene_url": None if images_only else base + "/scene.glb",
                  "environment": assessment.get("environment", "unknown"),
                  "preview_mode": "k8s_images" if images_only else "interactive",
                  "start_url": base + "/start.png", "reveal_url": base + "/reveal.png"}
        questions.append(record)
        environment = {"indoor":"室内","outdoor":"室外","mixed":"室内外混合"}.get(record['environment'],row.get('category','场景问答'))
        primary = (f'<a class="primary" href="{base}/start.png">查看集群初始图</a><a href="{base}/reveal.png">查看揭示图</a>'
                   if images_only else f'<a class="primary" href="viewer.html?id={row["uid"]}">进入 3D 自由探索</a><a href="{base}/scene.glb" download>下载 GLB</a>')
        original = f'<a href="{esc(str(row.get("download_url", "")),quote=True)}">下载原始素材 GLB</a>' if images_only else ''
        author_label = row.get('author') or row.get('author_display_name') or row.get('author_username') or '原作者'
        license_label = row.get('license') or row.get('license_label') or row.get('license_slug') or '见来源页'
        caveat = review.get("scene_notes", {}).get(row["uid"], "")
        caveat_html = f'<p class="caveat"><strong>抽查备注：</strong>{esc(caveat)}</p>' if caveat else ''
        cards.append(f'''<article id="q{number}"><header><span class="number">{number:02d}</span><div><h2>{esc(record['name'])}</h2><p class="category">{esc(str(environment))} · 中等题 · 模型审核通过</p></div></header>
{complexity}{caveat_html}<div class="overview">{''.join(overview_cards)}</div><div class="views inspection">{''.join(inspection_cards)}</div>
<div class="views"><figure><img loading="lazy" src="{base}/start.png" alt="第{number}题初始视角"><figcaption>初始视角：从这里开始探索</figcaption></figure><figure><img loading="lazy" src="{base}/reveal.png" alt="第{number}题揭示视角"><figcaption>揭示视角：用于检查答案证据</figcaption></figure></div>
<p class="question"><strong>原题</strong><br>{esc(str(question['question']))}</p><p class="answer"><strong>模型答案：</strong>{esc(str(question['answer']))}</p>
<p class="reason">{esc(str(row.get('reason', '')))}</p><p class="credit">来源：<a href="{esc(str(row.get('source_url', '')), quote=True)}" target="_blank" rel="noopener">{esc(str(author_label))}</a> · 原素材许可：{esc(str(license_label))}</p><div class="links">{primary}{original}<a href="{base}/question.json">题目与初始相机</a><a href="{base}/audit.json">审核记录</a><a href="{base}/screening.json">筛选判断</a><a href="{base}/provenance.json">素材来源</a></div></article>''')
    (target / "questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=2))
    (target / "questions.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions))
    public_summary = {key: summary[key] for key in ("status", "target", "discovered", "counts", "batches", "level", "elapsed_seconds", "selected_environments", "minimum_environments") if key in summary}
    public_summary["inspection_review"] = review
    (target / "summary.json").write_text(json.dumps(public_summary, ensure_ascii=False, indent=2))
    css = '''*{box-sizing:border-box}body{margin:0;background:#eef2f7;color:#172435;font:16px/1.75 system-ui}main{max-width:1200px;margin:auto;padding:24px 20px 60px}nav{display:flex;gap:22px;flex-wrap:wrap}a{color:#1d4ed8}h1{font-size:32px;margin-bottom:8px}.intro,.category,.reason{color:#526174}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.summary span{background:#fff;padding:8px 14px;border-radius:8px}article{background:#fff;border-radius:14px;padding:24px;margin:24px 0;box-shadow:0 2px 12px #17243508}header{display:flex;gap:16px;align-items:flex-start}.number{color:#1d4ed8;font-weight:750;font-size:26px}h2{margin:0;font-size:22px}.category{margin:3px 0 20px;font-size:14px}.views{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:0}img{width:100%;display:block;background:#f5f5f5;border-radius:8px}figcaption{font-size:14px;color:#526174;margin-top:5px}.question{font-size:18px;background:#f1f5fb;border-radius:8px;padding:16px}.answer{color:#155e42}.links{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.primary{background:#1d4ed8;color:#fff;padding:9px 16px;border-radius:8px;text-decoration:none}@media(max-width:700px){.views{grid-template-columns:1fr}h1{font-size:26px}article{padding:16px}.links{gap:12px}}'''
    css += '.complexity{background:#f1f5fb;padding:14px 18px;border-radius:10px;margin-bottom:20px}.complexity p{margin:5px 0;font-size:14px}.overview{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}.credit{font-size:14px;color:#526174}@media(max-width:700px){.overview{grid-template-columns:1fr 1fr}}'
    css += '.inspection{margin-bottom:24px}.caveat{padding:12px 16px;background:#fff8e8;border-radius:8px;color:#6d5123;font-size:14px}.protocol{background:white;padding:20px;border-radius:12px}.protocol pre{white-space:pre-wrap;font-size:14px;line-height:1.8}'
    title = f"Codex 实测 · {len(questions)} 个已选场景"
    if images_only:
        title = f"在线重新采集 · {len(questions)} 个室内外探索场景"
    progress = f"目标 {summary.get('target', count)} 题 · 当前交付 {len(questions)} 题"
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}</style><main><nav><a href="/">历史代表题</a><a href="/deepseek/">DeepSeek 解题过程</a><a href="/workflow/">整体流程</a><a href="questions.jsonl" download>下载本批题目</a></nav><h1>{title}</h1><p class="intro">2026-10-14 · Codex CLI + gpt-6.1-sol · xhigh。在线下载素材，K8s 取景，模型筛选、原景出题或必要增强、看图作答与独立上下文审核。</p><p class="intro">这里优先展示空间紧凑、内容物丰富的场景。物件数量、类别与区域是模型看图的估计，方便比较内容丰富度。原题和答案直接显示在图片下方。进入 3D 后可旋转、平移、缩放与双击换焦；起始相机使用交付位姿。模型审核不是人工标注。</p><div class="summary"><span>{progress}</span><span>本批发现 {summary.get('discovered', 0)} 个素材</span><span>剔除 {summary.get('counts', {}).get('reject', 0)} 个</span></div>{''.join(cards)}</main></html>'''
    (target / "index.html").write_text(page)
    if images_only:
        page = page.replace('进入 3D 后可旋转、平移、缩放与双击换焦；起始相机使用交付位姿。',
                            '本页使用 K8s 渲染的静态图片，不启动浏览器3D渲染。完整场景交付保存在本批次目录；原始素材下载链接与交付文件分开标注。')
        page = page.replace('这里优先展示空间紧凑、内容物丰富的场景。',
                            '素材由本轮在线重新检索并下载，排除了历史素材 ID；最终按室内外配额交付。这里优先展示空间紧凑、内容物丰富的场景。')
        page = page.replace('整体流程</a>', '历史流程说明（旧版）</a>')
        rules = (Path(__file__).resolve().parents[1] / "scene_factory/screening/prompts/v5.txt").read_text()
        (target / "screening-prompt-v5.txt").write_text(rules)
        elapsed = summary.get("elapsed_seconds", 0) / 60
        environments = summary.get("selected_environments", {})
        caveat_text = review.get("overall_note", "")
        protocol = f'''<section class="protocol"><h2>这轮筛选如何改变</h2><p>新抓取线上元数据 → 用 Objaverse 官方路径索引确认下载地址 → K8s 八视角 → 模型主动换焦、旋转与缩放取证 → 原景出一道中等题 → 看图作答 → 独立上下文审核。</p><p>不再按“20 个物件 / 8 类内容 / 3 个区域”硬筛。保留要求是：至少 3 个不同目标、2 种题型、2 个内容不同的区域，并有局部观察证据。本轮交付室外 {environments.get('outdoor', 0)} 题、室内 {environments.get('indoor', 0)} 题；单工作任务耗时约 {elapsed:.1f} 分钟。</p><p class="caveat">{esc(caveat_text)}</p><details><summary>查看本轮筛选 Prompt 原文</summary><pre>{esc(rules)}</pre></details></section>'''
        page = page.replace("</main>", protocol + "</main>")
        (target / "index.html").write_text(page)
    else:
        viewer = (site / "dist/viewer.html").read_text().replace('href="/">20 道代表题', f'href="/{slug}/">本批已选场景')
        viewer = viewer.replace('src="/viewer.js"', f'src="/{slug}/viewer.js"')
        (target / "viewer.html").write_text(viewer)
        script = (site / "dist/viewer.js").read_text().replace("'./vendor/", "'/vendor/")
        script = script.replace("'/data/questions.json'", f"'/{slug}/questions.json'")
        (target / "viewer.js").write_text(script)
    # Keep the exact public inputs in Site source; dist itself is ignored.
    shutil.copytree(target, site / "codex-source" / slug, dirs_exist_ok=True)
    return {"directory": str(target), "questions": len(questions), "route": f"/{slug}/"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--slug", default="codex-five-20261014")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--images-only", action="store_true", help="publish only K8s-rendered images, avoiding browser 3D load")
    args = parser.parse_args()
    print(json.dumps(export(args.campaign, args.site, slug=args.slug, count=args.count, images_only=args.images_only), ensure_ascii=False))
