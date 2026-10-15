"""Export a static comparison of original tasks and user-request extensions."""
import argparse
import html
import json
from pathlib import Path
import shutil


def export(run, site, slug="followup-20261014"):
    summary=json.loads((run/'summary.json').read_text())
    rows=summary['results']
    target=site/'codex-source'/slug
    target.mkdir(parents=True,exist_ok=True)
    esc=html.escape
    cards=[]
    tasks=[]
    mode_counts={mode:{status:sum(r['mode']==mode and r['status']==status for r in rows)
                      for status in ('keep','reject','failed')} for mode in ('bundle','chain')}
    for index,r in enumerate(rows,1):
        title='增加两个独立要求' if r['mode']=='bundle' else '追加两跳空间定位'
        status={'keep':'保留','reject':'剔除','failed':'处理失败'}[r['status']]
        key=r['uid']+'/'+r['mode']
        folder=target/'cases'/key
        folder.mkdir(parents=True,exist_ok=True)
        base='/'+slug+'/cases/'+key
        plan=r.get('plan',{})
        original=r.get('original',{}).get('question','')
        followup=plan.get('followup','未能生成可审核的追加需求')
        pictures=[]
        used={'initial'}
        for req in plan.get('requirements',[]):used.update(req['views'])
        for hop in plan.get('hops',[]):used.update(hop['views'])
        for answer in r.get('answers',{}).get('answers',[]):used.update(answer['evidence_views'])
        for hop in r.get('answers',{}).get('hop_answers',[]):used.update(hop['evidence_views'])
        for ordinal,v in enumerate(r.get('evidence',[]),1):
            if v['label'] not in used:continue
            name=v['label']+'.png'
            shutil.copy2(v['path'],folder/name)
            label=(f'图{ordinal} · 原始初始视角 · 保持不变' if v['label']=='initial'
                   else f'图{ordinal} · 证据视角 · '+v['label'])
            pictures.append(f'<figure><img loading="lazy" src="{base}/{name}" alt="{esc(label)}"><figcaption>{esc(label)}</figcaption></figure>')
        answers=[]
        questions={q['id']:q['question'] for q in plan.get('requirements',[])}
        for a in r.get('answers',{}).get('answers',[]):
            label={'original':'原题','extra_1':'新增要求 1','extra_2':'新增要求 2','final':'链条最终要求'}.get(a['id'],a['id'])
            answers.append('<div class="answer"><b>'+esc(label)+'</b><p>'+esc(questions.get(a['id'],''))+'</p><p>模型观察答案：'+esc(a['answer'])+'</p></div>')
        steps=[]
        hop_answers={h['id']:h for h in r.get('answers',{}).get('hop_answers',[])}
        for h in plan.get('hops',[]):
            steps.append('<li><b>'+esc(h['id'])+'</b> '+esc(h['instruction'])+'<br>模型观察：'+esc(hop_answers.get(h['id'],{}).get('selected','尚无可确认的选择'))+'</li>')
        audit=r.get('audit',{})
        (folder/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
        protocol_path=run/r['uid']/r['mode']/'protocol.json'
        if protocol_path.is_file():
            protocol=json.loads(protocol_path.read_text())
            for name,value in protocol['prompts'].items():
                (folder/(name+'-prompt.txt')).write_text(value)
        delivery=Path(r['delivery']) if r.get('delivery') else None
        if delivery:
            task=json.loads((delivery/'task.json').read_text());task.update(uid=r['uid'],name=r['name'],scene=None,
                                                                         original_glb_url=r['source'].get('download_url'),
                                                                         scene_sha256=r.get('scene_sha256'),
                                                                         preview_mode='k8s_images')
            task['required_answer_ids']=(['original']+[h['id'] for h in plan['hops']]+['final'] if r['mode']=='chain'
                                         else [q['id'] for q in task['requirements']])
            task['grading']='all_requested_answers_and_spatial_selections_correct'
            (folder/'task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2))
            shutil.copy2(delivery/'answers.json',folder/'answers.json')
            tasks.append(task)
        content='<div class="images">'+''.join(pictures)+'</div>'
        content+='<div class="dialogue"><p><b>用户原题</b><br>'+esc(original)+'</p><p><b>用户追加需求</b><br>'+esc(followup)+'</p></div>'
        content+='<div class="answers">'+''.join(answers)+'</div>'
        if steps:content+='<ol class="steps">'+''.join(steps)+'</ol>'
        content+='<p class="judgment"><b>独立审核：</b>'+esc(r.get('reason',''))+'</p>'
        if r.get('attempts'):
            content+='<details><summary>初版与返修记录</summary>'+''.join('<p>第 '+str(a['attempt']+1)+' 版：'+('通过' if a['accepted'] else '不通过')+' · '+esc(a['reason'])+'</p>' for a in r['attempts'])+'</details>'
        source=r.get('source',{})
        content+='<p class="source">素材：<a href="'+esc(source.get('source_url') or '',quote=True)+'">'+esc(source.get('author_display_name') or '原作者')+'</a> · '+esc(source.get('license_slug') or '')+' · <a href="'+base+'/audit.json">审核 JSON</a>'
        if delivery:content+=' · <a href="'+base+'/task.json">无答案任务 JSON</a> · <a href="'+base+'/answers.json">模型答案 JSON</a>'
        if protocol_path.is_file():content+=' · <a href="'+base+'/plan-prompt.txt">本样例使用的追加需求 Prompt</a>'
        content+='</p>'
        cards.append(f'<article class="{r["mode"]}" id="case-{index}"><header><span class="badge {r["status"]}">{status}</span><h2>{esc(r["name"])} · {title}</h2></header>{content}</article>')
    public_summary={k:summary[k] for k in ('status','total','counts','elapsed_seconds')}
    public_summary['mode_counts']=mode_counts
    public_summary['first_pass_kept']=sum(bool(r.get('attempts') and r['attempts'][0]['accepted']) for r in rows)
    public_summary['repaired_cases']=sum(len(r.get('attempts',[]))>1 for r in rows)
    (target/'summary.json').write_text(json.dumps(public_summary,ensure_ascii=False,indent=2))
    (target/'tasks.jsonl').write_text(''.join(json.dumps(t,ensure_ascii=False)+'\n' for t in tasks))
    prompts=Path(__file__).resolve().parents[1]/'scene_factory/prompts'
    first_protocol=run/rows[0]['uid']/rows[0]['mode']/'protocol.json' if rows else None
    actual_prompts=json.loads(first_protocol.read_text())['prompts'] if first_protocol and first_protocol.is_file() else {}
    for name in ('plan','answer','audit'):
        (target/f'{name}-prompt.txt').write_text(actual_prompts.get(name) or (prompts/f'followup_{name}_zh.txt').read_text())
    css='''*{box-sizing:border-box}body{font:16px/1.8 system-ui;margin:0;background:#edf2f7;color:#172b47}main{max-width:1200px;margin:auto;padding:25px 20px 60px}nav{display:flex;gap:22px;flex-wrap:wrap}a{color:#2455b6}h1{font-size:30px}h2{font-size:22px;margin:0}.intro{color:#52647b}.metrics,.answers{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.metric,article,.protocol{background:#fff;padding:22px;border-radius:12px;margin:18px 0}.metric h3{margin:0}.badge{display:inline-block;background:#eaf1ff;padding:3px 12px;border-radius:7px;margin-bottom:8px}.keep{color:#146542}.reject{color:#a94325}.failed{color:#815b12}.images{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin:20px 0}figure{margin:0}img{width:100%;display:block;border-radius:8px}figcaption,.source{font-size:14px;color:#5d6e84}.dialogue{background:#ecf3ff;padding:16px 20px;border-radius:9px}.answer{padding:14px 18px;background:#f5f7fa;border-radius:8px}.answer p{margin:7px 0}.judgment{background:#fff8e6;padding:12px 18px;border-radius:8px}pre{white-space:pre-wrap;font-size:14px;line-height:1.8}.steps{padding:18px 18px 18px 40px;background:#f1f6fb;border-radius:8px}@media(max-width:700px){.metrics,.images,.answers{grid-template-columns:1fr}h1{font-size:26px}article{padding:16px}}'''
    metrics=''.join('<div class="metric"><h3>'+title+'</h3><p>保留 '+str(mode_counts[mode]['keep'])+' · 剔除 '+str(mode_counts[mode]['reject'])+' · 处理失败 '+str(mode_counts[mode]['failed'])+'</p></div>' for mode,title in [('bundle','方案一：增加独立要求'),('chain','方案二：增加空间推理链')])
    progress=f"本批已处理 {len(rows)} 份 · 初版通过 {public_summary['first_pass_kept']} 份 · 返修 {public_summary['repaired_cases']} 份 · {'已结束' if summary['status']=='complete' else '仍在运行'}"
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>3D Surfing · 原题后的用户追加增难</title><style>{css}</style><main><nav><a href="/fresh-scenes-20261014/">原来 5 道题</a><a href="/deepseek/">DeepSeek 解题过程</a><a href="tasks.jsonl" download>下载保留的追加任务</a></nav><h1>保留原题，追加一条用户需求</h1><p class="intro">Codex CLI + gpt-6.1-sol · xhigh。本轮在同一原景、同一原题、同一初始视角上对照两种增难方式。没有修改场景或换掉原题；新增需求不透露旧答案。</p><p>{progress}</p><div class="metrics">{metrics}</div><section class="protocol"><h2>怎样判断增难有效</h2><p>方案一要求原题加两个独立要求全部正确；新增目标来自不同内容区域。方案二以原题找到的目标为参照，依次进行两次空间选择，再观察隐藏属性；审核会检查每一跳有没有真实候选、能不能被删掉。</p><p>这里展示模型答案与独立上下文审核，剔除样例也保留用于说明问题。尚未用盲解 agent 测量实际难度或操作次数；多个子问题增加完成工作量，不保证每个子问题更难。模型审核不是人工 gold。全部图片来自K8s，本页不启动3D渲染。</p><p><a href="plan-prompt.txt">本批追加需求 Prompt</a> · <a href="answer-prompt.txt">作答 Prompt</a> · <a href="audit-prompt.txt">审核 Prompt</a>。每个样例另附实际使用的 Prompt 快照。</p></section>{''.join(cards)}</main></html>'''
    (target/'index.html').write_text(page)
    shutil.copytree(target,site/'dist'/slug,dirs_exist_ok=True)
    return {'route':'/'+slug+'/','cases':len(rows),'kept_tasks':len(tasks),'mode_counts':mode_counts}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--slug',default='followup-20261014')
    args=parser.parse_args()
    print(json.dumps(export(args.run,args.site,args.slug),ensure_ascii=False))
