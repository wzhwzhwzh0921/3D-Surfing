"""Persistent parallel production; stop at target verified deliveries, never screens."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from scene_factory.automation import file_sha, rows, save, write_rows
from scene_factory.rendering import render_settings
from scene_factory.download import glb_header
from scene_factory.sources import source_restriction, normalize_source

DELIVERY=('scene.glb','question.json','start.png','reveal.png','audit.json')


def snapshot_code(out):
    destination=out/'workflow_snapshot'
    manifest=out/'code_snapshot.json'
    if manifest.exists():
        expected=json.loads(manifest.read_text())
        if not all((destination/name).is_file() and file_sha(destination/name)==sha
                   for name,sha in expected['files'].items()):
            raise ValueError('Production code snapshot changed')
        return destination
    selected=[]
    for folder in ('scene_factory','tools','experiments'):
        selected.extend(p for p in (REPO/folder).rglob('*') if p.is_file())
    selected.extend(REPO/name for name in ('pyproject.toml','README.md','.env.example'))
    hashes={}
    for source in selected:
        if '__pycache__' in source.parts or source.suffix in ('.pyc','.glb','.blend','.log'):
            continue
        data=source.read_bytes()
        if re.search(rb'(?:ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,})',data):
            raise ValueError('Unexpected secret-like literal in code snapshot')
        name=str(source.relative_to(REPO));target=destination/name
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        hashes[name]=hashlib.sha256(data).hexdigest()
    save(manifest,dict(created_at=time.time(),files=hashes))
    return destination


def alive(pid, directory):
    try:
        command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return str(directory).encode() in command and any(b'scene_auto_pipeline.py' in p for p in command)
    except (OSError,ValueError,TypeError):
        return False


class Production:
    def __init__(self,args):
        self.args=args;self.out=args.out.resolve();self.out.mkdir(parents=True,exist_ok=True)
        self.state_path=self.out/'production.json'
        prepared=json.loads((self.out/'sources/prepared.json').read_text())
        self.config=dict(target=args.target,model=args.model,url=args.url,render=render_settings(),
            catalogs={Path(r['path']).name:prepared['files'][Path(r['path']).name] for r in prepared['catalogs']},
            max_bytes=prepared['configuration']['max_bytes'],max_repairs=args.max_repairs)
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else dict(
            configuration=self.config,created_at=time.time(),workers={},status='starting')
        if self.state['configuration']!=self.config:
            raise ValueError('Production configuration changed; use a different output folder')
        self.code=snapshot_code(self.out);self.workers=[];self.children={};self.verified={};self.stop=False
        if getattr(args,'scale_out',False):self.partition_remaining(prepared)
        self.review_enabled=not getattr(args,'no_delivery_review',False)
        if self.review_enabled:
            prior=self.state.get('delivery_review')
            advance=getattr(args,'advance_delivery_review',False)
            if advance or not prior:
                body=(REPO/'tools/delivery_qa_gate.py').read_bytes()
                gate=self.out/('delivery_review_'+hashlib.sha256(body).hexdigest()[:12]+'.py')
                if not gate.exists():gate.write_bytes(body)
            else:gate=Path(prior.get('script') or self.out/'delivery_review_snapshot.py')
            gate_sha=file_sha(gate)
            if prior and prior['code_sha256']!=gate_sha:
                if not advance:raise ValueError('Final review protocol changed')
                self.state.setdefault('delivery_review_history',[]).append({**prior,'advanced_at':time.time()})
            self.state['delivery_review']=dict(code_sha256=gate_sha,enabled=True,
                   script=str(gate),review_dir=str(self.out/'delivery_reviews'/gate_sha[:12]),
                   added_at=prior.get('added_at') if prior else time.time())
            self.gate_script=gate;self.gate_child=None
        for index,item in enumerate(prepared['catalogs'],1):
            worker=self.state['workers'].setdefault(str(index),dict(restarts=0,status='waiting',next_start=0))
            directory=self.out/'shards'/f'shard-{index:02d}'
            catalog=self.out/'sources'/Path(item['path']).name
            if file_sha(catalog)!=self.config['catalogs'][catalog.name]:
                raise ValueError('Prepared source shard changed')
            self.workers.append((str(index),directory,catalog,item['count'],worker))
            pid=worker.get('pid')
            if pid and alive(pid,directory):worker['status']='running'
            elif worker.get('status')=='running':worker.update(status='waiting',next_start=0,pid=None)
        partition=self.state.get('source_partition')
        if partition:
            for item in partition['new_shards']:
                catalog=self.out/item['path']
                if file_sha(catalog)!=item['sha256']:raise ValueError('Partition source shard changed')
                index=str(item['index']);directory=self.out/'shards'/f'shard-{int(index):02d}'
                worker=self.state['workers'].setdefault(index,dict(restarts=0,status='waiting',next_start=0))
                self.workers.append((index,directory,catalog,item['count'],worker))
                pid=worker.get('pid')
                if pid and alive(pid,directory):worker['status']='running'
                elif worker.get('status')=='running':worker.update(status='waiting',next_start=0,pid=None)
        signal.signal(signal.SIGINT,self.request_stop)
        signal.signal(signal.SIGTERM,self.request_stop)

    def partition_remaining(self,prepared):
        """Split unprocessed tails, without modifying any frozen source or QA code."""
        if self.state.get('source_partition'):return
        planned=[];heads=[];seen=set();total=0
        for index,item in enumerate(prepared['catalogs'],1):
            worker=self.state['workers'].get(str(index),{})
            directory=self.out/'shards'/f'shard-{index:02d}'
            if worker.get('pid') and alive(worker['pid'],directory):
                raise ValueError('Stop source workers before partitioning; in-flight work is preserved')
            catalog=self.out/'sources'/Path(item['path']).name
            if file_sha(catalog)!=self.config['catalogs'][catalog.name]:
                raise ValueError('Prepared source shard changed')
            entries=rows(catalog);count=len(entries);cut=(count//2//100)*100
            if cut<100:raise ValueError('Source shard too small for a page-boundary split')
            last=sorted((directory/'batches').glob('batch_*/batch.json'))
            if last:
                cursor=json.loads(last[-1].read_text())['discovery']['cursor']
                if cursor['offset']>cut:raise ValueError('Source tail already scanned; cannot partition safely')
            for entry in entries:
                row=normalize_source(entry)
                if row['uid'] in seen:raise ValueError('Original source shards overlap')
                seen.add(row['uid'])
                if source_restriction(row,queries=[],max_bytes=self.config['max_bytes'],online=False):
                    raise ValueError('Partition source exceeds the download budget')
            heads.append(dict(index=index,cut=cut,original_count=count))
            planned.append((index+len(prepared['catalogs']),entries[cut:]))
            total+=count
        shards=[]
        for index,entries in planned:
            path=self.out/'sources_scaleout'/f'shard-{index:02d}.jsonl'
            body=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in entries).encode()
            expected=hashlib.sha256(body).hexdigest()
            if path.exists() and file_sha(path)!=expected:raise ValueError('Partition source shard changed')
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_suffix('.part');temporary.write_bytes(body);temporary.replace(path)
            shards.append(dict(index=index,path=str(path.relative_to(self.out)),count=len(entries),sha256=expected))
        for item in heads:
            worker=self.state['workers'].setdefault(str(item['index']),dict(restarts=0,next_start=0))
            worker.update(scene_budget=item['cut'],status='waiting',pid=None,next_start=0)
        self.state['source_partition']=dict(version='page-boundary-scaleout-v1',created_at=time.time(),
            total_unique_sources=total,heads=heads,new_shards=shards)
        save(self.state_path,self.state)

    def request_stop(self,*_):self.stop=True

    def ensure_gate(self):
        if not self.review_enabled:return
        pid=self.state.get('gate_pid')
        try:
            cmd=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            running=str(self.out).encode() in cmd and str(self.gate_script).encode() in cmd
        except OSError:running=False
        parallel=getattr(self.args,'delivery_review_parallel',2)
        if running:
            value=cmd[cmd.index(b'--parallel')+1] if b'--parallel' in cmd else b'2'
            if value!=str(parallel).encode() and self.state.get('gate_parallel_stop_pid')!=pid:
                os.kill(pid,signal.SIGINT);self.state['gate_parallel_stop_pid']=pid;save(self.state_path,self.state)
            return
        command=[sys.executable,str(self.gate_script),'--out',str(self.out),'--code-root',str(self.code),
                 '--parallel',str(parallel),'--model',self.args.model,'--url',self.args.url,
                 '--review-dir',self.state['delivery_review']['review_dir']]
        with (self.out/'delivery_review.log').open('a') as log:
            self.gate_child=subprocess.Popen(command,cwd=self.code,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        self.state['gate_pid']=self.gate_child.pid;self.state.pop('gate_parallel_stop_pid',None);save(self.state_path,self.state)

    def launch(self,index,directory,catalog,count,worker):
        directory.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,str(self.code/'tools/scene_auto_pipeline.py'),
            '--catalog',str(catalog),'--out',str(directory),'--target',str(self.args.target),
            '--batch-size',str(self.args.batch_size),'--max-scenes',str(max(1,worker.get('scene_budget',count))),
            '--max-scan',str(max(1,count+1)),'--max-pages',str(max(1,math.ceil(count/100)+1)),
            '--workers','2','--single-worker','--retry-failed',
            '--max-bytes',str(self.config['max_bytes']),'--max-repairs',str(self.args.max_repairs),
            '--model',self.args.model,'--url',self.args.url]
        log_path=directory/'supervisor.log'
        with log_path.open('a') as log:
            process=subprocess.Popen(command,cwd=self.code,stdout=log,stderr=subprocess.STDOUT,
                                     start_new_session=True,env=dict(os.environ))
        self.children[index]=process
        worker.update(pid=process.pid,status='running',started_at=time.time(),command=command,log=str(log_path))
        save(self.state_path,self.state)
        print(json.dumps(dict(event='worker_started',shard=index,pid=process.pid),ensure_ascii=False),flush=True)

    def update_workers(self):
        for index,directory,catalog,count,worker in self.workers:
            if worker['status']=='running' and not alive(worker.get('pid'),directory):
                child=self.children.pop(index,None)
                code=child.poll() if child else None
                try:summary=json.loads((directory/'summary.json').read_text())
                except (OSError,ValueError):summary={}
                status=summary.get('status');failures=summary.get('counts',{}).get('failed',0)
                if (status=='complete' or
                        status in ('source_exhausted','scene_budget_exhausted','discovery_budget_exhausted') and not failures):
                    worker.update(status='finished',exit_code=code,terminal=status,pid=None)
                elif worker['restarts']>=self.args.max_restarts:
                    worker.update(status='blocked',exit_code=code,terminal=status,pid=None)
                else:
                    worker.update(status='waiting',restarts=worker['restarts']+1,
                        next_start=time.time()+self.args.retry_delay,exit_code=code,pid=None)
        running=sum(w['status']=='running' for *_,w in self.workers)
        for item in self.workers:
            if running>=self.args.parallel:break
            worker=item[-1]
            if worker['status']=='waiting' and time.time()>=worker.get('next_start',0):
                self.launch(*item);running+=1

    def verified_delivery(self,row):
        folder=Path(row['folder']);expected=row.get('delivery_sha256',{})
        if set(expected)!=set(DELIVERY):raise ValueError('Missing delivery hashes')
        stamp=tuple((name,(folder/name).stat().st_size,(folder/name).stat().st_mtime_ns) for name in DELIVERY)
        key=(str(folder),tuple(sorted(expected.items())))
        if self.verified.get(key)!=stamp:
            glb_header(folder/'scene.glb')
            if not all(file_sha(folder/name)==expected[name] for name in DELIVERY):
                raise ValueError('Delivery content changed')
            q=json.loads((folder/'question.json').read_text())
            if not q.get('question') or not str(q.get('answer','')).strip() or not q.get('initial_camera'):
                raise ValueError('Incomplete question delivery')
            if re.fullmatch(r'(?:region_\d+|az_\d+_(?:overview|closer)|detail_\d+_\d+)',str(q['answer']).strip()):
                raise ValueError('Answer is an internal render identifier, not a natural-language answer')
            self.verified[key]=stamp
        return folder

    def export(self,row):
        source=self.verified_delivery(row);folder=self.out/'delivery'/row['uid']
        try:
            self.verified_delivery({**row,'folder':str(folder)});intact=True
        except (OSError,ValueError,KeyError):intact=False
        if not intact:
            stage=folder.with_name('.'+folder.name+'.tmp');stage.mkdir(parents=True,exist_ok=True)
            for name in DELIVERY:
                target=stage/name;target.unlink(missing_ok=True)
                try:os.link(source/name,target)
                except OSError:shutil.copy2(source/name,target)
            save(stage/'provenance.json',{**row,'source_folder':str(source)})
            if folder.exists():
                archive=self.out/'delivery_history'/row['uid']/str(time.time_ns())
                archive.parent.mkdir(parents=True,exist_ok=True);folder.rename(archive)
            stage.replace(folder)
        delivered={**row,'source_folder':str(source),'folder':str(folder)}
        if row.get('delivery_review_path'):
            review=Path(row['delivery_review_path']);target=folder/'navigation_audit.json'
            if not target.exists() or file_sha(target)!=file_sha(review):shutil.copy2(review,target)
            delivered['navigation_audit_sha256']=file_sha(target)
        provenance={**row,'source_folder':str(source)}
        if json.loads((folder/'provenance.json').read_text())!=provenance:save(folder/'provenance.json',provenance)
        self.verified_delivery(delivered)
        return delivered

    def aggregate(self,status='running',reason=None):
        decisions={s:[] for s in ('keep','reject','failed','pending')};uids=set();hashes=set();invalid=[];duplicates=[]
        shard_summaries={}
        for index,directory,_,_,worker in self.workers:
            try:
                shard_summaries[index]=json.loads((directory/'summary.json').read_text())
                entries=rows(directory/'registry.jsonl')
            except (OSError,ValueError):continue
            for original in entries:
                row={**original,'production_shard':index};uid=row['uid']
                if uid in uids:duplicates.append(uid);continue
                uids.add(uid)
                if row['status']=='keep':
                    if self.review_enabled:
                        path=Path(self.state['delivery_review']['review_dir'])/uid/'record.json'
                        gate=json.loads(path.read_text()) if path.exists() else {}
                        gate_profile=gate.get('profile',{})
                        expected=hashlib.sha256(json.dumps(dict(profile=gate_profile,
                            delivery=row['delivery_sha256'],uid=uid),sort_keys=True).encode()).hexdigest()
                        current=(gate.get('fingerprint')==expected and
                                 gate_profile.get('code_sha256')==self.state['delivery_review']['code_sha256'] and
                                 gate_profile.get('model')==self.args.model and gate_profile.get('url')==self.args.url)
                        if not current or gate.get('status')=='failed':
                            decisions['pending'].append({**row,'status':'pending','phase':'delivery_review'});continue
                        if gate.get('status')!='keep':
                            terminal='failed' if gate.get('status')=='exhausted' else 'reject'
                            decisions[terminal].append({**row,'status':terminal,'phase':'delivery_review',
                                'reason':gate.get('judgment',{}).get('reason') or gate.get('error',''),
                                'delivery_review_path':str(path)});continue
                        row={**row,'delivery_review_path':str(path),
                             'primary_review_reason':row.get('reason',''),
                             'reason':gate.get('judgment',{}).get('reason',''),
                             'final_delivery_repairs':max(0,len(gate.get('history',[]))-1)}
                        if gate.get('folder'):
                            row.update(baseline_result_path=row['result_path'],
                               folder=gate['folder'],result_path=gate['result_path'],
                               delivery_sha256=gate['delivery_sha256'])
                    digest=row.get('glb_sha256')
                    if not digest or digest in hashes:
                        duplicates.append(uid);continue
                    try:row=self.export(row)
                    except (OSError,ValueError,KeyError) as e:
                        error=type(e).__name__+': '+str(e)
                        invalid.append(dict(uid=uid,error=error))
                        decisions['failed'].append({**row,'status':'failed','phase':'delivery_integrity','reason':error})
                        continue
                    hashes.add(digest)
                decisions[row['status']].append(row)
        free=shutil.disk_usage(self.out).free
        accepted={r['uid'] for r in decisions['keep']}
        for folder in (self.out/'delivery').glob('*'):
            if folder.is_dir() and not folder.name.startswith('.') and folder.name not in accepted:
                archive=self.out/'delivery_history'/folder.name/str(time.time_ns())
                archive.parent.mkdir(parents=True,exist_ok=True);folder.rename(archive)
        result=dict(status=status,reason=reason,target=self.args.target,
            counts={k:len(v) for k,v in decisions.items()},duplicates_skipped=duplicates,
            integrity_errors=invalid,discovered=sum(r.get('discovered',0) for r in shard_summaries.values()),
            workers={i:{**w,'summary':shard_summaries.get(i)} for i,_,_,_,w in self.workers},
            elapsed_seconds=round(time.time()-self.state['created_at'],1),disk_free_gib=round(free/1024**3,2),
            model=self.args.model,level='medium',model_opinion_not_gold=True,updated_at=time.time())
        if self.state.get('source_partition'):result['source_partition']=self.state['source_partition']
        for k in ('keep','reject','failed'):write_rows(self.out/(k+'.jsonl'),decisions[k])
        write_rows(self.out/'delivery.jsonl',decisions['keep'])
        save(self.out/'progress.json',result);save(self.out/'summary.json',result)
        self.state.update(status=status,reason=reason,updated_at=time.time());save(self.state_path,self.state)
        page=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>千题生产进度</title><style>body{font:16px/1.7 system-ui;background:#f0f3f8;max-width:1250px;margin:24px auto;padding:0 20px}article{background:white;padding:18px;margin:18px 0;border-radius:12px}.views{display:grid;grid-template-columns:1fr 1fr;gap:12px}img{width:100%}figure{margin:0}@media(max-width:700px){.views{grid-template-columns:1fr}}</style>',
            '<h1>3D 探索问答 · 1000 题批量生产</h1>',
            '<p>'+html.escape(json.dumps({k:result[k] for k in ('status','target','counts','discovered','elapsed_seconds','disk_free_gib')},ensure_ascii=False))+'</p>',
            '<p>只计完整交付且通过模型审核的中等题。审核使用独立上下文，默认仍是同一模型；不是人工正确率。</p>']
        for row in decisions['keep']:
            folder=Path(row['folder']);q=json.loads((folder/'question.json').read_text())
            page.append('<article><h2>'+html.escape(row.get('name') or row['uid'])+'</h2><div class="views">')
            for name,label in (('start.png','初始视角'),('reveal.png','揭示视角')):
                url=html.escape(os.path.relpath(folder/name,self.out),quote=True)
                page.append(f'<figure><img loading="lazy" src="{url}"><figcaption>{label}</figcaption></figure>')
            page.append('</div><p><b>原题：</b>'+html.escape(q['question'])+'<br><b>模型答案：</b>'+html.escape(str(q['answer']))+'</p><p>'+html.escape(row.get('reason',''))+'</p>')
            links=[('scene.glb','下载3D场景'),('question.json','题目及初始相机'),('audit.json','闭环审核'),('provenance.json','原始来源')]
            if (folder/'navigation_audit.json').exists():links.append(('navigation_audit.json','交付审核'))
            for name,label in links:
                page.append('<a href="'+html.escape(os.path.relpath(folder/name,self.out),quote=True)+'">'+label+'</a> · ')
            page.append('</article>')
        for index,directory,_,_,worker in self.workers:
            if (directory/'gallery.html').exists():page.append('<p><a href="'+os.path.relpath(directory/'gallery.html',self.out)+'">队列 '+index+' 的全部留删及返修记录</a></p>')
        (self.out/'gallery.html').write_text('\n'.join(page)+'</html>')
        return result

    def stop_workers(self):
        live=[]
        gate_pid=None
        if self.review_enabled:
            pid=self.state.get('gate_pid')
            try:
                cmd=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                if str(self.out).encode() in cmd and str(self.gate_script).encode() in cmd:
                    os.kill(pid,signal.SIGINT);gate_pid=pid
            except (OSError,ProcessLookupError):pass
        for index,directory,_,_,worker in self.workers:
            pid=worker.get('pid')
            if pid and alive(pid,directory):
                try:os.kill(pid,signal.SIGINT);live.append((pid,directory))
                except ProcessLookupError:pass
        deadline=time.time()+90
        while time.time()<deadline and any(alive(pid,d) for pid,d in live):time.sleep(1)
        for pid,directory in live:
            if alive(pid,directory):os.kill(pid,signal.SIGKILL)
        if gate_pid:
            try:
                cmd=Path(f'/proc/{gate_pid}/cmdline').read_bytes().split(b'\0')
                if str(self.gate_script).encode() in cmd:os.kill(gate_pid,signal.SIGKILL)
            except (OSError,ProcessLookupError):pass
        owned=['scene-auto-'+hashlib.sha256(str(d).encode()).hexdigest()[:10]+'-work'
               for _,d,_,_,_ in self.workers]
        try:
            from scene_factory.rendering import cleanup_workers, render_settings
            cleanup_workers(owned)
            save(self.out/'worker_cleanup.json', dict(workers=owned, backend=render_settings()['backend'], status='ok'))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            save(self.out/'worker_cleanup.json', dict(workers=owned, error=type(e).__name__))

    def run(self):
        status='running';reason=None
        try:
            self.aggregate()
            while not self.stop:
                current=self.aggregate()
                if current['counts']['keep']>=self.args.target:
                    status='complete';reason='最终完整交付达到目标';break
                if current['disk_free_gib']<self.args.min_free_gib:
                    status='storage_blocked';reason='可用存储低于预设下限，已保存断点';break
                self.update_workers()
                self.ensure_gate()
                if all(w['status'] in ('finished','blocked') for *_,w in self.workers) and not current['counts']['pending']:
                    status='source_exhausted' if all(w['status']=='finished' for *_,w in self.workers) else 'blocked'
                    reason='所有队列已停止，最终交付尚未达到目标';break
                time.sleep(self.args.poll_seconds)
            if self.stop:status='interrupted';reason='已收到停止信号，保留断点'
        except Exception as e:
            status='failed';reason=type(e).__name__+': '+str(e);raise
        finally:
            self.stop_workers()
            final=self.aggregate(status,reason)
            print(json.dumps(dict(event='production_stopped',status=status,counts=final['counts'],reason=reason),ensure_ascii=False),flush=True)
        return final


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--target',type=int,default=1000)
    p.add_argument('--parallel',type=int,default=4);p.add_argument('--batch-size',type=int,default=4)
    p.add_argument('--max-repairs',type=int,default=2);p.add_argument('--max-restarts',type=int,default=20)
    p.add_argument('--retry-delay',type=float,default=60);p.add_argument('--poll-seconds',type=float,default=15)
    p.add_argument('--min-free-gib',type=float,default=20)
    p.add_argument('--no-delivery-review',action='store_true',help='diagnostic only: omit final navigability question review')
    p.add_argument('--scale-out',action='store_true',help='split untouched source tails into new shards; stop existing source workers first')
    p.add_argument('--delivery-review-parallel',type=int,default=2)
    p.add_argument('--advance-delivery-review',action='store_true',help='explicitly version final review changes; preserve earlier records')
    p.add_argument('--model',default=os.getenv('LUNA_MODEL','gpt-6.1-sol'));p.add_argument('--url',default=os.getenv('LUNA_CHAT_URL','http://127.0.0.1:18773/v1/chat/completions'))
    a=p.parse_args()
    if any(v<=0 for v in (a.target,a.parallel,a.batch_size,a.poll_seconds,a.delivery_review_parallel)) or a.max_restarts<0 or a.max_repairs<0:
        p.error('Invalid production budgets')
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    with (a.out/'.production.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=Production(a).run()
    if result['status']!='complete':raise SystemExit(1)


if __name__=='__main__':main()
