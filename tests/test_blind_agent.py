import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scene_factory.blind_agent import _request_model, _usage_summary, validate_tool


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        return self.body


class FakeSession:
    def __init__(self):
        self.responses = [FakeResponse(429, {"error": "rate limited"}, {"Retry-After": "0"}),
                          FakeResponse(200, {"choices": [{"message": {"role": "assistant",
                              "tool_calls": []}}], "usage": {"prompt_tokens": 100,
                              "completion_tokens": 10,
                              "prompt_tokens_details": {"cached_tokens": 70}}})]
        self.sent = []

    def post(self, endpoint, **kwargs):
        self.sent.append({"endpoint": endpoint, **kwargs})
        return self.responses.pop(0)


class BlindAgentTests(unittest.TestCase):
    def test_tool_contract_rejects_stale_invalid_and_unknown_evidence(self):
        known = {"frame_0001", "frame_0002"}
        self.assertEqual(validate_tool("orbit", {"observation_id": "frame_0001",
                                                 "yaw": 30, "pitch": 0}, "frame_0002", known),
                         "stale_observation")
        self.assertEqual(validate_tool("focus_at", {"observation_id": "frame_0002",
                                                    "x": float("nan"), "y": 0.5}, "frame_0002", known),
                         "invalid_coordinates")
        self.assertEqual(validate_tool("submit_answer", {"answer": "蓝色", "status": "observed",
                           "evidence_observation_ids": ["frame_9999"]}, "frame_0002", known),
                         "invalid_answer_or_evidence")
        self.assertIsNone(validate_tool("submit_answer", {"answer": "蓝色", "status": "observed",
                          "evidence_observation_ids": ["frame_0002"]}, "frame_0002", known))

    def test_retry_cache_counter_and_64k_output_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            session = FakeSession()
            records = []
            with patch("scene_factory.blind_agent.time.sleep"):
                message = _request_model("https://tokenhub.tencentmaas.com/v1/chat/completions",
                    "dummy-secret", "fake", [{"role": "system", "content": "instructions"}],
                    "stable-cache", time.monotonic() + 30, output, 1, records, session)
            self.assertEqual(message["role"], "assistant")
            self.assertEqual([r["status"] for r in records], [429, 200])
            sent = session.sent[0]
            self.assertEqual(sent["json"]["max_completion_tokens"], 65536)
            self.assertEqual(sent["json"]["prompt_cache_key"], "stable-cache")
            self.assertEqual(sent["headers"]["X-Session-ID"], "stable-cache")
            self.assertEqual(_usage_summary(records)["cache_hit_rate"], 0.7)
            self.assertNotIn("dummy-secret", (output / "usage.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
