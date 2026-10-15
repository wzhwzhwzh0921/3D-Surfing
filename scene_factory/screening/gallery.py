"""Static screening gallery: reads evidence, never renders 3D geometry."""
import html
import json
import os
from pathlib import Path

from .policy import VIEWS


def gallery(summary: dict, output: Path, captures: Path, sources: dict) -> None:
    names = {uid: row.get("name", "") for uid, row in sources.items()}
    # Retain the original optional legacy title index.
    registry = Path(__file__).resolve().parents[2] / "tools/registry.jsonl"
    if registry.exists():
        names = {row["uid"]: row.get("name", "")
                 for line in registry.read_text().splitlines() for row in [json.loads(line)]}
    rows = ['''<!doctype html><meta charset="utf-8"><title>场景机器筛选</title>
<style>body{font-family:system-ui;margin:24px;background:#f3f5f7;color:#182533}
.scene{background:white;padding:18px;margin:20px 0;border-radius:12px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
img{width:100%;background:#eee}.keep{color:#137647}.reject{color:#ac3434}
small{word-break:break-all}figure{margin:0}figcaption{font-size:12px}</style>
<h1>第一步：场景机器筛选</h1><p>仅根据8张既有K8s图判断场景探索潜力；
尚未人工验收，不等于正式接纳。不出题、不做增强。</p>''']
    if summary.get("version") == "v4":
        rows[-1] = rows[-1].replace("仅根据8张既有K8s图判断场景探索潜力；",
            "先看8张既有K8s图，拟保留场景必须请求局部真实3D换焦/旋转图。数量是模型估计，不是GLB精确统计；")
    counts = {key: value for key, value in summary.items() if key != "results"}
    rows.append("<p>" + html.escape(json.dumps(counts, ensure_ascii=False)) + "</p>")
    for record in summary["results"]:
        judgment = record.get("judgment", {})
        decision = judgment.get("decision", "error")
        label = {"keep": "机器保留", "reject": "机器删除"}.get(decision, "调用失败")
        uid = record["uid"]
        reason = judgment.get("reason", str(record.get("errors")))
        previous = record.get("source", {}).get("previous_judgment")
        rows.append(
            f'<article class="scene"><h2 class="{decision}">{label} · '
            f'{html.escape(names.get(uid, ""))}</h2><small>{html.escape(uid)}</small>'
            f'<p>{html.escape(reason)}</p>')
        if previous:
            rows.append('<p>旧规则：' + html.escape(str(previous.get("decision"))) + ' · ' +
                        html.escape(str(previous.get("reason", ""))) + '</p>')
        if "object_count_lower_bound" in judgment:
            rows.append('<p><strong>模型保守估计：</strong>' + str(judgment["object_count_lower_bound"]) +
                        ' 个完整物件 / ' + str(len(judgment["categories"])) + ' 类 / ' +
                        str(len(judgment["regions"])) + ' 个内容区域</p>')
            rows.append('<ul>')
            for item in judgment["categories"]:
                rows.append('<li>' + html.escape(item["name"]) + ' ≥ ' + str(item["count_lower_bound"]) +
                            '；证据：' + html.escape(', '.join(item["views"])) + '</li>')
            rows.append('</ul><ul>')
            for item in judgment["regions"]:
                rows.append('<li>' + html.escape(item["name"] + '：' + '、'.join(item["contents"])) + '</li>')
            rows.append('</ul>')
        rows.append('<div class="grid">')
        for view in VIEWS:
            source = html.escape(os.path.relpath(captures / uid / f"{view}.png", output), quote=True)
            rows.append(f'<figure><a href="{source}"><img loading="lazy" src="{source}"></a>'
                        f'<figcaption>{view}</figcaption></figure>')
        rows.append("</div>")
        if record.get("inspection_views"):
            rows.append('<h3>模型主动取证：真实3D换焦、旋转和缩放</h3><div class="grid">')
            for item in record["inspection_views"]:
                image = html.escape(os.path.relpath(item["path"], output), quote=True)
                request = item["request"]
                caption = item["label"] + ' · ' + request['purpose'] + ' · yaw=' + str(request['yaw']) + \
                          '° / pitch=' + str(request['pitch']) + '° / zoom=' + str(request['zoom'])
                rows.append(f'<figure><a href="{image}"><img loading="lazy" src="{image}"></a>'
                            f'<figcaption>{html.escape(caption)}</figcaption></figure>')
            rows.append('</div>')
        rows.append("</article>")
    (output / "gallery.html").write_text("\n".join(rows), encoding="utf-8")
