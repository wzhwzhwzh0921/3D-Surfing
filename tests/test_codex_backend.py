"""Check routing, image attachments, and authentication without model calls."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from scene_factory import codex_backend as backend


class CodexBackendTests(unittest.TestCase):
    def test_subscription_environment_drops_api_and_access_token_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "api-secret", "CODEX_API_KEY": "other-key",
                                     "CODEX_ACCESS_TOKEN": "access-secret", "OPENAI_BASE_URL": "relay",
                                     "SCENEQA_CODEX_HOME": "/tmp/test-sceneqa-codex"}):
            env = backend.codex_environment()
        self.assertFalse(any(name in env for name in backend.DIRECT_CREDENTIALS))
        self.assertEqual(env["CODEX_HOME"], "/tmp/test-sceneqa-codex")

    def test_images_keep_their_order_and_labels(self):
        images = [b"first-image-bytes", b"second-image-bytes"]
        parts = []
        for index, data in enumerate(images):
            parts.extend([{"type": "text", "text": f"view {index}"}, {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()}}])
        with tempfile.TemporaryDirectory() as directory:
            prompt, attached = backend.prepare_messages([{"role": "user", "content": parts}], directory)
            self.assertEqual([p.read_bytes() for p in attached], images)
        self.assertLess(prompt.index("view 0"), prompt.index("[attached image 1]"))
        self.assertLess(prompt.index("[attached image 1]"), prompt.index("view 1"))
        self.assertNotIn(base64.b64encode(images[0]).decode(), prompt)

    def test_remote_image_urls_are_not_silently_dropped_or_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                backend.prepare_messages([{"role": "user", "content": [{"type": "image_url",
                    "image_url": {"url": "https://example.org/image.png"}}]}], directory)

    def test_local_gateway_requires_the_local_token_and_preserves_completion(self):
        class Fake:
            status = {"account_fingerprint": "account-test"}
            def complete(self, payload):
                return {"choices": [{"message": {"content": json.dumps(payload["messages"])}}]}
        with backend.gateway(Fake(), port=0) as (url, token):
            payload = {"messages": [{"role": "user", "content": "test"}]}
            request = urllib.request.Request(url, data=json.dumps(payload).encode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 401)
            request.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read())
        self.assertEqual(json.loads(result["choices"][0]["message"]["content"]), payload["messages"])

    def test_cli_response_records_real_usage_and_cached_tokens(self):
        status = {"binary": "codex-test", "version": "test", "auth_method": "chatgpt",
                  "account_fingerprint": "test-account"}
        transport = backend.CodexBackend("gpt-6.1-sol", status=status)
        class FakeProcess:
            returncode = 0
            def communicate(self, prompt, timeout):
                self.prompt = prompt
                return json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20,
                    "output_tokens": 3, "cached_input_tokens": 10}}), ""
        def start(command, **kwargs):
            path = Path(command[command.index("--output-last-message") + 1])
            path.write_text(json.dumps({"content": '{"decision":"keep"}'}))
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            return FakeProcess()
        with patch.object(backend.subprocess, "Popen", start):
            result = transport.complete({"model": "gpt-6.1-sol", "messages": [
                {"role": "user", "content": "Return JSON"}], "response_format": {"type": "json_object"}})
        self.assertEqual(result["usage"]["total_tokens"], 23)
        self.assertEqual(result["usage"]["prompt_tokens_details"]["cached_tokens"], 10)
        self.assertEqual(json.loads(result["choices"][0]["message"]["content"])["decision"], "keep")

    def test_busy_codex_returns_retryable_429(self):
        class Fake:
            status = {"account_fingerprint": "test"}
            def complete(self, payload):
                raise BlockingIOError("busy")
        with backend.gateway(Fake(), port=0) as (url, token):
            request = urllib.request.Request(url, data=b'{}', headers={"Authorization": "Bearer " + token})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 429)
            self.assertEqual(error.exception.headers["Retry-After"], "5")

    def test_workflow_default_routes_to_codex_and_relay_is_explicit(self):
        from scene_factory import workflow
        with patch.dict(os.environ, {}, clear=True), patch.object(backend, "launch", return_value=0) as launch:
            self.assertEqual(workflow.main(["auto", "--out", "/tmp/not-created"]), 0)
            launch.assert_called_once()
        with patch.dict(os.environ, {"SCENEQA_MODEL_BACKEND": "relay", "SCENEQA_API_URL": "https://example.invalid/v1/chat/completions"}, clear=True), \
                patch.object(backend, "launch") as launch, patch.object(workflow.runpy, "run_path"):
            workflow.main(["auto", "--out", "/tmp/not-created"])
            launch.assert_not_called()

    def test_launch_overrides_old_endpoint_and_key_without_changing_parent_environment(self):
        import contextlib
        class Fake:
            def __init__(self, model):
                self.model = model
                self.reasoning = "xhigh"
                self.status = {"version": "codex-test"}
        @contextlib.contextmanager
        def fake_gateway(*args, **kwargs):
            yield "http://127.0.0.1:1/codex/test/v1/chat/completions", "local-token"
        with patch.dict(os.environ, {"LUNA_CHAT_URL": "https://old-relay", "LUNA_KEY_FILE": "old-key",
                                     "LUNA_API_KEY": "old-token"}, clear=True), \
                patch.object(backend, "CodexBackend", Fake), patch.object(backend, "gateway", fake_gateway), \
                patch.object(backend.subprocess, "call", return_value=0) as call:
            backend.launch(["auto", "--out", "/tmp/not-created", "--url", "https://old-relay"])
            args, kwargs = call.call_args
            self.assertEqual(kwargs["env"]["LUNA_API_KEY"], "local-token")
            self.assertNotIn("LUNA_KEY_FILE", kwargs["env"])
            self.assertEqual(args[0][-5:], ["http://127.0.0.1:1/codex/test/v1/chat/completions",
                                           "--model", "gpt-6.1-sol", "--workers", "1"])
            self.assertEqual(os.environ["LUNA_API_KEY"], "old-token")

    def test_followup_wait_budget_respects_explicit_override(self):
        import contextlib
        class Fake:
            def __init__(self, model):
                self.model=model;self.reasoning="xhigh";self.status={"version":"codex-test"}
                self.timeout=int(os.getenv("SCENEQA_CODEX_TIMEOUT","240"))
        observed=[]
        @contextlib.contextmanager
        def fake_gateway(instance, **kwargs):
            observed.append(instance.timeout)
            yield "http://127.0.0.1:1/codex/test/v1/chat/completions","local-token"
        for setting,expected in [({},480),({"SCENEQA_CODEX_TIMEOUT":"330"},330)]:
            with patch.dict(os.environ,setting,clear=True),patch.object(backend,"CodexBackend",Fake), \
                    patch.object(backend,"gateway",fake_gateway),patch.object(backend.subprocess,"call",return_value=0):
                backend.launch(["followup","--campaign","/tmp/not-created","--out","/tmp/not-created"])
                self.assertEqual(observed[-1],expected)


if __name__ == "__main__":
    unittest.main()
