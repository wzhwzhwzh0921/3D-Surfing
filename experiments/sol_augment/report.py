"""Summarize the full test denominator and all raw API attempts, not gold accuracy."""
import argparse,json,html
from urllib.parse import quote
from pathlib import Path
from collections import Counter


def summarize(root,queue):
    rows=[];total=Counter();unknown=0;calls=0
    worker_logs=list(root.glob('worker_*.log'));finished=set()
    for log in worker_logs:
        for line in log.read_text().splitlines():
            try:
                record=json.loads(line)
                if 'scene' in record:finished.add(record['scene'])
            except ValueError:pass
    for line in queue.read_text().splitlines():
        if not line.strip():continue
        item=json.loads(line) if line.lstrip().startswith('{') else {'source_glb':line.strip()}
        uid=item.get('uid') or Path(item['source_glb']).stem;folder=root/uid
        pointer_status=None;pointer_error=None
        pointer=folder/'current_run.json'
        if pointer.exists():
            try:
                current=json.loads(pointer.read_text());chosen=(folder/current['run_dir']).resolve()
                if not chosen.is_relative_to(folder.resolve()):raise ValueError('current_run path escapes UID')
                if current.get('status') not in ('running','complete','error','unsuitable'):raise ValueError('invalid current_run status')
                folder=chosen;pointer_status=current['status']
            except (ValueError,KeyError,TypeError) as exc:pointer_status='error';pointer_error='invalid current_run: '+str(exc)
        result_path=folder/'result.json';error_path=folder/'error.txt'
        result=json.loads(result_path.read_text()) if result_path.exists() else {}
        status='error' if result.get('error') else 'unsuitable' if result.get('unsuitable') else 'self_pass' if result.get('pass') is True else 'pending_review' if result.get('revision') and result.get('pass') is None else 'self_fail' if result else 'error' if error_path.exists() else 'in_progress'
        if pointer_status:
            if pointer_status=='running':status='in_progress';result={}
            elif pointer_status=='error':status='error';result={}
            elif pointer_status=='unsuitable':status='unsuitable'
        elif worker_logs and uid not in finished:status='in_progress'
        elif not worker_logs and (folder/'state.json').exists() and json.loads((folder/'state.json').read_text()).get('status')=='running':status='in_progress'
        usage=Counter();attempts=[]
        for raw in sorted(list(folder.glob('round_*/*/response*.txt'))+list(folder.glob('question_call/response*.txt'))):
            calls+=1
            try:
                data=json.loads(raw.read_text());u=data.get('usage')
                if not u:unknown+=1
                else:
                    for key in ('prompt_tokens','completion_tokens','total_tokens'):usage[key]+=u.get(key,0);total[key]+=u.get(key,0)
                    cached=u.get('prompt_tokens_details',{}).get('cached_tokens',0);usage['upstream_cached_prompt_tokens']+=cached;total['upstream_cached_prompt_tokens']+=cached
                    reasoning=u.get('completion_tokens_details',{}).get('reasoning_tokens',0);usage['reasoning_tokens']+=reasoning;total['reasoning_tokens']+=reasoning
                attempts.append({'response':str(raw),'usage':u,'finish_reason':data.get('choices',[{}])[0].get('finish_reason') if data.get('choices') else None})
            except (ValueError,TypeError):unknown+=1;attempts.append({'response':str(raw),'usage':None})
        for error in sorted(folder.glob('round_*/*/error_*.txt')):
            suffix=error.name.removeprefix('error_').removesuffix('.txt')
            if not (error.parent/('response_'+suffix+'.txt')).exists():
                unknown+=1;attempts.append({'error':str(error),'usage':None})
        glbs=list(folder.glob('round_*/built/*/scene.glb'));sets=sum(all((round_dir/'built'/tier/'scene.glb').is_file() for tier in ('simple','medium','hard')) for round_dir in folder.glob('round_*') if round_dir.is_dir());rounds=sorted({p.parent.name for p in folder.glob('round_*/plan.json')})
        if pointer_status=='error':glbs=[];sets=0
        rows.append({'uid':uid,'run_dir':str(folder),'name':item.get('name'),'status':status,'reason':pointer_error or result.get('error') or result.get('unsuitable') or (error_path.read_text() if error_path.exists() else None),'rounds':rounds,'three_glb_sets':sets,'glbs':list(map(str,glbs)),'final_items':result.get('items',[]),'attempts':attempts,'usage':dict(usage)})
    return {'denominator':len(rows),'counts':dict(Counter(r['status'] for r in rows)),'scenes_with_three_glbs':sum(r['three_glb_sets']>0 for r in rows),'raw_api_responses':calls,'unknown_usage_attempts':unknown,'usage':dict(total),'quality_note':'self_pass only means model visual self-review; not independent gold accuracy','scenes':rows}


