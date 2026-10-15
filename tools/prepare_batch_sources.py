"""Freeze download sources for model screening, balanced by metadata category."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_factory.automation import save, write_rows
from scene_factory.sources import (DATASET, OBJAVERSE_BASE, PROTOCOL, source_restriction,
                                   _fetch, _paths, normalize_source)


def prepare(out, *, seed=20261001, max_bytes=35*1048576, max_faces=None,
            exclude_uids=(), shards=4, min_faces=None, min_materials=None,min_textures=None):
    """The old face/material/texture arguments are accepted but have no effect."""
    import pyarrow.parquet as pq
    if shards <= 0 or max_bytes <= 0:
        raise ValueError('shards and max_bytes must be positive')
    configuration=dict(protocol=PROTOCOL,quality_screen='visual_model',seed=seed,max_bytes=max_bytes,
                       exclude_uids=sorted(set(exclude_uids)),shards=shards)
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out/'prepared.json').exists():
        old=json.loads((out/'prepared.json').read_text())
        if old['configuration'] != configuration:
            raise ValueError('Prepared source configuration changed; use a new folder')
        for name,expected in old['files'].items():
            if hashlib.sha256((out/name).read_bytes()).hexdigest()!=expected:
                raise ValueError('Prepared source catalog changed: '+name)
        return old
    index_url='https://datasets-server.huggingface.co/parquet?dataset='+DATASET
    manifest=json.loads(_fetch(index_url,max_bytes=1_000_000)[0])
    files=[r for r in manifest.get('parquet_files',[]) if r['config']=='default' and r['split']=='train']
    if not files or manifest.get('pending') or manifest.get('failed'):
        raise ValueError('Public metadata parquet is not available')
    paths,path_sha=_paths(out/'metadata_cache')
    filtered=Counter(); groups={}; seen=set(); total=0; snapshots=[]
    for index,entry in enumerate(files):
        u=entry['url']
        if urlparse(u).hostname!='huggingface.co' or '/datasets/Stable-X/ProSceneverse/' not in u:
            raise ValueError('Unexpected metadata download host or dataset')
        native=out/'metadata_cache'/f'metadata-{index}.parquet'
        if not native.exists():
            native.parent.mkdir(parents=True,exist_ok=True)
            data,_=_fetch(u,max_bytes=64_000_000)
            part=native.with_suffix('.part');part.write_bytes(data);part.replace(native)
        digest=hashlib.sha256(native.read_bytes()).hexdigest()
        snapshots.append(dict(url=u,sha256=digest,bytes=native.stat().st_size))
        for original in pq.read_table(native).to_pylist():
            total+=1
            reason=source_restriction(original,queries=[],max_bytes=max_bytes,online=True)
            if reason:
                filtered[reason]+=1;continue
            uid=str(original.get('scene_id') or original.get('uid') or '')
            if uid in seen or uid in exclude_uids:
                filtered['duplicate_or_excluded']+=1;continue
            path=paths.get(uid)
            if not isinstance(path,str) or not path.startswith('glbs/') or Path(path).stem!=uid:
                filtered['no_actual_glb_path']+=1;continue
            try:
                row=normalize_source({**original,'url':OBJAVERSE_BASE+path,'glb_url':OBJAVERSE_BASE+path})
            except ValueError:
                filtered['invalid_identifier_or_url']+=1;continue
            row['discovery']=dict(provider='prosceneverse-parquet',dataset=DATASET,
                metadata_url=u,metadata_sha256=digest,objaverse_index_sha256=path_sha,
                prepared_at=datetime.now(timezone.utc).isoformat())
            seen.add(uid);groups.setdefault(row['category'],[]).append(row)
    rng=random.Random(seed)
    for values in groups.values():rng.shuffle(values)
    queues={name:deque(values) for name,values in sorted(groups.items())}
    balanced=[]
    while any(queues.values()):
        for queue in queues.values():
            if queue:balanced.append(queue.popleft())
    write_rows(out/'sources.jsonl',balanced)
    catalogs=[]
    allocations=[[] for _ in range(shards)]
    for category,values in groups.items():
        for i,row in enumerate(values):allocations[i%shards].append(row)
    for i in range(shards):
        uid_set={r['uid'] for r in allocations[i]}
        selected=[r for r in balanced if r['uid'] in uid_set];name=f'shard-{i+1:02d}.jsonl'
        write_rows(out/name,selected)
        catalogs.append(dict(path=str(out/name),count=len(selected),
                             categories=dict(Counter(r['category'] for r in selected))))
    report=dict(configuration=configuration,dataset=DATASET,
             metadata_rows=total,eligible=len(balanced),filtered=dict(filtered),
             categories={k:len(v) for k,v in groups.items()},snapshots=snapshots,
             catalogs=catalogs,files={name:hashlib.sha256((out/name).read_bytes()).hexdigest()
             for name in ['sources.jsonl',*[f'shard-{i+1:02d}.jsonl' for i in range(shards)]]})
    save(out/'prepared.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--shards',type=int,default=4)
    parser.add_argument('--seed',type=int,default=20261001)
    parser.add_argument('--max-bytes',type=int,default=35*1048576)
    # Preserve old launch scripts without retaining metadata quality judgments.
    parser.add_argument('--max-faces',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--min-faces',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--min-materials',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--min-textures',type=int,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.shards<=0 or args.max_bytes<=0:parser.error('shards and max-bytes must be positive')
    print(json.dumps(prepare(args.out,shards=args.shards,seed=args.seed,
                     max_bytes=args.max_bytes,max_faces=args.max_faces,min_faces=args.min_faces,
                     min_materials=args.min_materials,min_textures=args.min_textures),ensure_ascii=False))


if __name__=='__main__':main()
