"""One command: discover online scenes, screen, generate medium QA, audit and repair."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scene_factory.automation import locked_campaign


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--target',type=int,default=100,help='final model-reviewed medium QA deliveries')
    p.add_argument('--provider',choices=('prosceneverse-search','prosceneverse','catalog'),default='prosceneverse-search')
    p.add_argument('--catalog',type=Path,help='optional existing JSONL GLB source pool')
    p.add_argument('--exclude-history',type=Path,action='append',default=[],
                   help='historical JSONL UIDs to exclude; used only as a denylist, never as sources')
    p.add_argument('--min-outdoor',type=int,help='minimum final outdoor deliveries, confirmed from rendered evidence')
    p.add_argument('--min-indoor',type=int,help='minimum final indoor deliveries')
    p.add_argument('--screen-version',choices=('v4','v5'),default='v5')
    p.add_argument('--query',dest='queries',action='append',default=[],help='metadata keyword, repeated arguments are OR')
    p.add_argument('--batch-size',type=int,default=4)
    p.add_argument('--max-scenes',type=int,default=5000)
    p.add_argument('--max-scan',type=int,default=10000)
    p.add_argument('--max-pages',type=int,default=100)
    p.add_argument('--page-size',type=int,default=100)
    p.add_argument('--max-faces',type=int,help=argparse.SUPPRESS)  # retired, compatibility only
    p.add_argument('--max-bytes',type=int,default=64*1048576)
    p.add_argument('--max-repairs',type=int,default=2)
    p.add_argument('--max-failed-batches',type=int,default=3)
    p.add_argument('--stage-timeout',type=int,default=1800)
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--single-worker',action='store_true',help='reuse one GPU pod for sequential screening and QA')
    p.add_argument('--discovery-cache',type=Path)
    p.add_argument('--key-file',type=Path)
    p.add_argument('--retry-failed',action='store_true',help='retry saved processing failures; quality rejects stay terminal')
    p.add_argument('--model',default=os.getenv('LUNA_MODEL','gpt-6.1-sol'))
    p.add_argument('--url',default=os.getenv('LUNA_CHAT_URL','http://127.0.0.1:18773/v1/chat/completions'))
    a=p.parse_args()
    if a.catalog:a.provider='catalog'
    balanced = a.provider=='prosceneverse-search'
    if a.min_outdoor is None:a.min_outdoor=(a.target+1)//2 if balanced else 0
    if a.min_indoor is None:a.min_indoor=a.target//2 if balanced else 0
    if min(a.min_indoor,a.min_outdoor)<0 or a.min_indoor+a.min_outdoor>a.target:
        p.error('environment minimums must be nonnegative and sum to at most target')
    if a.screen_version!='v5' and (a.min_indoor or a.min_outdoor):
        p.error('environment quotas require v5 visual labels')
    if a.provider=='catalog' and not a.catalog:p.error('--provider catalog requires --catalog')
    for name in ('target','batch_size','max_scenes','max_scan','max_pages','page_size','max_bytes','max_failed_batches','stage_timeout','workers'):
        if getattr(a,name)<=0:p.error(name+' must be positive')
    if a.max_repairs<0 or a.page_size>100:p.error('max-repairs must be nonnegative; page-size <= 100')
    if a.key_file:a.key_file=a.key_file.resolve()
    result=locked_campaign(a)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if result['status']=='failed':raise SystemExit(1)


if __name__=='__main__':main()