def gallery(root,report):
    labels={'self_pass':'模型自评通过','self_fail':'模型自评未通过','unsuitable':'不适合增难','in_progress':'运行中','error':'处理失败','pending_review':'修题后待审核'}
    tiers={'simple':'简单','medium':'中等','hard':'困难'}
    def esc(value):return html.escape(str(value or ''))
    def url(path):
        try:return quote(str(Path(path).resolve().relative_to(root.resolve())).replace('\\','/'),safe='/')
        except ValueError:return ''
    cards=[]
    for row in report['scenes']:
        items=[]
        for item in row['final_items']:
            folder=Path(item['glb']).parent;start=folder/item['start_image'];reveal=folder/item['reveal_image']
            items.append(f'<section><h3>{tiers.get(item["level"],item["level"])} · {esc(item.get("target"))}</h3><p class="question">{esc(item["question"])}</p><p>答案：{esc(item["answer"])}</p><div class="views"><figure><img loading="lazy" src="{url(start)}"><figcaption>起始 · {esc(item["start_image"])}</figcaption></figure><figure><img loading="lazy" src="{url(reveal)}"><figcaption>揭示 · {esc(item["reveal_image"])}</figcaption></figure></div><p class="note">自评：{"通过" if item["model_pass"] is True else "未通过"} — {esc(item["reason"])}</p><a href="{url(item["glb"])}" download>下载增强 GLB</a></section>')
        cards.append(f'<article><h2>{esc(row.get("name") or row["uid"])} <small>{labels[row["status"]]}</small></h2><div class="uid">{esc(row["uid"])}</div><p>{esc(row.get("reason"))}</p><div class="items">{"".join(items)}</div></article>')
    counts=' · '.join(f'{labels[k]} {v}' for k,v in report['counts'].items())
    document=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>20 场景增难实测</title><style>body{{font:15px system-ui;margin:0;background:#f5f5f4;color:#222}}header,article{{padding:20px;margin:16px;background:white;border-radius:10px}}header{{position:sticky;top:0;z-index:1}}h1{{margin:0 0 8px;font-size:23px}}h2{{font-size:19px;margin:0}}h3{{font-size:16px}}small,.note,.uid,figcaption{{color:#666}}small{{font-size:13px}}.items{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}}.views{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}figure{{margin:0}}img{{width:100%;display:block}}figcaption{{font-size:11px;overflow-wrap:anywhere}}.question{{font-weight:600}}.uid{{font-size:12px;margin-top:6px}}a{{color:#175dae}}@media(max-width:1000px){{.items{{grid-template-columns:1fr}}}}</style><header><h1>场景增难实测：{report['denominator']} 个场景</h1><div>{counts} · 已有三档 GLB 的场景 {report['scenes_with_three_glbs']}</div><small>答案与通过状态均为模型看图自评，尚非独立 gold 验收。运行中条目可能继续修订。</small></header>{''.join(cards)}</html>'''
    (root/'gallery.html').write_text(document)

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--queue',type=Path,required=True);a=p.parse_args()
    r=summarize(a.out,a.queue);gallery(a.out,r);(a.out/'summary.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in r.items() if k!='scenes'},ensure_ascii=False))
if __name__=='__main__':main()
