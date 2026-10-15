"""Resumable scene QA; model and rendering transports are configured separately."""
import argparse, base64, hashlib, json, os, re, subprocess, time, tempfile, struct, traceback
from pathlib import Path
from email.utils import parsedate_to_datetime
import requests
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scene_factory.rendering import create_render_worker, render_settings

HERE=Path(__file__).resolve().parent
WORKER_BYTES=(HERE/'worker.py').read_bytes()
WORKER_SHA256=hashlib.sha256(WORKER_BYTES).hexdigest()
ENDPOINT=os.getenv('LUNA_CHAT_URL','http://127.0.0.1:18773/v1/chat/completions')
MODEL=os.getenv('LUNA_MODEL','gpt-6.1-sol')
NAMESPACE=os.getenv('SCENEQA_NAMESPACE','default')
ROUNDS=int(os.getenv('SCENEQA_AUGMENT_ROUNDS','2'))
IMAGE=os.getenv('SCENEQA_RENDER_IMAGE','')
VIEWS=[f'az_{az:03d}_{zoom}' for zoom in ('overview','closer') for az in (45,135,225,315)]
PLAN_PROMPT='''看原景八图、物件组联系图和支撑bbox，自然复制可移动完整小物件，出简单/中等/困难三题。禁复制墙/壁炉等结构、碎片或奇怪摆放；食品放合理桌面。target只写类别不含颜色/数量/状态；三题物件类别与题型都不同，不可三题都计数。解题者只看增强场景，题里不能说“新增”或依赖原景对照；同款颜色泄露时改问具体位置/状态。起始图不能直接看出答案。中等须关键答案被明确遮挡，不只变小。困难先辨唯一地标，从至少两个候选目标/区域中圈定一个再查目标；删第一跳仍能定位则不通过。题型任选：数量/身份/颜色/图案文字/姿态状态/包含承载/相对位置/远近排序/尺寸比较/部件连接。copies用完整组ids和support；at=[u,v]是该支撑XZ范围内0到1的位置，默认中心[0.5,0.5]，程序落地；rotation_y可选度数，不缩放。立即输出JSON，不写答案或确认说明：{"items":[{"level":"simple|medium|hard","target":"物件类别","copies":[{"ids":[0],"support":1,"at":[0.5,0.5]}],"question":"问题","start":"视图名","reveal":"视图名或region_45/region_225"}]}。不适合则 {"unsuitable":"原因"}。'''
REVIEW_PROMPT='''联合看三档实际成品的start/reveal/region图，为每题回答并自查。三题物件类别与题型都不同，图中确认身份。起始不泄答案；摆放自然、完整且不悬浮；中等须具体遮挡；困难第一跳须从至少两个候选目标/区域圈定一个；删第一跳仍可定位须fail。答案只来自图，不按复制数推算。不要改相机/问题。只输出JSON：{"items":[{"level":"simple|medium|hard","answer":"答案","pass":true或false,"reason":"一句start/reveal证据；中等指遮挡，困难说明两跳"}]}。看不清、泄题或类别重复须fail。'''


def command(args,**kwargs):
    try:return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs).stdout
    except subprocess.CalledProcessError as exc:raise RuntimeError((exc.stderr or exc.stdout or str(exc))[-2400:]) from exc

def key():
    if os.getenv('LUNA_API_KEY'):return os.environ['LUNA_API_KEY']
    if os.getenv('LUNA_KEY_FILE'):return Path(os.environ['LUNA_KEY_FILE']).read_text().strip()
    raise ValueError('Provide a model credential through the workflow gateway, LUNA_API_KEY or LUNA_KEY_FILE')

def parse(content):
    text=content if isinstance(content,str) else ''.join(p.get('text','') for p in content)
    text=re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip());return json.loads(text[text.find('{'):text.rfind('}')+1])

