"""Batch counting and recovery tests; no model calls or local geometry loading."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'tools'))
import scene_production_batch as production
import prepare_batch_sources as sources
from scene_factory.automation import save,write_rows


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'sources';self.source.mkdir()
        catalogs=[];hashes={}
        for i in (1,2):
            path=self.source/f'shard-{i:02d}.jsonl';write_rows(path,[])
            hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
            catalogs.append(dict(path=str(path),count=10))
        save(self.source/'prepared.json',dict(catalogs=catalogs,files=hashes,
             configuration=dict(max_bytes=35*1048576)))
        self.signals=patch.object(production.signal,'signal');self.signals.start()
        self.snapshot=patch.object(production,'snapshot_code',return_value=REPO);self.snapshot.start()

    def tearDown(self):self.snapshot.stop();self.signals.stop();self.temp.cleanup()

    def create(self,**options):
        defaults=dict(out=self.root,target=1000,model='fixture-model',url='https://example.org/v1',
            max_repairs=2,parallel=2,batch_size=4,max_restarts=20,retry_delay=60,
            poll_seconds=15,min_free_gib=20,no_delivery_review=True)
        return production.Production(SimpleNamespace(**(defaults|options)))

    def delivery(self,uid,shard=1,source_hash=None):
        folder=self.root/'shards'/f'shard-{shard:02d}'/'case'/uid
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'scene.glb').write_bytes(struct.pack('<4sII',b'glTF',2,12))
        for name in ('start.png','reveal.png'):(folder/name).write_bytes(b'image fixture')
        save(folder/'question.json',dict(question='fixture question',answer='model answer',initial_camera=dict(position=[0,0,1])))
        save(folder/'audit.json',dict(judgment=dict(pass_=True)))
        row=dict(uid=uid,status='keep',folder=str(folder),glb_sha256=source_hash or hashlib.sha256(uid.encode()).hexdigest(),
            delivery_sha256={n:production.file_sha(folder/n) for n in production.DELIVERY},name=uid)
        directory=self.root/'shards'/f'shard-{shard:02d}'
        write_rows(directory/'registry.jsonl',[row]);save(directory/'summary.json',dict(discovered=1,counts=dict(keep=1)))
        return row

    def test_cross_shard_duplicates_do_not_count_twice(self):
        self.delivery('one',1,'same-source-hash');self.delivery('two',2,'same-source-hash')
        result=self.create().aggregate()
        self.assertEqual(result['counts']['keep'],1)
        self.assertEqual(result['duplicates_skipped'],['two'])
        delivered=json.loads((self.root/'keep.jsonl').read_text())
        self.assertTrue((Path(delivered['folder'])/'provenance.json').is_file())

    def test_changed_source_delivery_does_not_count_old_success(self):
        self.delivery('one');p=self.create();p.aggregate()
        source=json.loads((self.root/'shards/shard-01/registry.jsonl').read_text())['folder']
        (Path(source)/'question.json').write_text('{}')
        result=p.aggregate()
        self.assertEqual(result['counts']['keep'],0)
        self.assertEqual(len(result['integrity_errors']),1)

    def test_new_repaired_delivery_replaces_flat_export_and_preserves_old_version(self):
        self.delivery('one');p=self.create();p.aggregate()
        folder=self.root/'repaired';folder.mkdir()
        row=json.loads((self.root/'shards/shard-01/registry.jsonl').read_text())
        for name in production.DELIVERY:
            (folder/name).write_bytes((Path(row['folder'])/name).read_bytes())
        save(folder/'question.json',dict(question='repaired fixture',answer='model answer',initial_camera=dict(position=[0,0,1])))
        row.update(folder=str(folder),delivery_sha256={n:production.file_sha(folder/n) for n in production.DELIVERY})
        result=p.export(row)
        self.assertEqual(json.loads((Path(result['folder'])/'question.json').read_text())['question'],'repaired fixture')
        self.assertEqual(len(list((self.root/'delivery_history/one').iterdir())),1)

    def test_unchanged_native_asset_is_not_rehashed_at_every_progress_poll(self):
        self.delivery('one');p=self.create();p.aggregate()
        original=production.file_sha;assets=[]
        def sha(path):
            if Path(path).name=='scene.glb':assets.append(str(path))
            return original(path)
        with patch.object(production,'file_sha',side_effect=sha):p.aggregate()
        self.assertEqual(assets,[])

    def test_internal_render_identifier_is_rejected_as_an_invalid_answer_format(self):
        row=self.delivery('one');folder=Path(row['folder'])
        save(folder/'question.json',dict(question='fixture question',answer='region_45',initial_camera=dict(position=[0,0,1])))
        row['delivery_sha256']['question.json']=production.file_sha(folder/'question.json')
        write_rows(self.root/'shards/shard-01/registry.jsonl',[row])
        result=self.create().aggregate()
        self.assertEqual(result['counts']['keep'],0);self.assertEqual(len(result['integrity_errors']),1)

    def test_delivery_directory_contains_only_current_final_keeps(self):
        row=self.delivery('one');self.create().aggregate()
        p=self.create(no_delivery_review=False)
        result=p.aggregate()
        self.assertEqual(result['counts']['pending'],1)
        self.assertFalse((self.root/'delivery/one').exists())
        self.assertEqual(len(list((self.root/'delivery_history/one').iterdir())),1)

    def test_missing_audit_does_not_count_old_success(self):
        row=self.delivery('one');(Path(row['folder'])/'audit.json').unlink()
        result=self.create().aggregate()
        self.assertEqual(result['counts']['keep'],0)

    def test_final_review_pending_and_reject_do_not_count_primary_success(self):
        row=self.delivery('one');p=self.create(no_delivery_review=False)
        self.assertEqual(p.aggregate()['counts']['keep'],0)
        self.assertEqual(p.aggregate()['counts']['pending'],1)
        profile=dict(model=p.args.model,url=p.args.url,code_sha256=p.state['delivery_review']['code_sha256'])
        digest=hashlib.sha256(json.dumps(dict(profile=profile,delivery=row['delivery_sha256'],uid='one'),sort_keys=True).encode()).hexdigest()
        path=Path(p.state['delivery_review']['review_dir'])/'one/record.json'
        save(path,dict(profile=profile,fingerprint=digest,status='reject',judgment=dict(reason='unstable camera wording')))
        result=p.aggregate();self.assertEqual(result['counts']['keep'],0);self.assertEqual(result['counts']['reject'],1)

    def test_final_review_pass_is_exported_with_additional_audit(self):
        row=self.delivery('one');p=self.create(no_delivery_review=False)
        profile=dict(model=p.args.model,url=p.args.url,code_sha256=p.state['delivery_review']['code_sha256'])
        digest=hashlib.sha256(json.dumps(dict(profile=profile,delivery=row['delivery_sha256'],uid='one'),sort_keys=True).encode()).hexdigest()
        path=Path(p.state['delivery_review']['review_dir'])/'one/record.json'
        save(path,dict(profile=profile,fingerprint=digest,status='keep',judgment=dict(reason='fixture model review')))
        result=p.aggregate();self.assertEqual(result['counts']['keep'],1)
        self.assertTrue((self.root/'delivery/one/navigation_audit.json').is_file())

    def test_stale_final_review_fingerprint_cannot_pass_new_artifacts(self):
        row=self.delivery('one');p=self.create(no_delivery_review=False)
        profile=dict(model=p.args.model,url=p.args.url,code_sha256=p.state['delivery_review']['code_sha256'])
        save(Path(p.state['delivery_review']['review_dir'])/'one/record.json',dict(profile=profile,fingerprint='stale',status='keep'))
        result=p.aggregate();self.assertEqual(result['counts']['keep'],0);self.assertEqual(result['counts']['pending'],1)

    def test_changed_source_shard_is_rejected_before_launch(self):
        p=self.create();save(p.state_path,p.state)
        (self.source/'shard-01.jsonl').write_text('changed')
        with self.assertRaisesRegex(ValueError,'source shard changed'):self.create()

    def source_pool(self,count=400):
        prepared=json.loads((self.source/'prepared.json').read_text())
        for i,item in enumerate(prepared['catalogs'],1):
            path=Path(item['path'])
            write_rows(path,[dict(uid=f'source-{i}-{n}',name='garden diorama',
                category='DESIGNED_ROOM',url=f'https://example.org/{i}-{n}.glb') for n in range(count)])
            item['count']=count;prepared['files'][path.name]=production.file_sha(path)
        save(self.source/'prepared.json',prepared)
        return prepared

    def test_scaleout_partitions_unprocessed_tails_without_changing_frozen_sources(self):
        original=self.source_pool();p=self.create(scale_out=True,parallel=4)
        self.assertEqual(len(p.workers),4)
        self.assertEqual([w[-1].get('scene_budget') for w in p.workers[:2]],[200,200])
        all_uids=set()
        for _,_,catalog,count,worker in p.workers:
            entries=production.rows(catalog)[:worker.get('scene_budget',count)]
            uids={r['uid'] for r in entries}
            self.assertFalse(all_uids&uids);all_uids|=uids
        self.assertEqual(len(all_uids),800)
        for name,sha in original['files'].items():self.assertEqual(production.file_sha(self.source/name),sha)
        restored=self.create(parallel=4)
        self.assertEqual(len(restored.workers),4)
        self.assertEqual(restored.state['source_partition'],p.state['source_partition'])

    def test_scaleout_refuses_live_source_worker(self):
        self.source_pool();p=self.create();p.state['workers']['1'].update(status='running',pid=123)
        save(p.state_path,p.state)
        with patch.object(production,'alive',return_value=True):
            with self.assertRaisesRegex(ValueError,'Stop source workers'):self.create(scale_out=True)
        self.assertFalse((self.root/'sources_scaleout').exists())

    def test_scaleout_refuses_already_scanned_tail(self):
        self.source_pool();directory=self.root/'shards/shard-01/batches/batch_00001'
        save(directory/'batch.json',dict(discovery=dict(cursor=dict(offset=300))))
        with self.assertRaisesRegex(ValueError,'already scanned'):self.create(scale_out=True)
        self.assertFalse((self.root/'sources_scaleout').exists())

    def test_scaleout_refuses_modified_new_shard_on_resume(self):
        self.source_pool();p=self.create(scale_out=True)
        catalog=p.workers[-1][2];catalog.write_text('changed')
        with self.assertRaisesRegex(ValueError,'Partition source shard changed'):self.create()

    def test_head_worker_keeps_original_source_fingerprint_budgets_after_scaleout(self):
        self.source_pool();p=self.create(scale_out=True)
        child=SimpleNamespace(pid=999)
        with patch.object(production.subprocess,'Popen',return_value=child) as popen:p.launch(*p.workers[0])
        command=popen.call_args.args[0]
        self.assertEqual(command[command.index('--max-scenes')+1],'200')
        self.assertEqual(command[command.index('--max-scan')+1],'401')

    def test_cleanup_only_deletes_deterministically_owned_pods(self):
        p=self.create()
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND':'k8s'}), \
                patch('scene_factory.rendering.command', return_value='') as run:
            p.stop_workers()
        command=run.call_args.args[0]
        names=command[command.index('pod')+1:command.index('--ignore-not-found')]
        self.assertEqual(len(names),2)
        self.assertTrue(all(name.startswith('scene-auto-') and name.endswith('-work') for name in names))

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'),'optional batch dependency')
    def test_prepared_shards_are_disjoint_and_balanced_within_each_category(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        out=self.root/'prepared';metadata=out/'metadata_cache';metadata.mkdir(parents=True)
        records=[];paths={}
        for category,n in [('DESIGNED_ROOM',13),('GAME_SCENE',17),('URBAN_SCENE',9)]:
            for i in range(n):
                uid=f'{category}_{i}';paths[uid]=f'glbs/test/{uid}.glb'
                records.append(dict(scene_id=uid,category=category,name='garden diorama',face_count=2200,
                    material_count=3,texture_count=0,license_slug='by',source_url='https://example.org/source',author_username='fixture'))
        pq.write_table(pa.Table.from_pylist(records),metadata/'metadata-0.parquet')
        reply=json.dumps(dict(parquet_files=[dict(config='default',split='train',
            url='https://huggingface.co/datasets/Stable-X/ProSceneverse/resolve/main/data.parquet')])).encode()
        with patch.object(sources,'_fetch',return_value=(reply,{})),patch.object(sources,'_paths',return_value=(paths,'index-sha')):
            # Retired arguments cannot secretly re-enable old quality heuristics.
            report=sources.prepare(out,shards=4,max_faces=10,min_faces=999999,min_materials=999,min_textures=999)
            repeated=sources.prepare(out,shards=4)
        self.assertEqual(report,repeated);self.assertEqual(report['eligible'],39)
        uid_sets=[]
        for catalog in report['catalogs']:
            uid_sets.append({r['uid'] for r in production.rows(catalog['path'])})
        self.assertEqual(len(set.union(*uid_sets)),sum(map(len,uid_sets)))
        for category in report['categories']:
            counts=[r['categories'].get(category,0) for r in report['catalogs']]
            self.assertLessEqual(max(counts)-min(counts),1)
        self.assertEqual(report['configuration']['quality_screen'], 'visual_model')
        self.assertNotIn('min_faces', report['configuration'])

    @unittest.skipUnless(importlib.util.find_spec('pyarrow'),'optional batch dependency')
    def test_prepare_defers_sparse_metadata_and_unknown_categories_to_visual_screen(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        out=self.root/'model-sources';metadata=out/'metadata_cache';metadata.mkdir(parents=True)
        records=[];paths={}
        for i in range(5):
            uid=f'source_{i}';paths[uid]=f'glbs/test/{uid}.glb'
            records.append(dict(scene_id=uid, category='UNKNOWN_TO_OLD_FILTER', name='Single prop scan',
                                face_count=None, material_count=None, texture_count=None,
                                license_slug='by', source_url='https://example.org/source',author_username='fixture',
                                size_bytes=None))
        records[1].update(category=None,name='',face_count=1,material_count=1,texture_count=0)
        records[2].update(face_count=90000000,material_count=9000)
        records[3]['size_bytes']=36*1048576
        records[4]['license_slug']='by-nc'
        pq.write_table(pa.Table.from_pylist(records),metadata/'metadata-0.parquet')
        reply=json.dumps(dict(parquet_files=[dict(config='default',split='train',
            url='https://huggingface.co/datasets/Stable-X/ProSceneverse/resolve/main/data.parquet')])).encode()
        with patch.object(sources,'_fetch',return_value=(reply,{})),patch.object(sources,'_paths',return_value=(paths,'index-sha')):
            report=sources.prepare(out,shards=2)
        self.assertEqual(report['eligible'],3)
        self.assertEqual(report['filtered'],{'asset_bytes':1,'license':1})
        self.assertEqual({row['uid'] for row in production.rows(out/'sources.jsonl')},
                         {'source_0','source_1','source_2'})
        self.assertIn('UNKNOWN',report['categories'])
        old=json.loads((out/'prepared.json').read_text());old['configuration'].pop('protocol')
        save(out/'prepared.json',old)
        with self.assertRaisesRegex(ValueError,'configuration changed'):
            sources.prepare(out,shards=2)


class RemoteLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(REPO/'experiments/sol_augment'))
        import run
        cls.augment=run

    def test_finished_owned_worker_is_recreated_without_eight_hour_exit(self):
        commands=[]
        remote=None
        def command(args,**kwargs):
            commands.append((args,kwargs))
            if 'get' in args:return json.dumps(dict(status=dict(phase='Succeeded')))
            if '-c' in args:return remote.worker_sha256
            return ''
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND':'k8s','SCENEQA_RENDER_IMAGE':'example/render:v1'}), \
                patch('scene_factory.rendering.command',side_effect=command):
            remote=self.augment.Remote('owned-fixture')
            remote.create()
        self.assertTrue(any('delete' in a for a,_ in commands))
        spec=json.loads(next(k['input'] for a,k in commands if 'apply' in a))
        self.assertEqual(spec['spec']['containers'][0]['command'],['sleep','infinity'])

    def test_remote_temporary_directory_is_cleaned_even_when_geometry_worker_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'fixture.glb';source.write_bytes(b'fixture')
            commands=[]
            def command(args,**kwargs):
                commands.append(args)
                if 'exec' in args and 'python' in args:raise RuntimeError('fixture worker failed')
                return ''
            with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND':'k8s','SCENEQA_RENDER_IMAGE':'example/render:v1'}), \
                    patch('scene_factory.rendering.command',side_effect=command):
                with self.assertRaisesRegex(RuntimeError,'fixture worker failed'):
                    self.augment.Remote('owned-fixture').run(source,Path(temp)/'out')
            cleanups=[a for a in commands if 'rm' in a]
            self.assertEqual(len(cleanups),1);self.assertRegex(cleanups[0][-1],r'^/tmp/qa-[0-9a-f]{16}$')


if __name__=='__main__':unittest.main()
