"""One image-grounded question-only revision; never changes geometry or cameras."""
import argparse,json,os,shutil,hashlib
from pathlib import Path
import run

PROMPT='''依据成品图与独立审核反馈，仅修指定bad_levels的题目和答案。不能改变场景、相机或其他档题目。用图中唯一地标明确目标，用地标/场景方位说明左右参照；避免多张同款桌导致歧义。三题物件和题型仍不同；起始图不可直接答；中等须遮挡，困难须必要两跳。答案仅看图，不按计划猜。只输出JSON：{"items":[{"level":"档位","question":"修订问题","answer":"图中答案"}]}。'''

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--result',type=Path,required=True);ap.add_argument('--critic',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args()
    result=json.loads(a.result.read_text());critic=json.loads(a.critic.read_text());judgment=critic.get('judgment',critic);bad=judgment['bad_levels']
    if result.get('terminal') is not True or not result.get('items'):raise ValueError('requires complete terminal artifact set')
    if not bad or len(bad)!=len(set(bad)) or any(x not in ('simple','medium','hard') for x in bad):raise ValueError('invalid bad_levels')
    a.out.mkdir(parents=True,exist_ok=True);(a.out/'state.json').write_text(json.dumps({'status':'running'}));run.set_current_run(a.out,a.out,'running')
    payload={'questions':[{k:i[k] for k in ('level','question','answer')} for i in result['items']],'bad_levels':bad,'critic':judgment['reason']}
    images=[]
    for item in result['items']:
        tier=Path(item['glb']).resolve().parent
        if item['level'] in bad:
            names=[f'az_{az:03d}_overview.png' for az in (45,135,225,315)]+[item['reveal_image'],'region_45.png','region_225.png']
        else:names=[item['start_image'],item['reveal_image']]
        for name in dict.fromkeys(names):
            path=tier/name
            if not path.is_file():raise ValueError('missing image '+str(path))
            images.append((item['level']+' '+name,path))
    revised=run.call(a.out/'question_call',PROMPT+'\n'+json.dumps(payload,ensure_ascii=False,separators=(',',':')),images)
    rows=revised.get('items',[])
    levels=[i.get('level') for i in rows]
    if len(levels)!=len(set(levels)) or not set(bad).issubset(levels):raise ValueError('revision must include all bad_levels once')
    originals={i['level']:i for i in result['items']}
    for row in rows:
        if row['level'] not in bad:
            old=originals.get(row['level'])
            if old is None or row.get('question')!=old['question'] or str(row.get('answer'))!=str(old['answer']):raise ValueError('unrequested tier changed')
    rows=[row for row in rows if row['level'] in bad]
    for row in rows:
        if not isinstance(row.get('question'),str) or not row['question'].strip():raise ValueError('question must be nonempty text')
        if type(row.get('answer')) not in (str,int,float) or not str(row['answer']).strip():raise ValueError('answer must be text or number')
    changes={r['level']:r for r in rows};items=[]
    for old in result['items']:
        item=dict(old);dest=a.out/'round_0'/'built'/item['level'];source=Path(old['glb']).resolve().parent
        if not dest.exists():shutil.copytree(source,dest,copy_function=os.link)
        if item['level'] in changes:
            item.update(question=changes[item['level']]['question'],answer=str(changes[item['level']]['answer']))
            item['model_pass']=None;item['reason']='原自评不覆盖修订题目；等待新独立审核'
            item['revision_note']='question-only Sol revision; pending independent audit'
        item['glb']=str((dest/'scene.glb').resolve());qa=dest/'question.json';qa.unlink(missing_ok=True);qa.write_text(json.dumps(item,ensure_ascii=False,indent=2));items.append(item)
    final={**result,'pass':None,'items':items,'iteration':0,'revision':{'source_result':str(a.result.resolve()),'critic':str(a.critic.resolve()),'bad_levels':bad,'geometry_changed':False,'camera_changed':False},'validation':'question-only model revision; prior selfpass is not independent acceptance'}
    (a.out/'error.txt').unlink(missing_ok=True)
    (a.out/'result.json').write_text(json.dumps(final,ensure_ascii=False,indent=2));(a.out/'terminal.json').write_text(json.dumps({'revision_complete':True}));(a.out/'state.json').write_text(json.dumps({'status':'complete'}));run.set_current_run(a.out,a.out,'complete');print(json.dumps({'run_dir':str(a.out),'terminal':True,'revised_levels':bad}))
if __name__=='__main__':
    try:main()
    except Exception:
        import sys,traceback
        args=sys.argv;out=Path(args[args.index('--out')+1]) if '--out' in args else None
        if out:
            out.mkdir(parents=True,exist_ok=True);(out/'error.txt').write_text(traceback.format_exc());(out/'state.json').write_text(json.dumps({'status':'error'}));run.set_current_run(out,out,'error')
        raise
