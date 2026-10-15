"""Online search recovery, history exclusion and source evidence contracts."""
import json
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scene_factory.sources import DiscoveryError, discover_batch, PATHS_URL
from scene_factory.live_sources import SNAPSHOT_URL


class LiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        parser=patch('scene_factory.live_sources.read_metadata',side_effect=self.read_metadata)
        parser.start()
        self.addCleanup(parser.stop)

    def source(self, number):
        uid = f"{number:032x}"
        keyword=('market','dock','campsite','workshop','living room')[number//10]
        return dict(scene_id=uid, file_name=f"glbs/000-000/{uid}.glb", name=f"{keyword} {number}",
                    source_url=f"https://sketchfab.com/3d-models/{uid}", author_username="artist",
                    license_slug="by", category="GAME_SCENE")

    def fetch(self, url, **kwargs):
        if url == PATHS_URL:
            return gzip.compress(json.dumps({r['scene_id']:f"glbs/000-099/{r['scene_id']}.glb"
                 for r in self.read_metadata(None)}).encode()), {}
        self.assertEqual(url,SNAPSHOT_URL)
        return b'fresh online metadata fixture', {'X-Repo-Commit':'frozen'}

    def read_metadata(self,path):
        return [self.source(index*10+n) for index in range(5) for n in (1,2)]

    def test_live_queries_balance_hints_and_preserve_remote_provenance(self):
        with patch('scene_factory.sources._fetch',side_effect=self.fetch):
            result=discover_batch(self.root,5,provider='prosceneverse-search',page_size=10)
        self.assertEqual([r['environment_hint'] for r in result['rows']],
                         ['outdoor','indoor','outdoor','indoor','outdoor'])
        for row in result['rows']:
            self.assertIn('fetched_at',row['discovery'])
            self.assertTrue(row['url'].startswith('https://huggingface.co/datasets/allenai/objaverse/'))
            self.assertIn('/glbs/000-099/',row['url'])
            self.assertIn('/000-000/',row['file_name'])
            self.assertNotIn('source_glb',row)
            self.assertNotIn('environment',row)  # query hint is not a model label

    def test_history_excluded_and_cursor_resumes_without_rereading_pool(self):
        old=f"{1:032x}"
        with patch('scene_factory.sources._fetch',side_effect=self.fetch):
            first=discover_batch(self.root,5,provider='prosceneverse-search',page_size=10,excluded_uids={old})
        self.assertNotIn(old,{r['uid'] for r in first['rows']})
        with patch('scene_factory.sources._fetch',side_effect=AssertionError('snapshot should suffice')):
            second=discover_batch(self.root,10,first['cursor'],provider='prosceneverse-search',page_size=10,excluded_uids={old})
        self.assertTrue(second['exhausted'])
        self.assertEqual(len({r['uid'] for r in first['rows']+second['rows']}),9)

    def test_resume_uses_this_runs_online_snapshot_during_network_outage(self):
        with patch('scene_factory.sources._fetch',side_effect=self.fetch):
            first=discover_batch(self.root,1,provider='prosceneverse-search',page_size=10)
        before=json.dumps(first['cursor'],sort_keys=True)
        with patch('scene_factory.sources._fetch',side_effect=DiscoveryError('network unavailable')):
            second=discover_batch(self.root,2,first['cursor'],provider='prosceneverse-search',page_size=10)
        self.assertEqual(len(second['rows']),2)
        self.assertEqual(json.dumps(first['cursor'],sort_keys=True),before)

    def test_failed_initial_online_download_is_not_exhaustion(self):
        with patch('scene_factory.sources._fetch',side_effect=DiscoveryError('network unavailable')):
            with self.assertRaises(DiscoveryError):
                discover_batch(self.root,2,provider='prosceneverse-search',page_size=10)

    def test_changed_online_snapshot_is_detected_on_resume(self):
        with patch('scene_factory.sources._fetch',side_effect=self.fetch):
            first=discover_batch(self.root,1,provider='prosceneverse-search',page_size=10)
        next(self.root.glob('*/metadata.parquet')).write_bytes(b'changed snapshot')
        with self.assertRaises(DiscoveryError):
            discover_batch(self.root,2,first['cursor'],provider='prosceneverse-search',page_size=10)

    def test_search_budget_reports_exhaustion_without_claiming_completion(self):
        with patch('scene_factory.sources._fetch',side_effect=self.fetch):
            result=discover_batch(self.root,20,provider='prosceneverse-search',page_size=10,max_pages=1)
        self.assertTrue(result['exhausted'])
        self.assertEqual(result['stats']['exhaustion_reason'],'search_budget')
