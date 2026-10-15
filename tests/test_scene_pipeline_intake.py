"""Intake isolation and subprocess contracts; no API calls or 3D processing."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
from scene_factory.screening import intake as pipeline


class ScenePipelineIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.input = self.root / "input.jsonl"
        self.captures = self.root / "captures"
        self.glb = self.root / "fixture.glb"
        self.glb.write_bytes(b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little"))
        self.process_calls = []
        self.screen_results = None
        self.process_patch = patch.object(pipeline.subprocess, "run", side_effect=self.process)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        environment = patch.dict(os.environ, {"LUNA_API_KEY": "mock-api-key"})
        environment.start()
        self.addCleanup(environment.stop)

    def images(self, uid, folder=None):
        folder = (folder or self.captures) / uid
        folder.mkdir(parents=True, exist_ok=True)
        for view in pipeline.VIEWS:
            (folder / f"{view}.png").write_bytes(b"remote image fixture")

    def process(self, command, **kwargs):
        self.process_calls.append((command, kwargs))
        if command[1].endswith("scene_screen_batch.py"):
            queue = Path(command[command.index("--queue") + 1]).read_text().splitlines()
            rows = self.screen_results if self.screen_results is not None else [
                {"uid": uid, "status": "ok", "judgment": {"uid": uid, "decision": "keep", "reason": "mock visible furniture"}}
                for uid in queue]
            summary = self.out / "screen/v4/summary.json"
            summary.parent.mkdir(parents=True, exist_ok=True)
            summary.write_text(json.dumps({"results": rows}))
        return subprocess.CompletedProcess(command, 0)

    def invoke(self, rows, *, reuse=True, extra=()):
        self.input.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows))
        # These frozen intake fixtures exercise the historical v4 protocol.
        args = ["--sources", str(self.input), "--out", str(self.out), "--version", "v4"]
        if reuse:
            args.extend(["--captures", str(self.captures)])
        args.extend(extra)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = pipeline.main(args)
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary, json.loads(output.getvalue().splitlines()[-1]))
        return code, summary

    def rows(self, name):
        return [json.loads(line) for line in (self.out / f"{name}.jsonl").read_text().splitlines()]

    def source(self, uid="good", **metadata):
        glb = self.glb
        if uid != "good":
            import hashlib
            glb = self.root / (hashlib.sha256(uid.encode()).hexdigest()[:16] + ".glb")
            body = uid.encode()
            glb.write_bytes(b"glTF" + (2).to_bytes(4, "little") + (12 + len(body)).to_bytes(4, "little") + body)
        return {"uid": uid, "source_glb": str(glb), "name": "fixture scene", "license": "CC0", **metadata}

    def test_bad_json_uid_duplicate_missing_images_and_oversize_are_independent(self):
        self.images("good")
        entries = ["{", [], self.source(uid="../unsafe"), self.source(), self.source(),
                   self.source(uid="missing"),
                   {"uid": "large", "url": "https://example.invalid/large.glb", "size_bytes": 999999999, "license": "CC-BY"}]
        with patch.object(pipeline, "download") as downloader:
            code, summary = self.invoke(entries)
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["counts"], {"keep": 1, "reject": 1, "failed": 5})
        self.assertEqual(summary["intake_failed"], 4)
        self.assertEqual(summary["render_failed"], 1)
        downloader.assert_not_called()
        self.assertEqual(self.rows("reject")[0]["stage"], "preflight")
        self.assertEqual(self.rows("reject")[0]["license"], "CC-BY")
        self.assertEqual(self.rows("keep")[0]["license"], "CC0")
        self.assertEqual(self.rows("failed")[-1]["name"], "fixture scene")
        command = self.process_calls[0][0]
        self.assertEqual(Path(command[command.index("--sources") + 1]), self.out / "sources.jsonl")
        self.assertEqual((self.out / "uids.txt").read_text(), "good\n")

    def test_zero_valid_inputs_replace_previous_keep_and_emit_summary(self):
        self.out.mkdir()
        for name in ("keep", "reject", "failed"):
            (self.out / f"{name}.jsonl").write_text('{"uid":"stale"}\n')
        code, summary = self.invoke(["broken JSON"])
        self.assertEqual(code, 0)
        self.assertEqual(summary["source_count"], 0)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(self.rows("keep"), [])
        self.assertEqual(self.rows("reject"), [])
        self.assertEqual(len(self.rows("failed")), 1)
        self.assertFalse(self.process_calls)

    def test_empty_manifest_is_successful_empty_batch(self):
        code, summary = self.invoke([])
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["counts"], {"keep": 0, "reject": 0, "failed": 0})
        self.assertFalse(self.process_calls)

    def test_download_http_and_memory_errors_do_not_drop_successful_source(self):
        self.images("good")
        errors = [requests.HTTPError("404"), requests.HTTPError("429"), requests.HTTPError("502"), MemoryError("fixture exhausted")]
        entries = [{"uid": f"bad{i}", "url": f"https://example.invalid/{i}.glb", "name": f"source{i}"}
                   for i in range(len(errors))] + [self.source()]
        with patch.object(pipeline, "download", side_effect=errors):
            code, summary = self.invoke(entries)
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"], {"keep": 1, "reject": 0, "failed": 4})
        self.assertTrue(all(row["stage"] == "intake" and "name" in row for row in self.rows("failed")))

    def test_first_occurrence_reserves_uid_even_if_its_download_fails(self):
        with patch.object(pipeline, "download", side_effect=requests.HTTPError("404")) as downloader:
            code, summary = self.invoke([{"uid": "same", "url": "https://example.invalid/a.glb"}, self.source(uid="same")])
        self.assertEqual(code, 0)
        self.assertEqual(summary["source_count"], 0)
        self.assertEqual(downloader.call_count, 1)
        self.assertIn("duplicate uid", self.rows("failed")[1]["reason"])

    def test_changed_url_for_same_uid_has_distinct_download_cache_path(self):
        paths = []

        def mock_download(url, target, **kwargs):
            paths.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.glb.read_bytes())

        self.images("good")
        with patch.object(pipeline, "download", side_effect=mock_download):
            self.invoke([{"uid": "good", "url": "https://example.invalid/first.glb"}])
            self.invoke([{"uid": "good", "url": "https://example.invalid/second.glb"}])
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(self.rows("keep")[0]["url"], "https://example.invalid/second.glb")

    def test_screen_process_failure_cannot_fall_back_to_old_keep(self):
        self.images("good")
        self.invoke([self.source()])
        self.process_patch.stop()
        with patch.object(pipeline.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["screen"])):
            code, summary = self.invoke([self.source()])
        self.assertEqual(code, 0)
        self.assertEqual(summary["screen_failed"], 1)
        self.assertEqual(self.rows("keep"), [])
        self.assertEqual(self.rows("failed")[0]["stage"], "screen")
        self.assertFalse((self.out / "screen/v4/summary.json").exists())

    def test_nonzero_screen_process_ignores_even_a_valid_written_summary(self):
        self.images("good")
        self.process_patch.stop()

        def process(command, **kwargs):
            self.process(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)

        with patch.object(pipeline.subprocess, "run", side_effect=process):
            code, summary = self.invoke([self.source()])
        self.assertEqual(code, 0)
        self.assertEqual(summary["screen_failed"], 1)
        self.assertEqual(self.rows("keep"), [])

    def test_all_shared_size_metadata_aliases_preflight_without_download(self):
        for field in pipeline.SIZE_FIELDS:
            with self.subTest(field=field), patch.object(pipeline, "download") as downloader:
                code, summary = self.invoke([{"uid": "large", "url": "https://example.invalid/large.glb", field: 999999999}])
            self.assertEqual(code, 0)
            self.assertEqual(summary["preflight_rejected"], 1)
            self.assertEqual(self.rows("reject")[0]["stage"], "preflight")
            downloader.assert_not_called()

    def test_missing_or_malformed_screen_summary_is_explicit_failure(self):
        self.images("good")
        self.process_patch.stop()
        for content in (None, "{", '{"results":null}'):
            def process(command, **kwargs):
                if content is not None:
                    target = self.out / "screen/v4/summary.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                return subprocess.CompletedProcess(command, 0)

            with self.subTest(content=content), patch.object(pipeline.subprocess, "run", side_effect=process):
                code, summary = self.invoke([self.source()])
                self.assertEqual(code, 0)
                self.assertEqual(summary["screen_failed"], 1)
                self.assertEqual(self.rows("keep"), [])

    def test_screen_errors_bad_uid_and_duplicate_results_never_become_keep(self):
        for uid in ("keep", "reject", "http", "json", "duplicate"):
            self.images(uid)
        self.screen_results = [
            {"uid": "keep", "status": "ok", "judgment": {"uid": "keep", "decision": "keep", "reason": "visible furniture"}},
            {"uid": "reject", "status": "ok", "judgment": {"uid": "reject", "decision": "reject", "reason": "single object"}},
            {"uid": "http", "status": "failed", "errors": ["HTTP 502"], "judgment": {"uid": "http", "decision": "keep", "reason": "stale judgment"}},
            {"uid": "json", "status": "ok", "judgment": {"uid": "wrong", "decision": "keep", "reason": "wrong uid"}},
        ] + [{"uid": "duplicate", "status": "ok", "judgment": {"uid": "duplicate", "decision": "keep", "reason": "ambiguous"}}] * 2
        code, summary = self.invoke([self.source(uid=uid) for uid in ("keep", "reject", "http", "json", "duplicate")])
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"], {"keep": 1, "reject": 1, "failed": 3})
        self.assertIn("HTTP 502", self.rows("failed")[0]["reason"])
        self.assertEqual(self.rows("reject")[0]["license"], "CC0")

    def test_partial_render_process_failure_preserves_valid_remote_evidence(self):
        self.process_patch.stop()
        worker_sha = pipeline.stream_sha256(ROOT / "experiments/sol_augment/worker.py")

        def process(command, **kwargs):
            if command[1].endswith("run.py"):
                inspect = self.out / "remote/good/inspect"
                inspect.mkdir(parents=True)
                (inspect / "fingerprint.json").write_text(json.dumps({"source_sha256": pipeline.stream_sha256(self.glb), "worker_sha256": worker_sha}))
                for view in pipeline.VIEWS:
                    (inspect / f"{view}.png").write_bytes(b"remote fixture")
                raise subprocess.CalledProcessError(1, command)
            return self.process(command, **kwargs)

        with patch.object(pipeline.subprocess, "run", side_effect=process):
            code, summary = self.invoke([self.source(), self.source(uid="broken")], reuse=False)
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"], {"keep": 1, "reject": 0, "failed": 1})
        self.assertIn("render subprocess", self.rows("failed")[0]["reason"])
        self.assertEqual((self.out / "uids.txt").read_text(), "good\n")
        self.assertEqual(self.rows("keep")[0]["source_inspect"], str(self.out / "remote/good/inspect"))

    def test_hash_mismatch_removes_old_capture_images_and_does_not_screen(self):
        self.images("good", self.out / "captures")
        inspect = self.out / "remote/good/inspect"
        inspect.mkdir(parents=True)
        (inspect / "fingerprint.json").write_text(json.dumps({"source_sha256": "stale", "worker_sha256": "stale"}))
        code, summary = self.invoke([self.source()], reuse=False)
        self.assertEqual(code, 0)
        self.assertEqual(summary["render_failed"], 1)
        self.assertFalse(any((self.out / "captures/good").iterdir()))
        self.assertEqual(len(self.process_calls), 1)

    def test_key_file_is_normalized_only_into_environment_for_both_stages(self):
        self.process_patch.stop()
        key_file = self.root / "secret"
        key_file.write_text('export LUNA_API_KEY="test-private-secret"\n')
        calls = []

        def process(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(kwargs["env"]["LUNA_API_KEY"], "test-private-secret")
            self.assertNotIn("LUNA_KEY_FILE", kwargs["env"])
            self.assertNotIn("--key-file", command)
            self.assertNotIn("test-private-secret", " ".join(command))
            if command[1].endswith("run.py"):
                inspect = self.out / "remote/good/inspect"
                inspect.mkdir(parents=True)
                (inspect / "fingerprint.json").write_text(json.dumps({"source_sha256": pipeline.stream_sha256(self.glb), "worker_sha256": pipeline.stream_sha256(ROOT / "experiments/sol_augment/worker.py")}))
                for view in pipeline.VIEWS:
                    (inspect / f"{view}.png").write_bytes(b"remote fixture")
                return subprocess.CompletedProcess(command, 0)
            raise RuntimeError("mock process failure: test-private-secret")

        with patch.object(pipeline.subprocess, "run", side_effect=process):
            code, summary = self.invoke([self.source()], reuse=False, extra=["--key-file", str(key_file)])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(summary["screen_failed"], 1)
        for file in self.out.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"test-private-secret", file.read_bytes())
        self.assertIn("[redacted]", self.rows("failed")[0]["reason"])

    def test_luna_key_file_environment_also_normalizes_to_pure_key(self):
        key_file = self.root / "secret"
        key_file.write_text("plain-private-secret\n")
        self.images("good")
        with patch.dict(os.environ, {"LUNA_KEY_FILE": str(key_file)}):
            code, _ = self.invoke([self.source()])
        self.assertEqual(code, 0)
        self.assertEqual(self.process_calls[0][1]["env"]["LUNA_API_KEY"], "plain-private-secret")

    def test_unreadable_manifest_has_state_and_nonzero_configuration_exit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = pipeline.main(["--sources", str(self.input), "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.out / "summary.json").read_text())["status"], "failed")
        self.assertEqual(self.rows("failed")[0]["uid"], "__batch__")
        self.assertEqual(self.rows("keep"), [])

    def test_sha_stream_does_not_use_python311_file_digest(self):
        with patch.object(pipeline.hashlib, "file_digest", create=True, side_effect=AssertionError("Python 3.10 lacks file_digest")):
            self.assertEqual(pipeline.stream_sha256(self.glb), pipeline.hashlib.sha256(self.glb.read_bytes()).hexdigest())

    def test_duplicate_content_aliases_are_rejected_before_screening(self):
        self.images("good")
        code, summary = self.invoke([self.source(), self.source(uid="alias", source_glb=str(self.glb))])
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"], {"keep": 1, "reject": 1, "failed": 0})
        self.assertEqual(self.rows("reject")[0]["reason_code"], "duplicate_content")
        self.assertEqual(self.rows("reject")[0]["glb_sha256"], self.rows("keep")[0]["glb_sha256"])
        self.assertEqual((self.out / "uids.txt").read_text(), "good\n")

    def test_historical_hash_exclusion_is_resumable_without_rendering(self):
        exclude = self.root / "processed-shas.txt"
        exclude.write_text(pipeline.stream_sha256(self.glb) + "\n")
        code, summary = self.invoke([self.source()], reuse=False, extra=["--exclude-shas", str(exclude)])
        self.assertEqual(code, 0)
        self.assertEqual(summary["preflight_rejected"], 1)
        self.assertEqual(self.rows("reject")[0]["reason_code"], "duplicate_content")
        self.assertFalse(self.process_calls)

    def test_unverified_reused_captures_do_not_propagate_source_inspect(self):
        self.images("good")
        code, _ = self.invoke([self.source(source_inspect="unverified")])
        self.assertEqual(code, 0)
        self.assertNotIn("source_inspect", self.rows("keep")[0])

    def test_render_capture_cleanup_error_is_an_asset_failure(self):
        target = self.out / "captures/good" / f"{pipeline.VIEWS[0]}.png"
        target.mkdir(parents=True)
        code, summary = self.invoke([self.source()], reuse=False)
        self.assertEqual(code, 0)
        self.assertEqual(summary["render_failed"], 1)
        self.assertIn("capture cleanup", self.rows("failed")[0]["reason"])

    def test_screen_error_redacts_secret_and_clears_stale_nested_lists(self):
        self.images("good")
        self.screen_results = [{"uid": "good", "status": "failed", "errors": ["mock-api-key"]}]
        nested = self.out / "screen/v4"
        nested.mkdir(parents=True)
        for name in ("keep", "reject", "failed"):
            (nested / f"{name}.jsonl").write_text('{"uid":"stale"}\n')
        code, summary = self.invoke([self.source()])
        self.assertEqual(code, 0)
        self.assertEqual(summary["screen_failed"], 1)
        self.assertIn("[redacted]", self.rows("failed")[0]["reason"])
        self.assertTrue(all(not (nested / f"{name}.jsonl").exists() for name in ("keep", "reject", "failed")))


if __name__ == "__main__":
    unittest.main()
