"""One critic-directed medium repair, preserving other variants and history."""
import json,os,shutil,traceback,time
from pathlib import Path
import run

PROMPT='''只修medium，按独立反馈修复。定位用中性外观/位置，目标或同款命名不能提示待答属性；计数可写类别。question须供观察者探索3D后回答，不能是复制/建模指令或题干已给所问答案；答案须回应问题。其他档不变；初始不可答，中等答案须明确遮挡；问题须唯一指代及明确方位参照，摆放自然且答案有图像支持。只看真实图回答，不推算答案。'''

def link_or_copy(src,dst):
    try:return os.link(src,dst)
    except OSError:return shutil.copy2(src,dst)

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.unlink(missing_ok=True) # Never overwrite a hardlinked prior artifact.
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2))

def repair(result_path,critic_path,out,remote=None,source=None):
    result_path=Path(result_path);critic_path=Path(critic_path);out=Path(out)
    if out.resolve()==result_path.parent.resolve():raise ValueError('repair requires fresh directory')
    state=json.loads((out/'state.json').read_text()) if (out/'state.json').exists() else {}
    if (out/'result.json').exists() and state.get('status')=='complete':raise ValueError('completed repair must not be overwritten')
    if out.exists() and any(out.iterdir()):
        archive=out.parent/(out.name+'.archive_'+str(time.time_ns()));archive.mkdir()
        for path in list(out.iterdir()):
            if path.name in ('plan_call','answer_call') and path.is_dir():
                shutil.copytree(path,archive/path.name,copy_function=shutil.copy2)
            else:shutil.move(str(path),str(archive/path.name))
    out.mkdir(parents=True,exist_ok=True);write_json(out/'state.json',{'status':'running'});run.set_current_run(out,out,'running')
    try:
        prior=json.loads(result_path.read_text());critic=json.loads(critic_path.read_text());judge=critic.get('judgment',critic)
        action=judge.get('repair_action');reason=judge.get('reason','')
        if action not in ('question','view','scene'):raise ValueError('critic repair_action must be question/view/scene')
        if prior.get('unsuitable') or judge.get('unsuitable') or judge.get('source_unsuitable') or critic.get('status') in ('unsuitable','source_unsuitable'):raise ValueError('independently unsuitable source must not be cloned')
        if prior.get('terminal') is not True:raise ValueError('prior result must be terminal')
        items=prior.get('items',[]);by_level={i['level']:i for i in items}
        if len(by_level)!=len(items) or 'medium' not in by_level or not set(by_level).issubset(('simple','medium','hard')):raise ValueError('requires unique legal levels including medium')
        old=by_level['medium'];context=[{k:i[k] for k in ('level','question','answer')} for i in items]
        dest=out/'round_0'/'built'/'medium';plan=None
        if action=='scene':
            source=Path(source or prior.get('source',''))
            if not source.is_file():raise ValueError('original source GLB missing')
            if remote is None:remote=run.Remote('scene-sol-medium-repair');remote.create()
            inspect=out/'inspect';run.ensure_inspect(remote,source,inspect)
            inventory=json.loads((inspect/'inventory.json').read_text());groups=json.loads((inspect/'groups.json').read_text())
            prompt=PROMPT+'\n重摆原生完整小物件，仅medium；copies用ids/support/at:[u,v]，u/v为支撑XZ bbox的0..1位置。只输出{"items":[{"level":"medium","target":"类别","copies":[{"ids":[0],"support":1,"at":[0.5,0.5]}],"question":"题目","start":"视图名","reveal":"视图名"}]}，不适合输出unsuitable。\n'+json.dumps({'questions':context,'groups':groups,'supports':run.support_candidates(inventory,groups),'views':run.VIEWS+['region_45','region_225']},ensure_ascii=False)
            prompt+='\n优先保留原场景重新选择目标和相机，此时copies=[]且start/reveal使用已有az视角；扫描网格或无完整可复制物件时不强行增强。只有确需增强且有完整小物件时才填copies。'
            if os.getenv('SCENEQA_NATIVE_ONLY')=='1':
                prompt+='\n本批次copies必须是[]，禁止修改原场景；只调整问题、目标或相机，不可行就unsuitable。'
            plan=run.call(out/'plan_call',prompt,[(v,inspect/(v+'.png')) for v in run.VIEWS]+[('native groups',inspect/'groups.png')],feedback=reason)
            run.validate_plan(plan,inventory,levels=('medium',))
            if plan.get('unsuitable'):raise ValueError('source unsuitable: '+plan['unsuitable'])
            write_json(out/'plan.json',plan);remote.run(source,out/'round_0'/'built',out/'plan.json');run.validate_build(out/'round_0'/'built',levels=('medium',));selected=plan['items'][0]
            medium={**old,'target':selected['target'],'question':selected['question'],'start_image':selected['start']+'.png','reveal_image':selected['reveal']+'.png','scene_mode':'augmented' if selected['copies'] else 'native'}
        else:
            native=Path(old['glb']).resolve().parent;shutil.copytree(native,dest,copy_function=link_or_copy);cameras=json.loads((dest/'cameras.json').read_text())
            images=[('medium '+name,dest/name) for name in dict.fromkeys([f'az_{az:03d}_overview.png' for az in (45,135,225,315)]+[old['start_image'],old['reveal_image'],'region_45.png','region_225.png']) if (dest/name).is_file()]
            schema='{"question":"问题"}' if action=='question' else '{"question":"问题","start":"camera名","reveal":"camera名"}'
            choice=run.call(out/'plan_call',PROMPT+'\n只输出'+schema+'；question操作不可换机位；view操作只能使用已有相机。\n'+json.dumps({'questions':context,'valid_views':list(cameras),'start':old['start_image'],'reveal':old['reveal_image']},ensure_ascii=False),images,feedback=reason)
            if not isinstance(choice.get('question'),str) or not choice['question'].strip():raise ValueError('repair question must be nonempty')
            medium={**old,'question':choice['question']}
            if action=='view':
                start=choice.get('start');reveal=choice.get('reveal')
                if start not in cameras or reveal not in cameras or start==reveal:raise ValueError('start/reveal must be distinct existing cameras')
                medium.update(start_image=start+'.png',reveal_image=reveal+'.png')
        cameras=json.loads((dest/'cameras.json').read_text());start=medium['start_image'].removesuffix('.png');reveal=medium['reveal_image'].removesuffix('.png')
        if start not in cameras or reveal not in cameras or start==reveal:raise ValueError('invalid repair cameras')
        medium['initial_camera']=cameras[start]
        if action=='question' and medium['initial_camera']!=old['initial_camera']:raise ValueError('question-only camera changed')
        evidence=[('medium '+role,dest/name) for role,name in [('start',medium['start_image']),('reveal',medium['reveal_image'])]]
        evidence += [('medium '+name,dest/name) for name in ('region_45.png','region_225.png') if (dest/name).exists()]
        response=run.call(out/'answer_call',PROMPT+'\n回答修订题并自查，只输出{"answer":"图中答案","pass":true或false,"reason":"起始/遮挡证据"}。\n'+json.dumps({'question':medium['question']},ensure_ascii=False),evidence,feedback=reason)
        run.validate_review(response);medium.update(answer=response['answer'],model_pass=response['pass'],reason=response['reason'],glb=str((dest/'scene.glb').resolve()))
        final_items=[]
        for old_item in items:
            if old_item['level']=='medium':item=medium
            else:
                item=dict(old_item);tier=out/'round_0'/'built'/item['level'];shutil.copytree(Path(item['glb']).resolve().parent,tier,copy_function=link_or_copy);item['glb']=str((tier/'scene.glb').resolve())
            write_json(Path(item['glb']).parent/'question.json',item);final_items.append(item)
        run.validate_build(out/'round_0'/'built',levels=tuple(by_level))
        final={**prior,'items':final_items,'iteration':0,'focus':'medium','pass':medium['model_pass'] is True,'terminal':True,'revision':{'action':action,'source_result':str(result_path.resolve()),'critic':str(critic_path.resolve()),'changed_levels':['medium']},'validation':'fresh medium visual self-review, awaiting independent audit'}
        write_json(out/'result.json',final);write_json(out/'terminal.json',{'terminal':True});write_json(out/'state.json',{'status':'complete'});run.set_current_run(out,out,'complete');return out/'result.json'
    except Exception:
        write_json(out/'state.json',{'status':'error'});(out/'error.txt').write_text(traceback.format_exc());run.set_current_run(out,out,'error');raise
