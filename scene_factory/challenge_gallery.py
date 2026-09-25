"""Visual triage of current blind-trial conflicts with supervisor reviews."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .blind_trace import discover_trace_paths, summarize_traces
from .supervisor import current_review


HINTS = {
    "supervisor_pass_blind_unsolved": "检查目标是否可通过换焦/旋转找到，以及题干地标是否足够清楚。",
    "supervisor_pass_blind_guessed": "检查揭示视角是否真的显示决定性特征，区分证据不足与盲解器失误。",
    "supervisor_pass_zero_action_answer": "优先检查浏览器初始视角是否泄露答案；再与 Blender 起始图比较。",
    "blind_unsolved_needs_review": "盲解未完成：检查目标可达性，也要排除模型导航失败。",
    "blind_guessed_needs_review": "盲解只给出猜测：检查证据是否足够清楚。",
    "blind_zero_action_answer": "检查浏览器初始视角是否已经泄露答案，以及是否只是盲解器猜测。",
    "blind_builder_answer_mismatch": "独立核对可见证据、题目歧义和生成器答案；不要直接采用任何一方。",
}


def render_challenge_gallery(traces: Path, runs_root: Path, output: Path) -> dict:
    root = runs_root.resolve()
    summary = summarize_traces(discover_trace_paths(traces), root)
    cards = []

    def media(path: Path, label: str) -> str:
        if not path.is_file():
            return ""
        rel = html.escape(os.path.relpath(path, output.parent.resolve()), quote=True)
        return (f'<figure><img loading="lazy" src="{rel}" alt="{html.escape(label)}">'
                f'<figcaption>{html.escape(label)}</figcaption></figure>')

    for item in summary["challenge_flags"]:
        work = (root / item["work_rel"]).resolve()
        if not work.is_relative_to(root):
            continue
        trace_path = Path(item["trace"])
        trace = json.loads(trace_path.read_text())
        validation = json.loads((work / "output/validation.json").read_text())
        result = next(row for row in validation["results"] if row["level"] == item["level"])
        level = item["level"]
        asset = work / "output" / level / "scene.glb"
        asset_rel = html.escape(os.path.relpath(asset, output.parent.resolve()), quote=True)
        start = media(work / "output" / level / "start.png", "Blender 起始图（正交）")
        evidence = "".join(media(work / "output" / level / f"{view}.png", f"Blender {view}")
                           for view in ("reveal", "detail", "answer_view"))
        frames = []
        for index in range(1, len(trace["actions"]) + 2):
            frame_id = f"frame_{index:04d}"
            frame = trace_path.parent / "observations" / f"{frame_id}.png"
            if frame.resolve().is_relative_to(trace_path.parent.resolve()):
                frame_html = media(frame, f"浏览器 {frame_id}")
                if frame_html:
                    frames.append(frame_html)
        actions = "".join(
            f'<li><code>{html.escape(row["tool"])}</code> '
            f'<code>{html.escape(json.dumps(row["args"], ensure_ascii=False))}</code> '
            f'{html.escape(row["before_observation_id"])} → '
            f'{html.escape(row["after_observation_id"])}</li>'
            for row in trace["actions"])
        review = current_review(work, validation) or {}
        tier_review = review.get("tiers", {}).get(level, {})
        hints = "".join(f"<li>{html.escape(HINTS[flag])}</li>" for flag in item["flags"])
        cards.append(f'''<article>
<h2>{html.escape(item["scene_id"])} · {html.escape(level)}</h2>
<p class="question">{html.escape(result["question"])}</p>
<p><b>冲突标记：</b>{html.escape(", ".join(item["flags"]))}</p>
<ul>{hints}</ul>
<div class="visual">{start}<model-viewer src="{asset_rel}" camera-controls
interaction-prompt="none" shadow-intensity="0.7"></model-viewer></div>
<p>盲解状态：{html.escape(trace["completion_status"])}；成功相机操作：{len(trace["actions"])}；
当前监督结论：{html.escape(str(item["supervisor_verdict"] or "无"))}</p>
<details><summary>查看浏览器动作与逐帧截图</summary><ol>{actions or "无成功相机操作"}</ol>
<div class="frames">{"".join(frames) or "无保存截图（手动导出的轨迹可能只有动作 JSON）"}</div></details>
<details><summary>查看 Blender 其他证据图</summary><div class="frames">{evidence}</div></details>
<details><summary>最后核对答案与监督理由</summary>
<p>盲解答案：{html.escape(str(trace.get("answer") or "未提交"))}</p>
<p>生成器答案：{html.escape(str(result["answer"]["value"]))}</p>
<p>监督理由：{html.escape(str(tier_review.get("reason") or "无有效报告"))}</p></details>
</article>''')
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>3D 盲解冲突复核</title>
<script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
<style>body{font:16px system-ui;max-width:1200px;margin:24px auto;padding:0 18px;background:#eef2f5;color:#172532}
article{background:white;padding:18px;margin:18px 0;border-radius:12px;box-shadow:0 2px 12px #0001}
h2{font-size:1.2rem}.question{font-size:1.1rem;font-weight:650}.visual{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.frames{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
figure{margin:0}img,model-viewer{width:100%;height:330px;object-fit:contain;background:#dde4e9}
.frames img{height:220px}figcaption{text-align:center;color:#526579}summary{cursor:pointer;padding:8px 0;font-weight:650}
code{white-space:pre-wrap}li{margin:6px 0}@media(max-width:700px){.visual{grid-template-columns:1fr}}
</style><h1>3D 盲解冲突复核</h1>
<p>标记只提示要检查什么，不判定谁答对。浏览器透视截图与 Blender 正交截图不可直接混算难度。</p>
__CARDS__</html>'''.replace("__CARDS__", "\n".join(cards) or "<p>当前没有有效的冲突轨迹。</p>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page)
    return {"output": str(output), "input_traces": summary["input_traces"],
            "valid_traces": summary["valid_traces"], "challenges": len(cards),
            "rejected": summary["rejected"]}
