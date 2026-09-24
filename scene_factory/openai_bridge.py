"""Translate Claude Code's Anthropic Messages API into OpenAI Chat Completions.

Used for TokenHub, which exposes /v1/chat/completions rather than /v1/messages.
The proxy binds to loopback only and never records request bodies or credentials.
"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests

from .gateway import RETRYABLE, normalize_usage


def _text(blocks):
    if isinstance(blocks, str):
        return blocks
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _image(block):
    src = block.get("source", {})
    if src.get("type") == "base64":
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}}
    if src.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    raise ValueError("Unsupported image source in Claude tool result")


def to_openai(payload, model, max_tokens):
    system = _text(payload.get("system", ""))
    messages = [{"role": "system", "content": system}] if system else []
    for m in payload.get("messages", []):
        blocks = m.get("content", "")
        if isinstance(blocks, str):
            messages.append({"role": m["role"], "content": blocks})
            continue
        if m["role"] == "assistant":
            content = _text(blocks)
            tool_calls = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    tool_calls.append({"id": b["id"], "type": "function",
                                       "function": {"name": b["name"],
                                                    "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}})
            message = {"role": "assistant", "content": content or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
        else:
            user_parts = []
            result_images = []
            for b in blocks:
                typ = b.get("type")
                if typ == "text":
                    user_parts.append({"type": "text", "text": b.get("text", "")})
                elif typ == "image":
                    user_parts.append(_image(b))
                elif typ == "tool_result":
                    result = b.get("content", "")
                    messages.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                     "content": _text(result)})
                    if isinstance(result, list):
                        result_images.extend(_image(x) for x in result if x.get("type") == "image")
            if user_parts:
                messages.append({"role": "user", "content": user_parts})
            if result_images:
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Image returned by the preceding tool:"}, *result_images]})
    tools = [{"type": "function", "function": {"name": t["name"],
              "description": t.get("description", ""), "parameters": t.get("input_schema", {"type": "object"})}}
             for t in payload.get("tools", []) if t.get("name")]
    outgoing = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
                "stream": bool(payload.get("stream")), "user": "scene-qa-factory"}
    if tools:
        outgoing["tools"] = tools
        outgoing["tool_choice"] = "auto"
    if outgoing["stream"]:
        outgoing["stream_options"] = {"include_usage": True}
    return outgoing


def from_openai(value, model):
    choice = (value.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        content.append({"type": "tool_use", "id": call.get("id") or secrets.token_hex(12),
                        "name": function.get("name", "unknown"), "input": arguments})
    reason = choice.get("finish_reason")
    stop = "tool_use" if message.get("tool_calls") else "max_tokens" if reason == "length" else "end_turn"
    usage = value.get("usage") or {}
    return {"id": value.get("id", "msg_" + secrets.token_hex(10)), "type": "message",
            "role": "assistant", "model": model, "content": content, "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0),
                      "cache_read_input_tokens": usage.get("prompt_cache_hit_tokens",
                                                       (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))}}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endpoint, key, model, output, max_tokens, retry_delays, timeout):
        super().__init__(("127.0.0.1", 0), Handler)
        self.endpoint, self.key, self.model = endpoint, key, model
        self.output, self.max_tokens = Path(output), max_tokens
        self.retry_delays, self.timeout = retry_delays, timeout
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def record(self, row):
        with self.lock, self.output.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def authorized(self):
        supplied = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        return secrets.compare_digest(supplied, self.server.token)

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            return self.send_json(401, {"error": {"type": "authentication_error", "message": "Unauthorized"}})
        if urlsplit(self.path).path == "/v1/models":
            return self.send_json(200, {"data": [{"id": self.server.model, "type": "model"}], "has_more": False})
        return self.send_json(404, {"error": {"type": "not_found_error", "message": "Unknown route"}})

    def event(self, kind, value):
        self.wfile.write(f"event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": {"type": "authentication_error", "message": "Unauthorized"}})
        path = urlsplit(self.path).path
        if path not in ("/v1/messages", "/v1/messages/count_tokens"):
            return self.send_json(404, {"error": {"type": "not_found_error", "message": "Unknown route"}})
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except (ValueError, TypeError):
            return self.send_json(400, {"error": {"type": "invalid_request_error", "message": "Invalid JSON"}})
        if path.endswith("count_tokens"):
            # Claude Code may ask for a planning estimate. This is never used in billing reports.
            return self.send_json(200, {"input_tokens": max(1, len(json.dumps(payload)) // 4)})
        server = self.server
        try:
            body = to_openai(payload, server.model, server.max_tokens)
        except (ValueError, KeyError) as exc:
            return self.send_json(400, {"error": {"type": "invalid_request_error", "message": str(exc)}})
        metadata = {"protocol": "tokenhub-openai-bridge", "model": server.model,
                    "requested_model": payload.get("model"), "max_output_tokens": server.max_tokens,
                    "message_count": len(payload.get("messages", [])),
                    "tool_names": [t.get("name") for t in payload.get("tools", [])],
                    "request_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                    "stream": bool(body["stream"])}
        response = None
        for attempt in range(len(server.retry_delays) + 1):
            started = time.time()
            record = {**metadata, "time": started, "attempt": attempt + 1, "complete": False}
            try:
                response = requests.post(server.endpoint, json=body,
                                         headers={"Authorization": f"Bearer {server.key}",
                                                  "Content-Type": "application/json"},
                                         stream=body["stream"], timeout=(30, server.timeout))
            except requests.RequestException as exc:
                server.record({**record, "status": None, "error": type(exc).__name__,
                               "elapsed": time.time() - started})
                if attempt == len(server.retry_delays):
                    return self.send_json(502, {"error": {"type": "api_error", "message": "Upstream connection failed"}})
                time.sleep(server.retry_delays[attempt])
                continue
            if response.status_code in RETRYABLE and attempt < len(server.retry_delays):
                retry_after = response.headers.get("retry-after", "0")
                server.record({**record, "status": response.status_code, "elapsed": time.time() - started})
                response.close()
                try:
                    delay = max(server.retry_delays[attempt], min(float(retry_after), 300))
                except ValueError:
                    delay = server.retry_delays[attempt]
                time.sleep(delay)
                continue
            break
        if response.status_code != 200:
            status = response.status_code
            response.close()
            server.record({**record, "status": status, "elapsed": time.time() - started})
            return self.send_json(status, {"error": {"type": "api_error", "message": f"Upstream HTTP {status}"}})
        usage = {}
        completed = False
        try:
            if body["stream"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.event("message_start", {"type": "message_start", "message": {
                    "id": "msg_" + secrets.token_hex(10), "type": "message", "role": "assistant",
                    "model": server.model, "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}})
                started_blocks = set()
                block_map = {}
                next_index = 0
                tool_seen = False
                finish_reason = None
                for line in response.iter_lines():
                    if not line.startswith(b"data: "):
                        continue
                    raw = line[6:]
                    if raw == b"[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage.update(chunk["usage"])
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    if delta.get("content"):
                        if "text" not in block_map:
                            block_map["text"] = next_index
                            next_index += 1
                            self.event("content_block_start", {"type": "content_block_start",
                                        "index": block_map["text"], "content_block": {"type": "text", "text": ""}})
                        self.event("content_block_delta", {"type": "content_block_delta",
                                    "index": block_map["text"], "delta": {"type": "text_delta", "text": delta["content"]}})
                    for call in delta.get("tool_calls") or []:
                        tool_seen = True
                        key = ("tool", call.get("index", 0))
                        if key not in block_map:
                            block_map[key] = next_index
                            next_index += 1
                            self.event("content_block_start", {"type": "content_block_start",
                                        "index": block_map[key], "content_block": {"type": "tool_use",
                                        "id": call.get("id") or "toolu_" + secrets.token_hex(10),
                                        "name": (call.get("function") or {}).get("name", "unknown"),
                                        "input": {}}})
                        part = (call.get("function") or {}).get("arguments")
                        if part:
                            self.event("content_block_delta", {"type": "content_block_delta",
                                        "index": block_map[key], "delta": {"type": "input_json_delta",
                                        "partial_json": part}})
                for index in sorted(block_map.values()):
                    self.event("content_block_stop", {"type": "content_block_stop", "index": index})
                stop = "tool_use" if tool_seen else "max_tokens" if finish_reason == "length" else "end_turn"
                normalized = normalize_usage(usage, "openai")
                self.event("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": stop, "stop_sequence": None},
                    "usage": {"input_tokens": normalized.get("input_tokens") or 0,
                              "output_tokens": normalized.get("output_tokens") or 0,
                              "cache_read_input_tokens": normalized.get("cached_tokens") or 0}})
                self.event("message_stop", {"type": "message_stop"})
                completed = True
            else:
                value = response.json()
                usage = value.get("usage") or {}
                self.send_json(200, from_openai(value, server.model))
                completed = True
        except (requests.RequestException, BrokenPipeError, ConnectionResetError, ValueError) as exc:
            record["error"] = type(exc).__name__
        finally:
            response.close()
            server.record({**record, **normalize_usage(usage, "openai"),
                           "status": 200, "complete": completed,
                           "elapsed": time.time() - started})


@contextmanager
def bridge(**kwargs):
    server = Server(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