def call(folder,prompt,images,feedback=""):
    if 'json' not in (prompt+' '+feedback).lower():prompt+='\n只输出 JSON 对象'
    folder.mkdir(parents=True,exist_ok=True);parts=[{'type':'text','text':prompt}]
    for name,path in images:
        parts.extend([{'type':'text','text':name},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode(),'detail':'high'}}])
    if feedback:parts.append({'type':'text','text':feedback})
    body={'model':MODEL,'messages':[{'role':'user','content':parts}],'max_tokens':5500,'response_format':{'type':'json_object'}}
    fingerprint=hashlib.sha256(json.dumps({'endpoint':ENDPOINT,'body':body},sort_keys=True).encode()).hexdigest()
    cache=folder/'cache.json'
    if cache.exists():
        old=json.loads(cache.read_text())
        if old['fingerprint']==fingerprint:return old['result']
    for attempt in range(4):
        began=time.time();attempt_fingerprint=hashlib.sha256(json.dumps({'endpoint':ENDPOINT,'body':body},sort_keys=True).encode()).hexdigest()
        raw_index=attempt
        while (folder/f'response_{attempt_fingerprint[:12]}_{raw_index}.txt').exists() or (folder/f'error_{attempt_fingerprint[:12]}_{raw_index}.txt').exists():raw_index+=1
        try:
            response=requests.post(ENDPOINT,headers={'Authorization':'Bearer '+key()},json=body,timeout=600)
            (folder/f'response_{attempt_fingerprint[:12]}_{raw_index}.txt').write_text(response.text)
            response.raise_for_status();raw=response.json()
            (folder/f'usage_{attempt_fingerprint[:12]}_{raw_index}.json').write_text(json.dumps({'model':MODEL,'usage':raw.get('usage',{}),'elapsed':time.time()-began}))
            result=parse(raw['choices'][0]['message']['content'])
            (folder/'usage.json').write_text(json.dumps({'model':MODEL,'usage':raw.get('usage',{}),'elapsed':time.time()-began,'fingerprint':fingerprint}))
            cache.write_text(json.dumps({'fingerprint':fingerprint,'actual_request_fingerprint':attempt_fingerprint,'result':result},ensure_ascii=False,indent=2));return result
        except Exception as exc:
            (folder/f'error_{attempt_fingerprint[:12]}_{raw_index}.txt').write_text(type(exc).__name__+': '+str(exc)+'\n'+traceback.format_exc())
            if attempt==3:raise
            delay=min(30,5*(attempt+1))
            if isinstance(exc,requests.HTTPError):
                status=exc.response.status_code
                if status!=429 and status<500:raise
                retry_after=exc.response.headers.get('Retry-After')
                if status==429 and retry_after:
                    try:delay=max(0,float(retry_after))
                    except ValueError:
                        try:delay=max(0,parsedate_to_datetime(retry_after).timestamp()-time.time())
                        except (ValueError,TypeError):pass
            elif isinstance(exc,(requests.Timeout,requests.ConnectionError)):pass
            elif isinstance(exc,(ValueError,KeyError,TypeError)):
                if attempt>0:raise
                body['messages'][0]['content'].append({'type':'text','text':'上次格式错误。只返回短JSON对象。'})
            else:raise
            time.sleep(delay)



def Remote(name):
    """Compatibility factory; new code depends on the RenderWorker interface."""
    return create_render_worker(name, WORKER_BYTES)


def artifact_fingerprint(source,plan=None,worker_sha256=WORKER_SHA256):
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    value={'source_sha256':digest.hexdigest(),'worker_sha256':worker_sha256,'render':render_settings()}
    if plan:value['plan_sha256']=hashlib.sha256(plan.read_bytes()).hexdigest()
    return value


def ensure_inspect(remote,source,out,screen_only=False):
    fingerprint=artifact_fingerprint(source,worker_sha256=remote.worker_sha256);marker=out/'fingerprint.json'
    old=json.loads(marker.read_text()) if marker.exists() else {}
    same=all(old.get(k)==v for k,v in fingerprint.items())
    views_ready=all((out/name).exists() for name in ['cameras.json',*[v+'.png' for v in VIEWS]])
    groups_ready=all((out/name).exists() for name in ['groups.json','groups.png','inventory.json'])
    if same and views_ready and (screen_only or groups_ready):return out
    if not screen_only and same and views_ready:
        remote.run(source,out,groups_only=True)
    else:remote.run(source,out,screen_only=screen_only)
    marker.write_text(json.dumps({**fingerprint,'mode':'screen' if screen_only else 'augment'},indent=2));return out


