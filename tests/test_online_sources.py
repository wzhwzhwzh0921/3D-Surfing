import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from scene_factory.sources import (DiscoveryError, OBJAVERSE_BASE, PATHS_URL,
                                   _fetch, discover_batch, normalize_source,
                                   SIZE_FIELDS, source_config, source_fingerprint,
                                   source_size_bytes)


def source(i, category="DESIGNED_ROOM", **extra):
    uid = f"{i:032x}"
    return dict(scene_id=uid, name="Garden room diorama", category=category,
                face_count=12000, material_count=8, texture_count=4,
                license_slug="by", license_url="https://creativecommons.org/licenses/by/4.0/",
                author_username="Artist", source_url=f"https://sketchfab.com/3d-models/{uid}", **extra)


class OnlineSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fake_fetch(self, records, *, fail_offset=None, revisions=None):
        paths = {r["scene_id"]: f"glbs/000-001/{r['scene_id']}.glb" for r in records}

        def fetch(url, **kwargs):
            from urllib.parse import parse_qs, urlparse
            if url == PATHS_URL:
                return gzip.compress(json.dumps(paths).encode()), {}
            query = parse_qs(urlparse(url).query)
            offset, length = int(query["offset"][0]), int(query["length"][0])
            if offset == fail_offset:
                raise DiscoveryError("temporary network outage")
            data = dict(rows=[dict(row=r) for r in records[offset:offset + length]],
                        num_rows_total=len(records))
            return json.dumps(data).encode(), {"X-Revision": (revisions or {}).get(offset, "rev-one")}
        return fetch

    def test_live_join_diversity_resume_attribution_and_cache(self):
        records = [source(1), source(2), source(3, "URBAN_SCENE"), source(4, "GAME_SCENE")]
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)) as fetch:
            first = discover_batch(self.cache, 2, page_size=4)
            self.assertEqual([r["category"] for r in first["rows"]], ["DESIGNED_ROOM", "URBAN_SCENE"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first["rows"][0]["author_username"], "Artist")
            self.assertEqual(first["rows"][0]["url"], OBJAVERSE_BASE + "glbs/000-001/" + f"{1:032x}.glb")
            self.assertEqual(first["rows"][0]["discovery"]["revision"], "rev-one")
        serialized = json.loads(json.dumps(first["cursor"]))
        with patch("scene_factory.sources._fetch", side_effect=AssertionError("should use pending cached rows")):
            second = discover_batch(self.cache, 5, serialized, page_size=4)
            replay = discover_batch(self.cache, 4, page_size=4)
        self.assertEqual(len(second["rows"]), 2)
        self.assertTrue(second["exhausted"])
        self.assertEqual(second["stats"]["exhaustion_reason"], "end_of_source")
        self.assertEqual(len({r["uid"] for r in first["rows"] + second["rows"]}), 4)
        self.assertEqual(len(replay["rows"]), 4)

    def test_failure_keeps_caller_cursor_usable_and_not_exhausted(self):
        records = [source(i) for i in range(1, 5)]
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)):
            first = discover_batch(self.cache, 2, page_size=2)
        before = json.dumps(first["cursor"], sort_keys=True)
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records, fail_offset=2)):
            with self.assertRaises(DiscoveryError):
                discover_batch(self.cache, 2, first["cursor"], page_size=2)
        self.assertEqual(json.dumps(first["cursor"], sort_keys=True), before)
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)):
            resumed = discover_batch(self.cache, 2, first["cursor"], page_size=2)
        self.assertEqual([r["uid"] for r in resumed["rows"]], [f"{3:032x}", f"{4:032x}"])

    def test_metadata_keeps_availability_limits_but_leaves_quality_to_model(self):
        records = [source(1), source(1), source(2), source(3), source(4), source(5)]
        records[3]["face_count"] = 900000
        records[4]["license_slug"] = "by-nc"
        records[5]["name"] = "Skybox panorama"
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)):
            result = discover_batch(self.cache, 9, page_size=6, excluded_uids={f"{2:032x}"})
        self.assertEqual([r["uid"] for r in result["rows"]], [f"{i:032x}" for i in (1, 3, 5)])
        self.assertEqual(result["stats"]["filtered"], dict(duplicate_or_excluded=2, license=1))
        self.assertTrue(all(row['stage'] == 'source_discovered' for row in result['rows']))

    def test_sparse_or_misleading_metadata_cannot_prejudge_scene_quality(self):
        records = [source(1), source(2, 'UNEXPECTED_CATEGORY'), source(3), source(4), source(5)]
        records[0].update(name='Single prop scan test scene', face_count=1, material_count=1, texture_count=0)
        for key in ('face_count', 'material_count', 'texture_count', 'category', 'name'):
            records[2].pop(key)
        records[3].update(category=None, face_count='invalid', material_count=None, texture_count=-1)
        records[4].update(face_count=90000000, material_count=9000, texture_count=0, name='')
        with patch('scene_factory.sources._fetch', side_effect=self.fake_fetch(records)):
            result = discover_batch(self.cache, 9, page_size=5)
        self.assertEqual({row['uid'] for row in result['rows']}, {f'{i:032x}' for i in range(1, 6)})
        self.assertEqual(result['stats']['filtered'], {})
        self.assertEqual(sum(row['category'] == 'UNKNOWN' for row in result['rows']), 2)

    def test_old_quality_filtered_cursor_cannot_resume_under_new_protocol(self):
        records = [source(1)]
        with patch('scene_factory.sources._fetch', side_effect=self.fake_fetch(records)):
            first = discover_batch(self.cache, 1)
        cursor = first['cursor']
        cursor['protocol'] = 'sceneqa-discovery-v1'
        with self.assertRaisesRegex(ValueError, 'different source/configuration'):
            discover_batch(self.cache, 1, cursor)

    def test_new_sources_reach_model_screen_with_mock_api_without_metadata_quality_verdict(self):
        import contextlib
        import io
        from scene_factory.screening import batch, judge, policy, rich
        records = [source(1), source(2)]
        records[0].update(name='Single prop scan', face_count=1, material_count=0, texture_count=0)
        records[1].update(name='Untitled', face_count=90000000, material_count=9000)
        with patch('scene_factory.sources._fetch', side_effect=self.fake_fetch(records)):
            discovered = discover_batch(self.cache, 2)
        captures = self.cache / 'captures'
        for row in discovered['rows']:
            folder = captures / row['uid']; folder.mkdir(parents=True)
            for label in policy.VIEWS:
                (folder / (label + '.png')).write_bytes(b'remote image fixture')
        config = judge.ScreenConfig(captures=captures, output=self.cache / 'screen',
                                    model='fixture-model', url='https://judge.invalid',
                                    version='v4', prompt=policy.prompt_text('v4'))
        def answer(request, **kwargs):
            from unittest.mock import MagicMock
            payload = json.loads(request.data)
            prompt = payload['messages'][0]['content'][0]['text']
            uid = next(row['uid'] for row in discovered['rows'] if row['uid'] in prompt)
            value = dict(uid=uid, action='decide', decision='reject', reason='模型查看实际图后判定内容不足',
                         object_count_lower_bound=0, categories=[], regions=[], exploration_evidence=[])
            reply = MagicMock()
            reply.__enter__.return_value = reply
            reply.read.return_value = json.dumps({'model':'fixture-model', 'choices':[{'message':{'content':json.dumps(value)}}]}).encode()
            return reply
        with patch.object(rich.urllib.request, 'urlopen', side_effect=answer) as http, \
                contextlib.redirect_stdout(io.StringIO()):
            result = batch.run_batch([row['uid'] for row in discovered['rows']], config, 'test-key',
                                     {row['uid']: row for row in discovered['rows']}, workers=1, queue=Path('uids.txt'))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(result['counts'], {'keep':0, 'reject':2})
        self.assertEqual(result['errors'], 0)

    def test_source_revision_change_is_infrastructure_error(self):
        records = [source(i) for i in range(1, 5)]
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records, revisions={2: "rev-two"})):
            result = discover_batch(self.cache, 2, page_size=2)
            with self.assertRaisesRegex(DiscoveryError, "revision changed"):
                discover_batch(self.cache, 2, result["cursor"], page_size=2)

    def test_scan_cap_preserves_pending_and_reason(self):
        records = [source(i) for i in range(1, 10)]
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)):
            first = discover_batch(self.cache, 1, page_size=4, max_scan=3)
            second = discover_batch(self.cache, 9, first["cursor"], page_size=4, max_scan=3)
        self.assertEqual(len(first["rows"] + second["rows"]), 3)
        self.assertTrue(second["exhausted"])
        self.assertEqual(second["stats"]["exhaustion_reason"], "scan_budget")
        self.assertEqual(second["stats"]["scanned"], 3)

    def test_catalog_aliases_resume_fingerprint_and_query(self):
        path = self.cache / "catalog.jsonl"
        records = [dict(uid="garden-one", download_url="https://example.org/one.glb", name="Garden scene"),
                   dict(scene_id="street-two", glb_url="https://example.org/two.glb", name="Street diorama"),
                   dict(uid="../unsafe", url="https://example.org/three.glb")]
        path.write_text("\n".join(json.dumps(r) for r in records))
        with patch("scene_factory.sources._fetch", side_effect=AssertionError("catalog must be offline")):
            first = discover_batch(self.cache, 1, catalog=path, page_size=2)
            second = discover_batch(self.cache, 9, first["cursor"], catalog=path, page_size=2)
            query = discover_batch(self.cache, 9, catalog=path, queries=["street"])
        self.assertEqual(first["rows"][0]["url"], records[0]["download_url"])
        self.assertEqual(second["rows"][0]["uid"], "street-two")
        self.assertTrue(second["exhausted"])
        self.assertEqual([r["uid"] for r in query["rows"]], ["street-two"])
        path.write_text(path.read_text() + "\n{}")
        with self.assertRaisesRegex(ValueError, "different source"):
            discover_batch(self.cache, 1, first["cursor"], catalog=path, page_size=2)

    def test_normalize_rejects_unsafe_uid_and_non_http_urls(self):
        for row in (dict(uid="../../x", url="https://example.org/x.glb"),
                    dict(uid="x", url="file:///tmp/x.glb")):
            with self.assertRaises(ValueError):
                normalize_source(row)
        self.assertEqual(normalize_source(dict(url="https://example.org/derived.glb"))["uid"], "derived")

    def test_uid_boundary_matches_downstream_80_character_contract(self):
        for uid in ("a", "a" * 80, "_scene", "-scene"):
            with self.subTest(uid=uid):
                self.assertEqual(normalize_source(dict(uid=uid, url="https://example.org/x.glb"))["uid"], uid)
        for uid in ("a" * 81, "a.b", "../scene", "scene/child"):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                normalize_source(dict(uid=uid, url="https://example.org/x.glb"))

    def test_all_asset_size_aliases_obey_metadata_budget(self):
        path = self.cache / "sizes.jsonl"
        records = [dict(uid=f"scene-{i}", url="https://example.org/x.glb", **{field: 101})
                   for i, field in enumerate(SIZE_FIELDS)]
        path.write_text("\n".join(json.dumps(r) for r in records))
        result = discover_batch(self.cache, 20, catalog=path, max_bytes=100)
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["exhausted"])
        self.assertEqual(result["stats"]["filtered"]["asset_bytes"], len(SIZE_FIELDS))

    def test_missing_invalid_size_hints_defer_to_actual_download_budget(self):
        for value in (None, True, 'unknown', float('nan'), float('inf'), -1):
            self.assertIsNone(source_size_bytes({'size_bytes':value}))
        self.assertEqual(source_size_bytes({'size_bytes':3, 'glb_size_bytes':'100'}), 100)
        path = self.cache / 'missing-sizes.jsonl'
        path.write_text(json.dumps(dict(uid='scene', url='https://example.org/x.glb', size_bytes='unknown')))
        result = discover_batch(self.cache, 1, catalog=path, max_bytes=100)
        self.assertEqual(len(result['rows']), 1)

    def test_429_and_5xx_retry_then_success(self):
        def response(status, payload=b"{}", headers=None):
            from unittest.mock import MagicMock
            r = MagicMock(status_code=status, headers=headers or {})
            r.__enter__.return_value = r
            r.iter_content.return_value = [payload]
            return r
        responses = [response(429, headers={"Retry-After": "1"}), response(503), response(200)]
        with patch("scene_factory.sources.requests.get", side_effect=responses) as get, \
             patch("scene_factory.sources.time.sleep") as sleep:
            data, _ = _fetch("https://example.org/metadata", max_bytes=100)
        self.assertEqual(data, b"{}")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_network_retry_budget_raises_error(self):
        with patch("scene_factory.sources.requests.get", side_effect=requests.Timeout("down")), \
             patch("scene_factory.sources.time.sleep"):
            with self.assertRaises(DiscoveryError):
                _fetch("https://example.org/metadata", max_bytes=100, retries=1)

    def test_fingerprint_is_predictable_offline_and_config_sensitive(self):
        records = [source(1)]
        with patch("scene_factory.sources._fetch", side_effect=self.fake_fetch(records)):
            batch = discover_batch(self.cache, 1)
        with patch("scene_factory.sources._fetch", side_effect=AssertionError("offline helper")):
            self.assertEqual(source_fingerprint(), batch["source_fingerprint"])
            self.assertEqual(source_config(queries=[" Garden ", "garden"])["queries"], ["garden"])
            self.assertEqual(source_fingerprint(max_faces=400000), batch["source_fingerprint"])
            self.assertNotEqual(source_fingerprint(max_bytes=123456), batch["source_fingerprint"])
        with self.assertRaisesRegex(ValueError, "different source"):
            discover_batch(self.cache, 1, batch["cursor"], queries=["forest"])

    def test_metadata_response_cannot_exceed_download_budget(self):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=200, headers={})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"12345", b"67890"]
        with patch("scene_factory.sources.requests.get", return_value=response):
            with self.assertRaisesRegex(DiscoveryError, "exceeds"):
                _fetch("https://example.org/metadata", max_bytes=9)

    def test_partial_dataset_page_is_not_source_exhaustion(self):
        def fetch(url, **kwargs):
            if url == PATHS_URL:
                return gzip.compress(b"{}"), {}
            return json.dumps(dict(rows=[], num_rows_total=0, partial=True)).encode(), {}
        with patch("scene_factory.sources._fetch", side_effect=fetch):
            with self.assertRaises(DiscoveryError):
                discover_batch(self.cache, 1)


if __name__ == "__main__":
    unittest.main()
