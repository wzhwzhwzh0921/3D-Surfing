"""Screening refactor regressions, using mock HTTP and remote image fixtures."""
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scene_factory.screening import batch, judge, policy


class Response:
    def __init__(self, uid, decision="keep"):
        self.body = {"model": "fixture-model", "choices": [{"message": {"content": json.dumps({
            "uid": uid, "decision": decision, "reason": "实际图像中的场景判断"})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                      "prompt_tokens_details": {"cached_tokens": 50}}}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.body).encode()


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = judge.ScreenConfig(
            captures=self.root / "captures", output=self.root / "results",
            model="fixture-model", url="https://judge.invalid/v1/chat/completions",
            version="v3", prompt=policy.prompt_text("v3"))
        self.uid = "legacy-cache"
        self.images(self.uid)

    def images(self, uid):
        folder = self.config.captures / uid
        folder.mkdir(parents=True)
        for view in policy.VIEWS:
            (folder / f"{view}.png").write_bytes(b"remote image fixture")

    def legacy_cache(self, config):
        # Captured from the original script with mock HTTP, before the cleanup.
        fixture = Path(__file__).with_name("fixtures") / "screening-v3-legacy-record.json"
        folder = config.output / config.version / self.uid
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, folder / "record.json")
        return folder

    def test_reuses_original_successful_cache_without_http(self):
        self.legacy_cache(self.config)
        with patch.object(judge.urllib.request, "urlopen", side_effect=AssertionError("cache miss")):
            record = judge.judge_scene(self.uid, self.config, "mock-key", {"name": "new metadata"})
        self.assertTrue(record["cached"])
        self.assertEqual(record["judgment"]["decision"], "keep")
        self.assertEqual(record["source"], {"name": "new metadata"})

    def test_changed_protocol_or_images_does_not_reuse_old_opinion(self):
        variants = [{"prompt": self.config.prompt + "新版"}, {"model": "other-model"},
                    {"url": "https://other.invalid/v1"}, {"force": True}, {}]
        for index, changes in enumerate(variants):
            with self.subTest(changes=changes):
                config = replace(self.config, output=self.root / f"out-{index}", **changes)
                folder = self.legacy_cache(config)
                if not changes:
                    (config.captures / self.uid / f"{policy.VIEWS[0]}.png").write_bytes(b"changed image")
                with patch.object(judge.urllib.request, "urlopen", return_value=Response(self.uid)) as request:
                    record = judge.judge_scene(self.uid, config, "mock-key", {})
                self.assertEqual(request.call_count, 1)
                self.assertFalse(record["cached"])
                self.assertEqual(len(list(folder.glob("attempt_*/record.json"))), 1)

    def test_rate_limit_retries_and_saves_original_error(self):
        error = urllib.error.HTTPError(self.config.url, 429, "rate limited", {"Retry-After": "7"}, io.BytesIO(b"rate limited"))
        with patch.object(judge.urllib.request, "urlopen", side_effect=[error, Response(self.uid)]) as request, \
                patch.object(judge.time, "sleep") as sleep:
            record = judge.judge_scene(self.uid, self.config, "mock-key", {})
        self.assertEqual((record["status"], record["attempts"], request.call_count), ("ok", 2, 2))
        sleep.assert_called_once_with(7)
        folder = self.config.output / "v3" / self.uid
        self.assertEqual((folder / "error_1.json").read_bytes(), b"rate limited")
        self.assertTrue((folder / "raw_2.json").is_file())

    def test_invalid_answer_stays_failed_after_three_attempts(self):
        with patch.object(judge.urllib.request, "urlopen", return_value=Response("wrong-uid")) as request, \
                patch.object(judge.time, "sleep"):
            record = judge.judge_scene(self.uid, self.config, "mock-key", {})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("judgment", record)

    def test_batch_separates_decisions_failures_and_cache_accounting(self):
        self.images("reject-scene")
        uids = [self.uid, "missing-scene", "reject-scene"]
        sources = {self.uid: {"name": "metadata must not become visual evidence"}}

        def respond(request, **kwargs):
            payload = json.loads(request.data)
            user_text = payload["messages"][0]["content"][0]["text"]
            self.assertNotIn(sources[self.uid]["name"], user_text)
            uid = user_text.rsplit('："', 1)[1].split('"', 1)[0]
            return Response(uid, "reject" if uid == "reject-scene" else "keep")

        with patch.object(judge.urllib.request, "urlopen", side_effect=respond) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            first = batch.run_batch(uids, self.config, "mock-key", sources, workers=2, queue=Path("uids.txt"))
            second = batch.run_batch(uids, self.config, "mock-key", sources, workers=2, queue=Path("uids.txt"))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(first["counts"], {"keep": 1, "reject": 1})
        self.assertEqual(first["errors"], 1)
        self.assertEqual(first["provider_cached_tokens"], 100)
        self.assertEqual(second["cached_scene_count"], 2)
        self.assertEqual(second["new_token_totals"]["total_tokens"], 0)
        self.assertNotIn("provider_cached_tokens", second)
        self.assertEqual([row["uid"] for row in second["results"]], uids)
        for name, expected in (("keep", self.uid), ("reject", "reject-scene"), ("failed", "missing-scene")):
            rows = [json.loads(line) for line in (self.config.output / "v3" / f"{name}.jsonl").read_text().splitlines()]
            self.assertEqual([row["uid"] for row in rows], [expected])
        self.assertTrue((self.config.captures / "reject-scene").is_dir())
        self.assertTrue((self.config.output / "v3/gallery.html").is_file())


if __name__ == "__main__":
    unittest.main()
