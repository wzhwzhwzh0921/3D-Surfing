"""Cross-source recovery, attribution, fair retrieval and shared budget contracts."""
from collections import Counter
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scene_factory.live_sources import LIVE_PROFILES, SEARCH_GROUPS
from scene_factory.sources import DiscoveryError, PATHS_URL, discover_batch, source_config


def source(number, **changes):
    uid = f'{number:032x}'
    return dict(scene_id=uid, name='Market workshop scene',
        caption='A detailed courtyard with tools and shelves', tags=['market'],
        file_name=f'glbs/999-999/{uid}.glb', category='ENVIRONMENT_SCAN',
        author_username='original-artist', source_url=f'https://sketchfab.com/3d-models/{uid}',
        license_slug='by-sa', license_url='https://creativecommons.org/licenses/by-sa/4.0/',
        decision='REMOVE', **changes)


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = {'prosceneverse-search': [source(i) for i in (1, 2, 3)],
                        'scansceneverse-search': [source(i) for i in (2, 11, 12, 13)]}
        self.reader = patch('scene_factory.live_sources.read_metadata', side_effect=self.read)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def read(self, path):
        return self.records[Path(path).read_text()]

    def fetch(self, url, **kwargs):
        if url == PATHS_URL:
            index = {row['scene_id']: f"glbs/000-009/{row['scene_id']}.glb"
                     for records in self.records.values() for row in records}
            return gzip.compress(json.dumps(index).encode()), {}
        name = next(key for key, profile in LIVE_PROFILES.items() if profile['metadata_url'] == url)
        return name.encode(), {'X-Repo-Commit': 'a' * 40}

    def discover(self, limit=40, cursor=None, **changes):
        options = dict(provider='multi-source', page_size=10, max_scan=1000, max_pages=100)
        options.update(changes)
        return discover_batch(self.root, limit, cursor, **options)

    def test_round_robin_and_cross_source_dedup_keep_original_rights_and_paths(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            result = self.discover()
        self.assertEqual(len(result['rows']), 6)
        self.assertTrue(result['exhausted'])
        self.assertEqual(result['stats']['exhaustion_reason'], 'end_of_source')
        self.assertEqual([row['discovery']['provider'] for row in result['rows'][:4]],
                         ['prosceneverse-search','scansceneverse-search'] * 2)
        for row in result['rows']:
            self.assertIn('/glbs/000-009/', row['url'])
            self.assertEqual(row['license_slug'], 'by-sa')
            self.assertEqual(row['author_username'], 'original-artist')
            self.assertNotIn('environment', row)
            # An upstream designed-vs-scanned classifier is not our quality judge.
            self.assertEqual(row['decision'], 'REMOVE')
        self.assertEqual(len({row['uid'] for row in result['rows']}), 6)

    def test_offline_resume_and_new_exclusions_do_not_mutate_previous_cursor(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            first = self.discover(2)
        before = json.dumps(first['cursor'], sort_keys=True)
        with patch('scene_factory.sources._fetch', side_effect=AssertionError('use run snapshot')):
            rest = self.discover(cursor=first['cursor'], excluded_uids={f'{12:032x}'})
        self.assertEqual(before, json.dumps(first['cursor'], sort_keys=True))
        self.assertNotIn(f'{12:032x}', {row['uid'] for row in rest['rows']})
        self.assertEqual(len(first['rows'] + rest['rows']), 5)

    def test_one_source_failure_is_not_exhaustion_or_silent_fallback(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            first = self.discover(1)
        before = json.dumps(first['cursor'], sort_keys=True)
        with patch('scene_factory.sources._fetch', side_effect=DiscoveryError('429 temporary')):
            with self.assertRaises(DiscoveryError):
                self.discover(cursor=first['cursor'])
        self.assertEqual(before, json.dumps(first['cursor'], sort_keys=True))

    def test_aggregate_scan_and_page_budgets_are_not_multiplied_per_source(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            result = self.discover(max_scan=4, max_pages=2)
        self.assertTrue(result['exhausted'])
        self.assertEqual(result['stats']['exhaustion_reason'], 'search_budget')
        self.assertLessEqual(result['stats']['scanned'], 4)
        self.assertLessEqual(result['stats']['pages'], 2)

    def test_tiny_window_is_budget_exhaustion_not_end_of_both_sources(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            result = self.discover(max_scan=1, max_pages=1)
        self.assertTrue(result['exhausted'])
        self.assertEqual(result['stats']['exhaustion_reason'], 'search_budget')

    def test_source_and_query_changes_cannot_resume_a_different_cursor(self):
        with patch('scene_factory.sources._fetch', side_effect=self.fetch):
            first = self.discover(1)
        with self.assertRaisesRegex(ValueError, 'another source'):
            self.discover(cursor=first['cursor'], queries=['yard'])

    def test_expanded_buckets_balance_environments_and_add_complementary_types(self):
        self.assertEqual(len(SEARCH_GROUPS), 20)
        self.assertEqual(Counter(name.split('_')[0] for name, _ in SEARCH_GROUPS),
                         {'indoor': 10, 'outdoor': 10})
        terms = {word for _, words in SEARCH_GROUPS for word in words}
        self.assertTrue({'alley', 'junkyard', 'farmyard', 'train station', 'bookstore'} <= terms)
        config = source_config(provider='multi-source', max_scan=51, max_pages=11)
        self.assertEqual(sum(child['max_scan'] for child in config['sources']), 51)
        self.assertEqual(sum(child['max_pages'] for child in config['sources']), 11)