def validate_plan(plan,inventory,*,levels=("simple","medium","hard")):
    if not isinstance(plan,dict):raise ValueError('plan必须JSON对象')
    if 'unsuitable' in plan:
        if not isinstance(plan['unsuitable'],str) or not plan['unsuitable'].strip():raise ValueError('unsuitable须一句原因')
        return
    items=plan.get('items')
    if not isinstance(items,list) or len(items)!=len(levels) or {p.get('level') for p in items if isinstance(p,dict)}!=set(levels):raise ValueError('items必须包含指定档位:'+','.join(levels))
    ids={r['id'] for r in inventory}
    for item in items:
        tier=item['level']
        if item.get('start') not in VIEWS or item.get('reveal') not in VIEWS+['region_45','region_225'] or item['start']==item['reveal']:raise ValueError(tier+': start/reveal须有效且不同')
        for field in ('question','target'):
            if not isinstance(item.get(field),str) or not item[field].strip():raise ValueError(tier+': '+field+'须非空文字')
        copies=item.get('copies')
        minimum=0 if levels==('medium',) else 1
        if not isinstance(copies,list) or not minimum<=len(copies)<=8:raise ValueError(tier+': copies数量不合法；单题medium可用空列表保留原场景')
        if os.getenv('SCENEQA_NATIVE_ONLY')=='1' and copies:raise ValueError('本批次只用原始场景出题，copies必须为空')
        if not copies and item['reveal'] not in VIEWS:raise ValueError(tier+': 原场景题须使用已有八视角，不能引用尚未渲染的region')
        for index,copy in enumerate(copies):
            prefix=tier+f' copy{index}: '
            if not isinstance(copy.get('ids'),list) or not copy['ids'] or any(type(i) is not int or i not in ids for i in copy['ids']):raise ValueError(prefix+'ids须有效完整组id')
            if copy.get('support') is not None and (type(copy['support']) is not int or copy['support'] not in ids):raise ValueError(prefix+'support须有效id')
            if 'at' not in copy and 'offset' not in copy:copy['at']=[.5,.5]
            if 'at' in copy:
                if copy.get('support') is None or not isinstance(copy['at'],list) or len(copy['at'])!=2 or any(type(x) not in (int,float) or not 0<=x<=1 for x in copy['at']):raise ValueError(prefix+'at须[u,v]两数0..1并指定support')
            elif not isinstance(copy.get('offset'),list) or len(copy['offset'])!=3 or any(type(x) not in (int,float) or abs(x)>1 for x in copy['offset']):raise ValueError(prefix+'旧offset须三数-1..1；新计划请用at')

def validate_build(folder,*,levels=("simple","medium","hard")):
    for tier in levels:
        glb=folder/tier/'scene.glb'
        with glb.open('rb') as stream:header=stream.read(12)
        if len(header)!=12:raise RuntimeError(f'{tier}: GLB header缺失')
        magic,version,length=struct.unpack('<4sII',header)
        if magic!=b'glTF' or version!=2 or length!=glb.stat().st_size:raise RuntimeError(f'{tier}: GLB header/长度不正确')
        if not (folder/tier/'cameras.json').is_file():raise RuntimeError(f'{tier}: 缺少相机证据')


def validate_review(review):
    if type(review.get('pass')) is not bool:raise ValueError('review pass必须布尔值')
    if type(review.get('answer')) in (int,float):review['answer']=str(review['answer'])
    for field in ('answer','reason'):
        if not isinstance(review.get(field),str) or not review[field].strip():raise ValueError('review '+field+'必须非空文本')
        review[field]=review[field].strip()


def support_candidates(inventory,groups):
    """Prefer broad native surfaces already near small native props, then floors."""
    scored=[]
    for row in inventory:
        area=row['size'][0]*row['size'][2]
        if area<.0003:continue
        low,high=row['bbox'];best=10.
        for group in groups:
            if row['id'] in group['ids']:continue
            a,b=group['bbox'];x=(a[0]+b[0])*.5;z=(a[2]+b[2])*.5
            if low[0]<=x<=high[0] and low[2]<=z<=high[2] and high[1]<=a[1]+.005:best=min(best,max(0,a[1]-high[1]))
        scored.append((best,-area,row))
    return [entry[2] for entry in sorted(scored,key=lambda x:(x[0],x[1]))[:20]]

