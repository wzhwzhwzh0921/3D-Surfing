"""Final model review for questions usable with freely movable 3D cameras."""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

PROMPT='''你做严谨的3D探索题交付审核。解题者只有初始相机图和能自由旋转、平移、缩放的场景，没有规定的揭示截图。目标必须唯一可定位；答案不可由题干推出。左右等方位须有固定的场景物体基准，或明确说明使用已给定的初始画面；不能询问某张未给定的“揭示画面”的左/右。“长凳左端附近物体在画面哪侧”题干泄露答案，应不通过。仅说“这个物体”却没有唯一描述或标记，也不通过。中等题必须有真实遮挡；只变小但仍直接可见不通过。题目可用初始图中地标来定位目标，但不能依赖解题者没有的参考截图、物件编号或原场景对照。实际揭示图仅供你审核，不能成为题干定位的唯一依据。看实际初始/揭示及补充图，先自行回答，再判断给定答案、唯一指代、实际遮挡和自然摆放是否同时达标。看不清所问细节不能猜测。只输出JSON {"pass":true或false,"observed_answer":"你看图得到的答案或无法确定","reason":"具体原因"}。'''
PROMPT= PROMPT[:PROMPT.index('只输出JSON')] + '''地标和目标可以在探索后才发现，不要求它们出现在初始图；只要在完整场景中能唯一定位即可。明确的物体固有正面/背面、朝向也可以作方位基准。若文字能修好，repair_action=question；需选别的已有相机或换一个原场景中的可答目标，repair_action=view；需重建场景或完全看不清、不可修复，repair_action=reject。不要以目标不在初始图为由删除合理的探索题。只输出JSON {"pass":true或false,"observed_answer":"图中答案或无法确定","reason":"具体原因","repair_action":"none|question|view|reject"}。'''
STOP=False


def stop(*_):
    global STOP
    STOP=True


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));temporary.replace(path)


def signature(row,profile):
    folder=Path(row['folder'])
    return hashlib.sha256(json.dumps(dict(profile=profile,delivery=row['delivery_sha256'],
                      uid=row['uid']),sort_keys=True).encode()).hexdigest()


def review(row,out,profile,augment,repair,feedback):
    uid=row['uid'];folder=Path(row['folder']);record_path=out/uid/'record.json'
    digest=signature(row,profile)
    old=json.loads(record_path.read_text()) if record_path.exists() else {}
    if old.get('fingerprint')==digest:
        if old.get('status') in ('keep','reject','exhausted'):return old
        if time.time()<old.get('retry_at',0):return old
    attempts=old.get('attempts',0)+1 if old.get('fingerprint')==digest else 1
    try:
        current=Path(row['result_path']);history=[];final_folder=None
        for round_number in range(3):
            work=out/uid/f'round_{round_number}'
            if round_number:
                generated=work/'repaired'
                state=generated/'state.json'
                if state.exists() and json.loads(state.read_text()).get('status')=='complete':
                    current=generated/'result.json'
                else:
                    current=repair.repair(current,out/uid/f'round_{round_number-1}'/'judgment.json',generated)
            source,item,native,_=feedback.medium(current)
            images=[('initial',native.parent/item['start_image']),
                    ('judge evidence only: reveal',native.parent/item['reveal_image'])]
            images.extend((p.name,p) for p in sorted(native.parent.glob('az_*_overview.png')))
            q={key:item[key] for key in ('question','answer','initial_camera')}
            result=augment.call(work/'model_call',PROMPT,images,feedback=json.dumps(q,ensure_ascii=False))
            if (type(result.get('pass')) is not bool or not isinstance(result.get('reason'),str) or not result['reason'].strip()
                    or not isinstance(result.get('observed_answer'),str) or not result['observed_answer'].strip()
                    or result.get('repair_action') not in ('none','question','view','reject')):
                raise ValueError('Invalid final review schema')
            judgment=work/'judgment.json';save(judgment,dict(judgment=result,profile=profile,question=q))
            history.append(dict(round=round_number,result_path=str(current),judgment=result,question=q,
                                judgment_path=str(judgment)))
            if result['pass']:
                if round_number:final_folder=feedback.deliver(current,judgment,out/uid/'delivery')
                break
            if result['repair_action'] not in ('question','view'):break
        record=dict(uid=uid,fingerprint=digest,status='keep' if result['pass'] else 'reject',
                    profile=profile,judgment=result,history=history,attempts=attempts,finished_at=time.time(),gold=False)
        if final_folder:
            record.update(folder=str(final_folder),result_path=str(current),
                delivery_sha256={n:feedback.sha(final_folder/n) for n in
                                 ('scene.glb','question.json','start.png','reveal.png','audit.json')})
    except Exception as error:
        record=dict(uid=uid,fingerprint=digest,status='exhausted' if attempts>=20 else 'failed',
            profile=profile,attempts=attempts,error=type(error).__name__+': '+str(error),retry_at=time.time()+120,gold=False)
    save(record_path,record)
    print(json.dumps(dict(uid=uid,status=record['status']),ensure_ascii=False),flush=True)
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--code-root',type=Path,required=True)
    p.add_argument('--parallel',type=int,default=2);p.add_argument('--model',required=True);p.add_argument('--url',required=True)
    p.add_argument('--review-dir',type=Path,required=True)
    a=p.parse_args();a.out=a.out.resolve()
    sys.path.insert(0,str(a.code_root/'experiments/sol_augment'));import run as augment
    sys.path.insert(0,str(a.code_root/'tools'));import repair_medium as repair;import scene_feedback_loop as feedback
    augment.MODEL=a.model;augment.ENDPOINT=a.url
    repair.PROMPT += '\n修订时用固定物体关系定位，或明确以初始画面朝向为准；不要依赖未给定的揭示画面左右。目标和地标可以在探索后才发现。尽量用家具/器具完整类别，不以编号或“这个物体”代替唯一描述。必要时改问另一个实际被遮挡、图中清晰可答的原景目标。'
    profile=dict(version='navigable-question-v2-repair',model=a.model,url=a.url,
                 prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
                 code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    out=a.review_dir.resolve();signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    jobs={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.parallel) as pool:
        while not STOP:
            jobs={uid:job for uid,job in jobs.items() if not job.done()}
            for registry in sorted((a.out/'shards').glob('shard-*/registry.jsonl')):
                for line in registry.read_text().splitlines():
                    if len(jobs)>=a.parallel:break
                    if not line.strip():continue
                    row=json.loads(line);uid=row['uid']
                    if row['status']!='keep' or uid in jobs:continue
                    record_path=out/uid/'record.json'
                    if record_path.exists():
                        old=json.loads(record_path.read_text())
                        if old.get('fingerprint')==signature(row,profile):
                            if old['status'] in ('keep','reject','exhausted') or time.time()<old.get('retry_at',0):continue
                    jobs[uid]=pool.submit(review,row,out,profile,augment,repair,feedback)
            time.sleep(3)


if __name__=='__main__':main()
