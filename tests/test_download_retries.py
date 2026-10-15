"""Permanent missing assets stop quickly; rate limits still retry."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from scene_factory.download import download


class Response:
    def __init__(self,status):
        self.status_code=status
        self.body=b'glTF'+(2).to_bytes(4,'little')+(12).to_bytes(4,'little')
        self.headers={'Content-Length':str(len(self.body))}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError(str(self.status_code),response=self)
    def iter_content(self,*args):yield self.body


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.target=Path(temp.name)/'asset.glb'

    def test_permanent_404_does_not_consume_four_retries(self):
        with patch('scene_factory.download.requests.get',return_value=Response(404)) as get, \
             patch('scene_factory.download.time.sleep') as sleep:
            with self.assertRaises(requests.HTTPError):download('https://example.org/asset.glb',self.target)
        self.assertEqual(get.call_count,1);sleep.assert_not_called()

    def test_429_retries_and_keeps_only_the_complete_glb(self):
        with patch('scene_factory.download.requests.get',side_effect=[Response(429),Response(200)]) as get, \
             patch('scene_factory.download.time.sleep') as sleep:
            result=download('https://example.org/asset.glb',self.target)
        self.assertEqual(get.call_count,2);sleep.assert_called_once()
        self.assertEqual(result['bytes'],12)
        self.assertFalse(self.target.with_suffix('.glb.part').exists())

    def test_connection_failure_is_retryable(self):
        with patch('scene_factory.download.requests.get',side_effect=[requests.ConnectionError('temporary'),Response(200)]) as get, \
             patch('scene_factory.download.time.sleep'):
            download('https://example.org/asset.glb',self.target)
        self.assertEqual(get.call_count,2)

    def test_size_budget_failure_is_not_retried(self):
        with patch('scene_factory.download.requests.get',return_value=Response(200)) as get:
            with self.assertRaises(ValueError):download('https://example.org/asset.glb',self.target,max_bytes=8)
        self.assertEqual(get.call_count,1)
