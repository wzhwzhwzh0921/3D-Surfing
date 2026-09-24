from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

import requests

from scene_factory.openai_bridge import bridge, from_openai, to_openai


class BridgeTests(unittest.TestCase):
    def test_tool_and_image_translation(self):
        request = {"model": "sonnet", "stream": True, "system": "Use tools.",
                   "tools": [{"name": "Bash", "description": "Run a command",
                              "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
                   "messages": [
                       {"role": "user", "content": [{"type": "text", "text": "inspect"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}]},
                       {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1",
                            "name": "Bash", "input": {"command": "pwd"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "ok"},
                                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}]}]}
                   ]}
        x = to_openai(request, "deepseek/deepseek-flash", 65536)
        self.assertEqual(x["model"], "deepseek/deepseek-flash")
        self.assertEqual(x["max_completion_tokens"], 65536)
        self.assertEqual(x["messages"][2]["tool_calls"][0]["function"]["name"], "Bash")
        self.assertEqual(x["messages"][3]["role"], "tool")
        self.assertEqual(x["messages"][4]["role"], "user")
        self.assertTrue(x["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_response_translation(self):
        response = {"id": "abc", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 80}}
        value = from_openai(response, "deepseek/deepseek-flash")
        self.assertEqual(value["stop_reason"], "tool_use")
        self.assertEqual(value["content"][0]["input"]["command"], "pwd")
        self.assertEqual(value["usage"]["cache_read_input_tokens"], 80)

    def test_retry_429_and_record_cache(self):
        class Upstream(BaseHTTPRequestHandler):
            attempts = 0

            def log_message(self, *_):
                pass

            def do_POST(self):
                Upstream.attempts += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                if Upstream.attempts == 1:
                    self.send_response(429)
                    self.end_headers()
                    return
                value = {"id": "abc", "choices": [{"finish_reason": "stop",
                         "message": {"content": "OK"}}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 2,
                                   "prompt_cache_hit_tokens": 70}}
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                log = Path(temp) / "usage.jsonl"
                with bridge(endpoint=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
                            key="private", model="deepseek/deepseek-flash", output=log,
                            max_tokens=65536, retry_delays=(0,), timeout=5) as proxy:
                    response = requests.post(proxy.url + "/v1/messages",
                                             headers={"x-api-key": proxy.token},
                                             json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}],
                                                   "stream": False}, timeout=10)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["content"][0]["text"], "OK")
                rows = [json.loads(x) for x in log.read_text().splitlines()]
                self.assertEqual([x["status"] for x in rows], [429, 200])
                self.assertEqual(rows[-1]["cached_tokens"], 70)
                self.assertNotIn("private", log.read_text())
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
