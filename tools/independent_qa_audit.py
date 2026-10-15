"""Blind second-call model review; advisory only, never gold or acceptance."""
import argparse,base64,concurrent.futures,hashlib,json,os,pathlib,time,urllib.request,urllib.error,html,fcntl,tempfile
PROMPT='''你是独立的视觉审阅员，只根据给定题目、候选答案与实际图片复核，不猜测未见信息。这是另一轮模型审阅意见，不是人工gold。每场景有简单、中等、困难三题，三题的完整目标和题型都须不同：计数、身份、颜色、图案文字、姿态状态、包含承载、相对位置、远近排序、尺寸比较、部件连接。按实际问法判定，换措辞不算不同题型，不能三题全计数，也不能只对同类物件改问属性。全部三档的起始图都必须无法直接答题，简单至少一次合理换视角取证；中等有实际局部遮挡；困难必须先用唯一线索定位，再转向不同的局部隐藏目标，不能只加“先找到”文字。对困难题删除第一跳再检查：若剩余地址仍唯一指到目标则fail；图片中须有至少2个相似候选目标或区域，确实需要第一跳选定其中一个。答案必须被实际揭示图清楚支持，不许从小像素、标签或常识猜；物件放置应自然，无悬浮、穿插、无意义堆叠。detail图仅能帮助核对答案，不能替代start图检查泄题。每题指代须唯一；相对位置须明确以谁、哪个方向或哪一视角为参照。必须审完全部三题，即使简单题已失败，也检查未改的中等和困难题。理由须分别简述simple/medium/hard的检查结果，不能用未提及代表通过。任一硬条件不足则pass=false；没有确定证据须理由明确说“证据不足”，不能认可。只输出JSON {"uid":"给定ID","pass":true或false,"bad_levels":["simple/medium/hard中不合格档位"],"reason":"中文具体错误或证据不足，不超过180字"}。'''
MEDIUM_PROMPT='你是独立视觉审阅员，只审中等题，不评价其他题、题型重复或两跳。首先必须是观察者可探索回答的问题，不能是复制或建模指令，不能题干直接给出所问答案；候选答案须回应问题。定位线索也不能用目标或同款物件名称泄露待答属性，例如问袋上物品名不能用“面粉袋”定位；计数题可写类别，不泛化禁止类别。证据足够但上述不合格时选question返修。只根据实际图片检查：题目指代和位置参照清晰唯一；起始图无法直接回答且目标存在真实局部遮挡，仅物件很小不算；合理改变视角后揭示图清楚支持候选答案；摆放自然，无严重悬浮或穿插。失败时由你选择返修动作：question=场景证据足够，只修指代、措辞或候选答案；view=无需改3D，可在已有视图选择真正隐藏的起始或清楚揭示；scene=需改3D的真实遮挡、摆放或完整物件。通过为none；失败理由写具体返修要求，不猜测未见信息。理由必须明确交代指代、起始隐藏与真实遮挡、揭示答案证据、摆放；证据不足明确说不足并不通过。只输出JSON {"uid":"给定ID","pass":true或false,"bad_levels":[不合格时填"medium"],"repair_action":"none|question|view|scene","reason":"中文短理由，不超过180字"}。这是模型意见，不是人工gold。'
def validate_judgment(j,uid,level):
 if j.get('uid')!=uid or type(j.get('pass'))is not bool or not isinstance(j.get('bad_levels'),list)or not isinstance(j.get('reason'),str)or not j['reason'].strip():raise ValueError('bad audit JSON')
 if any(x not in (('medium',)if level=='medium'else('simple','medium','hard'))for x in j['bad_levels']):raise ValueError('invalid level')
 if level=='medium':
  action=j.get('repair_action')
  if action not in ('none','question','view','scene'):raise ValueError('invalid medium repair_action')
  if j['bad_levels']!=([]if j['pass']else['medium'])or (action=='none')!=j['pass']:raise ValueError('inconsistent medium pass/action/bad_levels')
 return {k:j[k]for k in ('uid','pass','bad_levels','reason')+(('repair_action',)if level=='medium'else())}