def run_scene(remote,source,out,initial_feedback="",*,focus="all",rounds=None):
    if focus not in ('all','medium'):raise ValueError('focus must be all or medium')
    count=ROUNDS if rounds is None else rounds
    if type(count) is not int or count<1:raise ValueError('rounds must be positive int')
    plan_prompt=PLAN_PROMPT;review_prompt=REVIEW_PROMPT
    levels=('medium',) if focus=='medium' else ('simple','medium','hard')
    if focus=='medium':
        plan_prompt='只出medium一道题。定位用中性外观/位置，目标/同款命名勿提示待答属性；计数可写类别。question须探索后可答，不能是复制/建模指令或题干已给所问答案；答案须回应问题。自然复制原生完整小物件，禁结构/碎片/悬浮；答案须被明显遮挡，起始图不可答；问题唯一指代、方位明确。copies为完整组ids与support；at是支撑XZ范围0..1位置。只输出JSON：{"items":[{"level":"medium","target":"类别","copies":[{"ids":[0],"support":1,"at":[0.5,0.5]}],"question":"题目","start":"视图名","reveal":"视图名"}]}。不适合则{"unsuitable":"原因"}。'
        plan_prompt+='\n优先直接利用原场景已有内容与遮挡出题，此时copies=[]，start/reveal都选已有az视角。没有可复制组不代表场景不能出题，尤其扫描场景不可强拆网格复制。只有原景确实缺少所需遮挡且存在完整可复制小物件时才填copies进行自然增强。不得为通过门槛硬凑数量。原景题也必须初始真实遮挡、换视角后唯一可答；若八视角不能提供清晰答案证据则换目标或报告不适合。题型可以是颜色、物件身份、数量、姿态、相对位置或承载关系，避免只出计数题。'
        plan_prompt+='\n利用场景的多个区域和相似候选目标：优先先通过唯一参照物或摆放关系找到目标，再观察其被遮挡的属性、内部物件或局部状态。题目难度来自真实内容和遮挡，不来自模糊纹理、扫描缺损、冗长题干或大量重复物件。不强求固定操作轮数；必须有清晰答案证据，不能只问全局显眼大物体的颜色。'
        review_prompt='定位须中性，目标/同款命名提示待答属性则fail；计数可写类别。看medium实际start/reveal/region，图中回答。question必须是探索后可答的问题；复制/建模指令、题干已给所问答案、答案不回应问题均fail。检查自然摆放、明确遮挡、初始不泄题、唯一问题及答案支持。只输出JSON：{"items":[{"level":"medium","answer":"答案","pass":true或false,"reason":"实际证据"}]}。不改题或相机。'
        if os.getenv('SCENEQA_NATIVE_ONLY')=='1':
            plan_prompt+='\n本批次必须直接用原始场景出题：copies只能是[]；禁止复制、增删或移动任何物件。选择已有遮挡、参照和清晰目标；不合适就unsuitable，不要凑题。'
    out.mkdir(parents=True,exist_ok=True);inspect=out/'inspect'
    ensure_inspect(remote,source,inspect)
    inventory=json.loads((inspect/'inventory.json').read_text());feedback=('\n此前独立看图反馈：'+initial_feedback) if initial_feedback else ''
    if len(inventory)>3000 and focus!='medium':
        result={'unsuitable':'超过3000节点，轻量组装无法可靠确认完整物件'};(out/'result.json').write_text(json.dumps(result,ensure_ascii=False));return result
    for iteration in range(count):
        round_dir=out/f'round_{iteration}';round_dir.mkdir(exist_ok=True)
        groups=json.loads((inspect/'groups.json').read_text())
        if not groups and focus!='medium':
            result={'unsuitable':'没有能可靠复制的独立小物件组'};(out/'result.json').write_text(json.dumps(result,ensure_ascii=False));return result
        supports=support_candidates(inventory,groups)
        prompt=plan_prompt+'\n联系图对应可复制完整组：'+json.dumps(groups,ensure_ascii=False,separators=(',',':'))+'\n支撑候选：'+json.dumps(supports,ensure_ascii=False,separators=(',',':'))+'\n可选视图名：'+','.join(VIEWS+['region_45','region_225'])
        plan=call(round_dir/'plan_call',prompt,[(v,inspect/(v+'.png')) for v in VIEWS]+[('可复制组id联系图',inspect/'groups.png')],feedback=feedback)
        try:validate_plan(plan,inventory,levels=levels)
        except Exception as exc:
            (round_dir/'plan_error.txt').write_text(type(exc).__name__+': '+str(exc)+'\n'+traceback.format_exc());feedback='\n上一版计划无效，请修正：'+type(exc).__name__+': '+str(exc);continue
        (round_dir/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
        if 'unsuitable' in plan:
            (out/'result.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2));return plan
        built=round_dir/'built'
        build_fingerprint=artifact_fingerprint(source,round_dir/'plan.json',remote.worker_sha256);marker=built/'fingerprint.json'
        if not marker.exists() or json.loads(marker.read_text())!=build_fingerprint or not all((built/tier/'cameras.json').exists() for tier in levels):
            build_ok=False
            for build_attempt in range(2):
                try:
                    remote.run(source,built,round_dir/'plan.json');validate_build(built,levels=levels);marker.write_text(json.dumps(build_fingerprint,indent=2));build_ok=True;break
                except Exception as exc:
                    (round_dir/f'build_error_{build_attempt}.txt').write_text(str(exc))
                    feedback='\n上一版构建失败，保留三档内容修正support/at：'+json.dumps(plan,ensure_ascii=False,separators=(',',':'))+'\n'+str(exc)[-800:]
                    if 'ValueError:' in str(exc) or 'GLB header' in str(exc):break
            if not build_ok:continue
        validate_build(built,levels=levels)
        questions=[{k:item[k] for k in ('level','question','start','reveal')} for item in plan['items']]
        images=[]
        for item in plan['items']:
            tier=built/item['level'];region='region_225' if item['reveal']=='region_45' else 'region_45'
            for role,view in [('start',item['start']),('reveal',item['reveal']),('region',region)]:
                if (tier/(view+'.png')).is_file():images.append((item['level']+' '+role+' '+view,tier/(view+'.png')))
        joint=call(round_dir/'review_call',review_prompt+'\n三题：'+json.dumps(questions,ensure_ascii=False,separators=(',',':')),images)
        if not isinstance(joint.get('items'),list) or len(joint['items'])!=len(levels) or {r.get('level') for r in joint['items']}!=set(levels):raise ValueError('review必须包含指定档位:'+','.join(levels))
        reviews={r['level']:r for r in joint['items']};results=[]
        for item in plan['items']:
            tier=built/item['level'];review=reviews[item['level']];validate_review(review)
            camera=json.loads((tier/'cameras.json').read_text())[item['start']]
            result={'level':item['level'],'target':item['target'],'question':item['question'],'answer':review['answer'],'initial_camera':camera,'start_image':item['start']+'.png','reveal_image':item['reveal']+'.png','model_pass':review['pass'],'reason':review['reason'],'glb':str(tier/'scene.glb'),'scene_mode':'augmented' if item['copies'] else 'native'}
            (tier/'question.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));results.append(result)
        summary={'source':str(source),'model':MODEL,'iteration':iteration,'items':results,'focus':focus,'pass':all(r['model_pass'] is True for r in results if focus=='all' or r['level']=='medium'),'validation':'model visual self-review, not independent gold'}
        (out/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        if focus=='medium' or summary['pass']:return summary
        feedback='\n上一版自查失败，调整这些问题：'+json.dumps([{k:r[k] for k in ('level','target','question','reason')} for r in results if r['model_pass'] is False and (focus=='all' or r['level']=='medium')],ensure_ascii=False,separators=(',',':'))
    summary=locals().get('summary',{'error':'有限修复仍未产出有效计划或GLB'})
    (out/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));return summary


def set_current_run(uid_root, run_dir, status):
    uid_root.mkdir(parents=True,exist_ok=True)
    pointer={"run_dir":str(run_dir.relative_to(uid_root)) or ".","status":status}
    temporary=uid_root/"current_run.json.tmp"
    temporary.write_text(json.dumps(pointer))
    temporary.replace(uid_root/"current_run.json")
    return pointer

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--queue',type=Path);ap.add_argument('--pod',default='scene-sol-augment-20260930');ap.add_argument('--prepare-only',action='store_true');ap.add_argument('--inspect-only',action='store_true');ap.add_argument('--screen-only',action='store_true');args=ap.parse_args()
    remote=Remote(args.pod)
    if args.prepare_only:remote.create();return
    sources=[(args.source.stem,args.source)] if args.source else [];feedback_by_uid={}
    if args.queue:
        for line in args.queue.read_text().splitlines():
            if not line.strip():continue
            row=json.loads(line) if line.lstrip().startswith('{') else None
            source=Path((row.get('source_glb') or row.get('path')) if row else line.strip())
            uid=str(row.get('uid') or source.stem) if row else source.stem
            sources.append((uid,source))
            if row and row.get('feedback'):feedback_by_uid[uid]=str(row['feedback'])
    uids=[uid for uid,_ in sources]
    if len(uids)!=len(set(uids)):raise ValueError('队列uid重复，必须给每件场景唯一uid')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',uid) for uid in uids):raise ValueError('非法uid，禁止路径字符或路径穿越')
    remote.create()
    for uid,source in sources:
        uid_root=args.out/uid;scene_out=uid_root
        try:
            if not (args.inspect_only or args.screen_only):set_current_run(uid_root,scene_out,"running")
            if args.inspect_only or args.screen_only:
                ensure_inspect(remote,source,scene_out/'inspect',screen_only=args.screen_only);print(json.dumps({'scene':uid,'inspect':str(scene_out/'inspect')}),flush=True);continue
            run_fingerprint={**artifact_fingerprint(source,worker_sha256=remote.worker_sha256),'model':MODEL,'url':ENDPOINT,'plan_prompt_sha256':hashlib.sha256(PLAN_PROMPT.encode()).hexdigest(),'review_prompt_sha256':hashlib.sha256(REVIEW_PROMPT.encode()).hexdigest(),'feedback_sha256':hashlib.sha256(feedback_by_uid.get(uid,'').encode()).hexdigest()}
            run_marker=scene_out/'run_fingerprint.json'
            existing=json.loads(run_marker.read_text()) if run_marker.exists() else None
            if existing is not None and existing!=run_fingerprint or (scene_out/'result.json').exists() and existing is None:
                scene_out=scene_out/('version_'+hashlib.sha256(json.dumps(run_fingerprint,sort_keys=True).encode()).hexdigest()[:12]);run_marker=scene_out/'run_fingerprint.json'
            set_current_run(uid_root,scene_out,'running')
            final=scene_out/'result.json'
            same=run_marker.exists() and json.loads(run_marker.read_text())==run_fingerprint
            state=json.loads((scene_out/'state.json').read_text()).get('status') if (scene_out/'state.json').exists() else None
            if final.exists() and same:
                result=json.loads(final.read_text())
                terminal=result.get('terminal') is True or (scene_out/'terminal.json').exists()
                if terminal and state!='running':
                    set_current_run(uid_root,scene_out,'unsuitable' if result.get('unsuitable') else 'error' if result.get('error') else 'complete')
                    print(json.dumps({'scene':uid,'cached_terminal':True,'run_dir':str(scene_out),'pass':result.get('pass',False),'unsuitable':result.get('unsuitable'),'error':result.get('error')},ensure_ascii=False),flush=True);continue
            scene_out.mkdir(parents=True,exist_ok=True);run_marker.write_text(json.dumps(run_fingerprint,indent=2))
            (scene_out/'state.json').write_text(json.dumps({'status':'running','uid':uid,'pod':remote.name,'started_at':time.time()}))
            result=run_scene(remote,source,scene_out,feedback_by_uid.get(uid,''));result['terminal']=True;(scene_out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            (scene_out/'state.json').write_text(json.dumps({'status':'unsuitable' if result.get('unsuitable') else 'error' if result.get('error') else 'complete','uid':uid,'finished_at':time.time()}))
            set_current_run(uid_root,scene_out,'unsuitable' if result.get('unsuitable') else 'error' if result.get('error') else 'complete')
            (scene_out/'terminal.json').write_text(json.dumps({'completed_at':time.time(),'uid':uid,'pass':result.get('pass',False),'unsuitable':result.get('unsuitable')}));print(json.dumps({'scene':uid,'run_dir':str(scene_out),'pass':result.get('pass',False),'unsuitable':result.get('unsuitable'),'error':result.get('error')},ensure_ascii=False),flush=True)
        except Exception as exc:
            dest=scene_out;set_current_run(uid_root,dest,'error');dest.mkdir(parents=True,exist_ok=True);error=type(exc).__name__+': '+str(exc);(dest/'error.txt').write_text(error+'\n'+traceback.format_exc());(dest/'state.json').write_text(json.dumps({'status':'error','uid':uid,'error':error,'finished_at':time.time()}));print(json.dumps({'scene':uid,'run_dir':str(dest),'error':error}),flush=True)
if __name__=='__main__':main()
