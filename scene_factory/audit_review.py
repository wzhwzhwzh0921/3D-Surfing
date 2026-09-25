"""A local review page that exports the same fingerprinted human-label CSV."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
from pathlib import Path

from .calibration import FIELDS, REQUIRED_FIELDS, tier_evidence_hash
from .supervisor import LEVELS, VIEWS


def render_label_page(labels: Path, runs_root: Path, output: Path) -> dict:
    root = runs_root.resolve()
    with labels.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not REQUIRED_FIELDS <= set(reader.fieldnames or []):
            raise ValueError("Label CSV is missing required columns")
        rows = [{field: row.get(field, "") or "" for field in FIELDS} for row in reader]
    cards = []
    validations = {}
    for index, row in enumerate(rows):
        if row["level"] not in LEVELS:
            raise ValueError(f"Unknown level: {row['level']}")
        work = (root / row["work_rel"]).resolve()
        if not work.is_relative_to(root):
            raise ValueError(f"Work path escapes runs root: {row['work_rel']}")
        scene = work / "output" / row["level"] / "scene.glb"
        if work not in validations:
            validation = json.loads((work / "output/validation.json").read_text())
            validations[work] = {result["level"]: result for result in validation["results"]}
        current = validations[work].get(row["level"])
        if (not scene.is_file() or current is None or
                current["question"] != row["question"] or
                json.dumps(current["answer"]["value"], ensure_ascii=False) != row["model_answer_json"] or
                hashlib.sha256(scene.read_bytes()).hexdigest() != row["asset_sha256"] or
                tier_evidence_hash(work, row["level"]) != row["evidence_sha256"]):
            raise ValueError(f"Label row is stale; regenerate template: {row['work_rel']}/{row['level']}")
        start_html = '<p>没有起始截图</p>'
        other_media = []
        for view in VIEWS:
            path = work / "output" / row["level"] / f"{view}.png"
            if path.is_file():
                rel = html.escape(os.path.relpath(path, output.parent.resolve()), quote=True)
                figure = (f'<figure><img loading="lazy" src="{rel}" alt="{view}">'
                          f'<figcaption>{view}</figcaption></figure>')
                if view == "start":
                    start_html = figure
                else:
                    other_media.append(figure)
        scene_rel = html.escape(os.path.relpath(scene, output.parent.resolve()), quote=True)
        scene_html = (f'<model-viewer src="{scene_rel}" loading="lazy" camera-controls '
                      f'interaction-prompt="none" shadow-intensity="0.7"></model-viewer>'
                      if scene.is_file() else '<p>当前 GLB 不存在</p>')
        cards.append(f'''<article data-index="{index}">
<div class="card-head"><span>{index + 1}/{len(rows)}</span><b>{html.escape(row["scene_id"])}</b>
<span>{html.escape(row["level"])} · {html.escape(row["question_type"])}</span></div>
<h2>{html.escape(row["question"])}</h2>
<div class="first-look">{start_html}</div>
<label>只看起始图，是否已经能回答？<select class="human_start_leak"><option value="">先判断这一项</option>
<option value="yes">能直接回答</option><option value="no">不能直接回答</option>
<option value="unknown">看不清</option></select></label>
<details class="explore"><summary>打开 3D 和补充视角</summary>
<div class="visual">{scene_html}<div class="views">{"".join(other_media) or "暂无补充截图"}</div></div></details>
<div class="form"><label>题目质量<select class="human_verdict"><option value="">未标注</option>
<option value="pass">通过</option><option value="review">待定</option><option value="fail">失败</option></select></label>
<label>我独立看到的答案<input class="human_answer" placeholder="先自己作答；看不清可留空"></label>
<button type="button" class="reveal">查看生成器答案</button>
<p class="builder-answer" hidden></p>
<label>生成器答案是否正确<select class="human_answer_correct" disabled>
<option value="">未判断</option><option value="yes">正确</option><option value="no">错误</option>
<option value="unknown">无法判断</option></select></label>
<label>证据与原因<textarea class="human_note" rows="3" placeholder="写明对象、视角、遮挡或歧义"></textarea></label></div>
</article>''')
    payload = json.dumps({"fields": FIELDS, "rows": rows}, ensure_ascii=False).replace("<", "\\u003c")
    storage_key = "sceneqa-human-labels-" + hashlib.sha256(labels.read_bytes()).hexdigest()
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>3D 场景题人工审核</title>
<script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
<style>body{font:16px system-ui;background:#eff2f5;color:#172532;margin:0 auto;max-width:1460px;padding:24px}
h1{margin-bottom:6px}.toolbar{position:sticky;top:0;z-index:4;background:#eff2f5;padding:12px 0;
display:flex;gap:16px;align-items:center;border-bottom:1px solid #ccd}button{cursor:pointer;padding:9px 14px;border:0;
border-radius:8px;background:#174f88;color:white}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:18px;margin-top:18px}
article{background:white;border-radius:12px;padding:16px;box-shadow:0 2px 12px #0001}.card-head{display:flex;gap:12px;color:#486}
h2{font-size:1.14rem}.first-look{max-width:380px}.first-look img{aspect-ratio:1}
details.explore{margin-top:12px}summary{cursor:pointer;color:#174f88;font-weight:650;margin:10px 0}
.visual{display:grid;grid-template-columns:minmax(230px,1fr) minmax(230px,1fr);gap:12px}
model-viewer{width:100%;height:480px;background:#dde4e9}.views{display:grid;grid-template-columns:1fr 1fr;gap:6px}
figure{margin:0}img{width:100%;aspect-ratio:1;object-fit:contain;background:#dde4e9}
figcaption{text-align:center;font-size:.85rem;color:#567}.form{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
label{display:flex;flex-direction:column;gap:5px}.form label:last-child{grid-column:1/-1}
select,input,textarea{font:inherit;padding:8px;border:1px solid #aab;border-radius:6px}
.builder-answer{padding:8px;background:#f4f8fc;margin:0}@media(max-width:650px){main{grid-template-columns:1fr}.visual{grid-template-columns:1fr}}
</style><h1>3D 场景题人工审核</h1>
<p>先只看题目和起始图，记录能否直接回答；然后展开 3D 和补充视角。独立作答后再揭示生成器答案。</p>
<div class="toolbar"><strong id="progress"></strong><button id="download" type="button">导出标注 CSV</button>
<span>浏览器只在本机保存草稿；请导出 CSV 后再运行 audit-evaluate。</span></div>
<main>__CARDS__</main>
<script id="label-data" type="application/json">__PAYLOAD__</script>
<script>
const data=JSON.parse(document.querySelector('#label-data').textContent);
const storageKey=__KEY__;
let draft={}; try{draft=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch{}
function save(){try{localStorage.setItem(storageKey,JSON.stringify(draft))}catch{}}
function updateProgress(){let n=0;document.querySelectorAll('article').forEach((card)=>{
  if(card.querySelector('.human_verdict').value)n++});
  document.querySelector('#progress').textContent=`已标注 ${n}/${data.rows.length}`;}
document.querySelectorAll('article').forEach((card)=>{
  const i=Number(card.dataset.index),row=data.rows[i],key=row.work_rel+'/'+row.level;
  const values={...row,...(draft[key]||{})};
  for(const field of ['human_start_leak','human_verdict','human_answer','human_answer_correct','human_note'])
    card.querySelector('.'+field).value=values[field]||'';
  const explore=card.querySelector('.explore');
  explore.addEventListener('toggle',()=>{
    if(explore.open && !card.querySelector('.human_start_leak').value){
      explore.open=false;alert('请先记录起始视角能否直接回答。');}
  });
  const answer=card.querySelector('.builder-answer');
  const correctness=card.querySelector('.human_answer_correct');
  if(values.human_answer_correct){answer.hidden=false;answer.textContent='生成器答案：'+JSON.parse(row.model_answer_json);
    correctness.disabled=false;card.querySelector('.human_answer').readOnly=true;}
  card.querySelector('.reveal').addEventListener('click',()=>{
    if(!card.querySelector('.human_start_leak').value){
      alert('请先记录起始视角能否直接回答。');return;}
    if(!card.querySelector('.human_answer').value.trim() &&
       !['review','fail'].includes(card.querySelector('.human_verdict').value)){
      alert('请先独立作答，或先判断题目质量。');return;}
    answer.textContent='生成器答案：'+JSON.parse(row.model_answer_json);answer.hidden=false;
    correctness.disabled=false;card.querySelector('.human_answer').readOnly=true;});
  card.querySelectorAll('select,input,textarea').forEach((node)=>node.addEventListener('input',()=>{
    draft[key]={};for(const field of ['human_start_leak','human_verdict','human_answer','human_answer_correct','human_note'])
      draft[key][field]=card.querySelector('.'+field).value;
    save();updateProgress();}));
});updateProgress();
function csvCell(value){return '"'+String(value??'').replaceAll('"','""')+'"'}
document.querySelector('#download').addEventListener('click',()=>{
  const lines=[data.fields.map(csvCell).join(',')];
  for(const row of data.rows){const key=row.work_rel+'/'+row.level;
    const merged={...row,...(draft[key]||{})};lines.push(data.fields.map(f=>csvCell(merged[f])).join(','));}
  const blob=new Blob(['\\ufeff',lines.join('\\r\\n'),'\\r\\n'],{type:'text/csv;charset=utf-8'});
  const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='human-labels.csv';link.click();
  setTimeout(()=>URL.revokeObjectURL(link.href),1000);
});
</script></html>'''.replace('__CARDS__', '\n'.join(cards)).replace('__PAYLOAD__', payload).replace('__KEY__', json.dumps(storage_key))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page)
    return {"output": str(output), "questions": len(rows),
            "scenes": len({row["work_rel"] for row in rows})}