def prompt_text(prompt,uid,blind):
 return prompt+'\n场景ID："'+uid+'"。\n题目与候选答案：'+json.dumps(blind,ensure_ascii=False)+'\n'

def evidence_key(uid,level,prompt,model,url,blind,glbs,paths):
 evidence={'uid':uid,'level':level,'prompt':prompt,'model':model,'url':url,'temperature':0,'max_tokens':1600,'items':blind,'glbs':[{'level':lev,'sha256':hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()}for lev,p in glbs],'images':[{'label':label,'sha256':hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()}for label,p in paths]}
 return hashlib.sha256(json.dumps(evidence,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def write_first_cache(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False)as f:
  json.dump(value,f,ensure_ascii=False,indent=2);name=f.name
 try:
  try:os.link(name,path)
  except FileExistsError:pass
 finally:os.unlink(name)

def prime_shared_cache(record_path,cache_dir):
 record_path=pathlib.Path(record_path).resolve();r=json.loads(record_path.read_text());level=r.get('level','all');prompt=MEDIUM_PROMPT if level=='medium'else PROMPT
 if r.get('status')!='ok':raise ValueError('only valid successful review records can prime cache')
 j=validate_judgment(r['judgment'],r['uid'],level);blind=r['input_items']
 if(record_path.parent/'prompt.txt').read_text()!=prompt_text(prompt,r['uid'],blind):raise ValueError('record prompt protocol mismatch')
 result_path=pathlib.Path(r['result_path']);result=json.loads(result_path.read_text());items=[x for x in result['items']if level=='all'or x['level']=='medium']
 if [{'level':x['level'],'question':x['question'],'answer':x['answer']}for x in items]!=blind:raise ValueError('record result question changed')
 glbs=[(x['level'],(result_path.parent/pathlib.Path(x['glb'])).resolve())for x in items];paths=[(x['label'],pathlib.Path(x['path']))for x in r['input_images']]
 cache_key=evidence_key(r['uid'],level,prompt,r['model'],r['url'],blind,glbs,paths)
 raw=[str(p.resolve())for p in sorted(record_path.parent.glob('raw_*.json'))]
 if not raw:raise ValueError('missing original raw response')
 value={'key':cache_key,'status':'ok','uid':r['uid'],'level':level,'judgment':j,'usage':r.get('usage',[]),'served_model':r.get('served_model'),'source_record':str(record_path),'raw_response_paths':raw,'created_at':time.time()}
 write_first_cache(pathlib.Path(cache_dir)/(cache_key+'.json'),value);return cache_key

def current_run(uid_root):
 root=uid_root.resolve();pointer_file=root/'current_run.json'
 if not pointer_file.exists():return root,None
 pointer=json.loads(pointer_file.read_text());relative=pointer.get('run_dir')
 if not isinstance(relative,str)or not relative or pathlib.Path(relative).is_absolute()or '..'in pathlib.Path(relative).parts:raise ValueError('current_run.run_dir must be a restricted relative path')
 directory=(root/relative).resolve()
 if not directory.is_relative_to(root):raise ValueError('current_run.run_dir escapes UID directory')
 if pointer.get('status')not in ('running','error','unsuitable','complete'):raise ValueError('invalid current_run status')
 return directory,pointer

def audit(uid,args,key):
 level=getattr(args,'level','all');prompt=MEDIUM_PROMPT if level=='medium'else PROMPT
 dest=args.output/uid;dest.mkdir(parents=True,exist_ok=True);r={'uid':uid,'status':'failed','cached':False,'level':level,'usage':[],'independent_model_opinion_only':True,'errors':[]};start=time.time();shared_lock=None
 try:
  current,pointer=current_run(args.baseline/uid)
  r['run_dir']=str(current);r['current_run']=pointer;r['result_path']=str(current/'result.json')
  if pointer and pointer['status']in ('running','error'):raise ValueError('current generation '+pointer['status']+'; no fallback or model call')
  if pointer and pointer['status']=='unsuitable':
   result=json.loads((current/'result.json').read_text())if(current/'result.json').exists()else{}
   r.update(status='source_unsuitable',source_reason=result.get('unsuitable','current generation unsuitable'),source_metadata=result.get('source'),elapsed_seconds=round(time.time()-start,2));(dest/'record.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));return r
  result=json.loads((current/'result.json').read_text());r['source_metadata']=result.get('source')
  if args.require_terminal and not(result.get('terminal')is True or(current/'terminal.json').exists()):raise ValueError('generation not terminal; no model call')
  items=result.get('items',[]);paths=[];blind=[];glbs=[]
  if not items and result.get('unsuitable'):
   r.update(status='source_unsuitable',source_reason=result['unsuitable'],elapsed_seconds=round(time.time()-start,2));(dest/'record.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));return r
  if not items and result.get('error'):raise ValueError('generation processing failed: '+str(result['error']))
  if level=='medium':
   items=[x for x in items if x.get('level')=='medium']
   if len(items)!=1:raise ValueError('need exactly one medium item')
  elif {x['level']for x in items}!={'simple','medium','hard'}:raise ValueError('not three levels')
  for item in items:
   glb=pathlib.Path(item['glb']);glb=(current/glb).resolve()if not glb.is_absolute()else glb.resolve()
   base=glb.parent
   if not glb.is_file():raise ValueError('missing final GLB')
   glbs.append((item['level'],glb))
   q={'level':item['level'],'question':item['question'],'answer':item['answer']};blind.append(q)
   paths.extend([(item['level']+' start '+item['start_image'],base/item['start_image']),(item['level']+' reveal '+item['reveal_image'],base/item['reveal_image'])])
   if args.regions:
    paths.extend((item['level']+' extra region '+p.name,p)for p in sorted(base.glob('region*.png'))[:2]if p not in {x[1]for x in paths})
   if args.overviews:
    paths.extend((item['level']+' extra overview '+p.name,p)for p in sorted(base.glob('az_*_overview.png'))if p not in {x[1]for x in paths})
   if args.details:
    paths.extend((item['level']+' extra detail '+p.name,p)for p in sorted(base.glob('detail_*.png'))[:2])
  data=[p.read_bytes()for _,p in paths];shared_key=evidence_key(uid,level,prompt,args.model,args.url,blind,glbs,paths);fingerprint=hashlib.sha256((shared_key+prompt+level+json.dumps(blind,ensure_ascii=False)+uid+str(current)+args.model+args.url).encode()+b''.join(hashlib.sha256(b).digest()for b in data)).hexdigest();record=dest/'record.json'
  r.update(fingerprint=fingerprint,model=args.model,url=args.url,input_items=blind,input_images=[{'label':label,'path':str(p)}for label,p in paths],input_glbs=[{'level':lev,'path':str(p)}for lev,p in glbs],shared_cache_key=shared_key)
  text=prompt_text(prompt,uid,blind)
  shared_dir=getattr(args,'shared_cache_dir',None);shared_path=None
  if shared_dir:
   shared_dir=pathlib.Path(shared_dir);shared_dir.mkdir(parents=True,exist_ok=True);shared_path=shared_dir/(shared_key+'.json');shared_lock=(shared_dir/(shared_key+'.lock')).open('a');fcntl.flock(shared_lock,fcntl.LOCK_EX)
   if shared_path.exists():
    cached=json.loads(shared_path.read_text())
    if cached.get('key')!=shared_key or cached.get('status')!='ok':raise ValueError('invalid shared cache record')
    judgment=validate_judgment(cached['judgment'],uid,level)
    if record.exists():
     previous=json.loads(record.read_text())
     if previous.get('judgment')!=judgment:
      archive=dest/('attempt_'+str(time.time_ns()));archive.mkdir()
      for f in list(dest.iterdir()):
       if f.is_file():f.replace(archive/f.name)
    r.update(status='ok',cached=True,cache_source='shared',judgment=judgment,usage=cached.get('usage',[]),served_model=cached.get('served_model'),source_record=cached['source_record'],raw_response_paths=cached.get('raw_response_paths',[]),elapsed_seconds=round(time.time()-start,2))
    (dest/'prompt.txt').write_text(text);record.write_text(json.dumps(r,ensure_ascii=False,indent=2));shared_lock.close();return r
  if record.exists():
   old=json.loads(record.read_text())
   if old.get('status')=='ok'and old.get('fingerprint')==fingerprint:
    validate_judgment(old['judgment'],uid,level)
    if shared_path:prime_shared_cache(record,shared_dir)
    if shared_lock:shared_lock.close()
    return {**old,'cached':True}
   archive=dest/('attempt_'+str(time.time_ns()));archive.mkdir()
   for f in list(dest.iterdir()):
    if f.is_file():f.replace(archive/f.name)
  (dest/'prompt.txt').write_text(text)
  parts=[{'type':'text','text':text}]
  for (label,_),b in zip(paths,data):parts.extend([{'type':'text','text':'\n图片标签：'+label+'\n'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(b).decode(),'detail':'high'}}])
  payload={'model':args.model,'max_tokens':1600,'temperature':0,'messages':[{'role':'user','content':parts}]}
  for attempt in range(1,args.max_attempts+1):
   r['attempts']=attempt;delay=2**attempt
   try:
    req=urllib.request.Request(args.url,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=180)as response:raw=response.read().decode()
    (dest/f'raw_{attempt}.json').write_text(raw);body=json.loads(raw);r['usage'].append(body.get('usage',{}));r['served_model']=body.get('model');content=body['choices'][0]['message']['content'].strip().removeprefix('```json').removesuffix('```').strip();j=json.loads(content)
    r['judgment']=validate_judgment(j,uid,level);r['status']='ok';break
   except urllib.error.HTTPError as e:
    (dest/f'error_{attempt}.json').write_bytes(e.read());r['errors'].append('HTTP '+str(e.code))
    if e.code not in (429,500,502,503,504):break
    try:delay=max(delay,float(e.headers.get('Retry-After','0')))
    except ValueError:pass
   except (urllib.error.URLError,TimeoutError,ConnectionError)as e:r['errors'].append(type(e).__name__+': '+str(e)[:200])
   except Exception as e:
    r['errors'].append(type(e).__name__+': '+str(e)[:200]);break
   if attempt<args.max_attempts:time.sleep(delay)
 except Exception as e:r['errors'].append(type(e).__name__+': '+str(e)[:200])
 r['elapsed_seconds']=round(time.time()-start,2);(dest/'record.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
 if shared_lock:
  try:
   if r['status']=='ok':prime_shared_cache(dest/'record.json',shared_dir)
  finally:shared_lock.close()
 return r

def main():
 p=argparse.ArgumentParser();p.add_argument('--shared-cache',dest='shared_cache_dir',type=pathlib.Path);p.add_argument('--level',choices=('all','medium'),default='all');p.add_argument('--baseline',type=pathlib.Path,required=True);p.add_argument('--queue',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--url',default=os.getenv('LUNA_CHAT_URL','http://127.0.0.1:18773/v1/chat/completions'));p.add_argument('--model',default='gpt-6.1-sol');p.add_argument('--key-file',type=pathlib.Path);p.add_argument('--workers',type=int,default=2);p.add_argument('--details',action='store_true');p.add_argument('--regions',action='store_true');p.add_argument('--overviews',action='store_true');p.add_argument('--max-attempts',type=int,default=3);p.add_argument('--require-terminal',action='store_true');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);key=a.key_file.read_text().strip()if a.key_file else os.getenv('LUNA_API_KEY');assert key,'Provide LUNA_API_KEY or --key-file';uids=list(dict.fromkeys(a.queue.read_text().split()));start=time.time()
 with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,a.workers))as pool:
  futures={pool.submit(audit,u,a,key):u for u in uids};results=[]
  for f in concurrent.futures.as_completed(futures):
   r=f.result();results.append(r);progress={'total':len(uids),'completed':len(results),'pending':len(uids)-len(results),'model_pass':sum(x.get('judgment',{}).get('pass')is True for x in results),'quality_fail':sum(x.get('judgment',{}).get('pass')is False for x in results),'processing_failed':sum(x['status']=='failed'for x in results),'source_unsuitable':sum(x['status']=='source_unsuitable'for x in results)}
   (a.output/'progress.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2));print(json.dumps({'uid':r['uid'],'status':r['status'],'judgment':r.get('judgment'),'completed':len(results),'total':len(uids)},ensure_ascii=False),flush=True)
  order={u:i for i,u in enumerate(uids)};results.sort(key=lambda r:order[r['uid']])
 summary={'level':a.level,'model':a.model,'scene_count':len(results),'passed':sum(r.get('judgment',{}).get('pass')is True for r in results),'failed_review':sum(r.get('judgment',{}).get('pass')is False for r in results),'errors':sum(r['status']=='failed'for r in results),'source_unsuitable':sum(r['status']=='source_unsuitable'for r in results),'cached_scene_count':sum(r['cached']for r in results),'new_scene_count':sum(not r['cached']for r in results),'elapsed_seconds':round(time.time()-start,2),'independent_model_opinion_only':True,'cost_usd':None,'new_model_call_count':sum(not r['cached']and r.get('attempts',0)>0 for r in results),'results':results}
 for name,rr in [('new_token_totals',[r for r in results if not r['cached']]),('historical_token_totals',results)]:summary[name]={k:sum(u.get(k,0)for r in rr for u in r['usage'])for k in ('prompt_tokens','completion_tokens','total_tokens')}
 summary['local_cache_hit_ratio']=summary['cached_scene_count']/len(results)if results else 0
 provider=[u['prompt_tokens_details']['cached_tokens']for r in results if not r['cached']for u in r['usage']if 'cached_tokens'in u.get('prompt_tokens_details',{})]
 if provider:summary['provider_cached_tokens']=sum(provider)
 summary['served_models']={m:sum(r.get('served_model')==m for r in results)for m in sorted({r['served_model']for r in results if r.get('served_model')})}
 (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
 gallery(summary,a.output)

def gallery(summary,out):
 title='中等题单题审核'if summary.get('level')=='medium'else'独立模型盲评'
 rows=['<!doctype html><meta charset="utf-8"><title>独立模型盲评</title><style>body{font-family:system-ui;margin:24px;background:#f4f6f8}.scene{background:white;margin:20px 0;padding:20px;border-radius:10px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}figure{margin:0}img{width:100%}figcaption{font-size:12px}small{word-break:break-all}</style><h1>独立模型盲评</h1><p>另一次模型调用意见，不是人工gold。处理失败、源场景不适合与视觉质量失败分开统计；模型未收到生成selfpass理由。</p>']
 rows[0]=rows[0].replace('独立模型盲评',title)
 rows.append('<p>'+html.escape(json.dumps({k:v for k,v in summary.items()if k!='results'},ensure_ascii=False))+'</p>')
 for r in summary['results']:
  j=r.get('judgment',{});label='模型通过'if j.get('pass')is True else'质量未通过'if j.get('pass')is False else'当前增强接口不适用'if r['status']=='source_unsuitable'else'处理失败'
  rows.append('<article class="scene"><h2>'+label+'</h2><small>'+html.escape(r.get('case_id',r['uid']))+'</small><p>'+html.escape(j.get('reason',str(r.get('source_reason',r.get('errors',[])))))+'</p>')
  for q in r.get('input_items',[]):
   rows.append('<h3>'+q['level']+'</h3><p>'+html.escape(q['question'])+'<br>候选答案：'+html.escape(q['answer'])+'</p><div class="grid">')
   for img in r.get('input_images',[]):
    if img['label'].startswith(q['level']+' '):
     src=html.escape(os.path.relpath(img['path'],out),quote=True);rows.append('<figure><a href="'+src+'"><img loading="lazy" src="'+src+'"></a><figcaption>'+html.escape(img['label'])+'</figcaption></figure>')
   rows.append('</div>')
  rows.append('</article>')
 (out/'gallery.html').write_text('\n'.join(rows))
if __name__=='__main__':main()
