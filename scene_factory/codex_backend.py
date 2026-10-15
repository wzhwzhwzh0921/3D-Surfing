"""Adapt Codex CLI (ChatGPT login or a configured provider) to image/JSON stages."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
MODEL_STAGES = {"screen", "screen-images", "qa", "followup", "followup-batch", "auto", "production", "review",
                "delivery-review", "augment"}
DIRECT_CREDENTIALS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                      "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION")
OUTPUT_SCHEMA = {"type": "object", "properties": {"content": {"type": "string"}},
                 "required": ["content"], "additionalProperties": False}


def codex_auth_mode():
    value = os.getenv("SCENEQA_CODEX_AUTH", "chatgpt")
    if value not in {"chatgpt", "configured"}:
        raise ValueError("SCENEQA_CODEX_AUTH must be chatgpt or configured")
    return value


def codex_environment():
    env = dict(os.environ)
    if codex_auth_mode() == "chatgpt":
        for name in DIRECT_CREDENTIALS:
            env.pop(name, None)
    if env.get("SCENEQA_CODEX_HOME"):
        env["CODEX_HOME"] = str(Path(env["SCENEQA_CODEX_HOME"]).expanduser().resolve())
    return env


def login_status():
    binary = shutil.which(os.getenv("SCENEQA_CODEX_BIN", "codex"))
    if not binary:
        raise RuntimeError("Codex CLI not found; install it and run codex login")
    env = codex_environment()
    version = subprocess.run([binary, "--version"], env=env, capture_output=True,
                             text=True, check=True, timeout=30).stdout.strip()
    if codex_auth_mode() == "configured":
        home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
        config = home / "config.toml"
        signature = {"home": str(home.resolve()), "provider": os.getenv("SCENEQA_CODEX_PROVIDER", "configured"),
                     "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest() if config.is_file() else None}
        return {"binary": binary, "version": version, "auth_method": "configured-provider",
                "provider": signature["provider"],
                "account_fingerprint": hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]}
    result = subprocess.run([binary, "login", "status"], env=env, capture_output=True,
                            text=True, timeout=30)
    status = (result.stdout + result.stderr).strip()
    if result.returncode or "logged in using chatgpt" not in status.lower():
        raise RuntimeError("This workflow requires a ChatGPT Codex login; run codex login")
    auth_path = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    identity = "credential-store"
    if auth_path.is_file():
        auth = json.loads(auth_path.read_text())
        if auth.get("auth_mode") != "chatgpt":
            raise RuntimeError("auth.json does not contain a ChatGPT login")
        identity = (auth.get("tokens") or {}).get("account_id") or identity
    return {"binary": binary, "version": version, "auth_method": "chatgpt",
            "account_fingerprint": hashlib.sha256(identity.encode()).hexdigest()[:16]}


def prepare_messages(messages, directory):
    """Attach images in order; keep their labels without pasting base64 into a prompt."""
    text, images = [], []
    for message in messages:
        text.append("\n[" + message["role"] + "]")
        content = message.get("content")
        if isinstance(content, str):
            text.append(content)
            continue
        if not isinstance(content, list):
            raise ValueError("Only text and image messages are supported")
        for part in content:
            if part.get("type") == "text":
                text.append(part["text"])
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                header, separator, encoded = url.partition(",")
                if not separator or header not in ("data:image/png;base64", "data:image/jpeg;base64",
                                                   "data:image/webp;base64"):
                    raise ValueError("Codex transport accepts inline PNG/JPEG/WebP images only")
                suffix = {"data:image/png;base64": ".png", "data:image/jpeg;base64": ".jpg",
                          "data:image/webp;base64": ".webp"}[header]
                path = Path(directory) / (f"image_{len(images) + 1:03d}" + suffix)
                path.write_bytes(base64.b64decode(encoded, validate=True))
                images.append(path)
                text.append(f"[attached image {len(images)}]")
            else:
                raise ValueError("Unsupported message part")
    return "\n".join(text), images


class CodexBackend:
    def __init__(self, model, *, status=None):
        self.status = status or login_status()
        self.model = model
        self.reasoning = os.getenv("SCENEQA_CODEX_REASONING", "xhigh")
        self.timeout = int(os.getenv("SCENEQA_CODEX_TIMEOUT", "240"))
        self.lock = threading.Lock()

    def complete(self, payload):
        if payload.get("model", self.model) != self.model:
            raise ValueError("Request model differs from the configured Codex model")
        if payload.get("tools") or payload.get("stream"):
            raise ValueError("This adapter supports the existing non-streaming image/JSON stages only")
        if not self.lock.acquire(timeout=10):
            raise BlockingIOError("Codex request in progress; retry this request")
        try:
            return self._complete(payload)
        finally:
            self.lock.release()

    def _complete(self, payload):
        began = time.time()
        with tempfile.TemporaryDirectory(prefix="sceneqa-codex-") as temporary:
            directory = Path(temporary)
            context, images = prepare_messages(payload["messages"], directory)
            schema = directory / "schema.json"
            schema.write_text(json.dumps(OUTPUT_SCHEMA))
            output = directory / "answer.json"
            prompt = ("Perform only the image/text judgment described in the conversation below. "
                      "Do not run commands, use external tools, or inspect other files. "
                      "Return your answer in the schema's content string. If the conversation "
                      "asks for JSON, content must contain that complete JSON object, without "
                      "Markdown fences. The numbered image markers refer to attached images "
                      "in the same order.\n" + context)
            command = [self.status["binary"], "exec", "--ignore-rules",
                       "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--model", self.model, "--json", "--color", "never",
                       "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                       "--disable", "shell_tool", "--disable", "multi_agent",
                       "--disable", "plugins", "--disable", "apps",
                       "--disable", "browser_use", "--disable", "computer_use",
                       "--output-schema", str(schema), "--output-last-message", str(output)]
            if self.reasoning not in {"", "inherit"}:
                command += ["-c", "model_reasoning_effort=" + json.dumps(self.reasoning)]
            if codex_auth_mode() == "chatgpt":
                command += ["--ignore-user-config", "-c", 'model_provider="openai"']
            elif os.getenv("SCENEQA_CODEX_PROVIDER"):
                command += ["-c", "model_provider=" + json.dumps(os.environ["SCENEQA_CODEX_PROVIDER"])]
            for path in images:
                command += ["--image", str(path)]
            command += ["-"]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, cwd=directory,
                                       env=codex_environment(), start_new_session=True)
            try:
                stdout, stderr = process.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                raise TimeoutError("Codex call timed out; no relay fallback") from None
            if process.returncode or not output.exists():
                # Do not forward CLI logs which might contain credential-bearing diagnostics.
                raise RuntimeError(f"Codex call failed (exit {process.returncode}); no relay fallback")
            content = json.loads(output.read_text())["content"]
            if not isinstance(content, str):
                raise ValueError("Codex returned invalid content")
            if payload.get("response_format", {}).get("type") == "json_object":
                if not isinstance(json.loads(content), dict):
                    raise ValueError("Codex response must be a JSON object")
            usage = {}
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    raw = event.get("usage", {})
                    usage = {"prompt_tokens": raw.get("input_tokens", 0),
                             "completion_tokens": raw.get("output_tokens", 0),
                             "total_tokens": raw.get("input_tokens", 0) + raw.get("output_tokens", 0)}
                    if "cached_input_tokens" in raw:
                        usage["prompt_tokens_details"] = {"cached_tokens": raw["cached_input_tokens"]}
            return {"id": "codex-" + secrets.token_hex(8), "object": "chat.completion",
                    "model": self.model, "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content}}], "usage": usage,
                    "sceneqa_backend": {"transport": "codex-cli", **self.status,
                                        "reasoning": self.reasoning, "images": len(images),
                                        "elapsed_seconds": round(time.time() - began, 2)}}


@contextmanager
def gateway(backend, *, port=18773):
    token = secrets.token_urlsafe(32)
    from . import model_backend
    protocol = {"account": backend.status["account_fingerprint"], "version": backend.status.get("version"),
                "model": getattr(backend, "model", None), "reasoning": getattr(backend, "reasoning", None),
                "backend": os.getenv("SCENEQA_MODEL_BACKEND", "codex"),
                "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "interface_sha256": hashlib.sha256(Path(model_backend.__file__).read_bytes()).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()[:24]
    route = "/codex/" + fingerprint + "/v1/chat/completions"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if code in (429, 502, 503, 504):
                self.send_header("Retry-After", "5")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path != route:
                self.reply(404, {"error": "Unknown endpoint"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, {"error": "Invalid local gateway credential"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 80 * 1024 * 1024:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                self.reply(200, backend.complete(payload))
            except BlockingIOError as exc:
                self.reply(429, {"error": str(exc)})
            except TimeoutError as exc:
                self.reply(504, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception:
                self.reply(502, {"error": "Local Codex request failed; no relay fallback"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}" + route, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def launch(argv):
    from .model_backend import configured_model, create_model_backend
    model = configured_model()
    for index, value in enumerate(argv):
        if value == "--model" and index + 1 < len(argv):
            model = argv[index + 1]
        elif value.startswith("--model="):
            model = value.split("=", 1)[1]
    backend = (CodexBackend(model) if os.getenv("SCENEQA_MODEL_BACKEND", "codex") == "codex"
               else create_model_backend(model))
    if argv[0] in {"followup", "followup-batch"} and not os.getenv("SCENEQA_CODEX_TIMEOUT") and hasattr(backend, "timeout"):
        # Compound image reasoning exceeded 240 s in real trials. Keep room for
        # process cleanup before the existing 600 s HTTP client timeout.
        backend.timeout = 480
    with gateway(backend, port=int(os.getenv("SCENEQA_CODEX_PORT", "18773"))) as (url, token):
        env = dict(os.environ, SCENEQA_CODEX_ACTIVE="1", SCENEQA_MODEL_ACTIVE="1", SCENEQA_MODEL=model, LUNA_CHAT_URL=url,
                   LUNA_API_KEY=token, LUNA_MODEL=model)
        env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.pop("LUNA_KEY_FILE", None)
        forwarded = list(argv)
        if argv[0] in {"auto", "screen", "screen-images", "production", "review", "delivery-review"}:
            forwarded += ["--url", url, "--model", model]
        if argv[0] in {"auto", "screen", "screen-images", "review"} and not any(
                arg == "--workers" or arg.startswith("--workers=") for arg in argv):
            forwarded += ["--workers", "1"]
        if any(arg == "--key-file" or arg.startswith("--key-file=") for arg in argv):
            raise ValueError("Gateway mode supplies credentials; omit --key-file")
        if argv[0] == "followup-batch":
            forwarded += ["--model", model]
        report = {"backend": os.getenv("SCENEQA_MODEL_BACKEND", "codex"),
                  "auth_method": backend.status.get("auth_method", "chatgpt"), "model": model,
                  "reasoning": getattr(backend, "reasoning", None), "version": backend.status.get("version")}
        if argv[0] in {"followup", "followup-batch"}: report["timeout_seconds"] = getattr(backend, "timeout", None)
        print(json.dumps(report), flush=True)
        return subprocess.call([sys.executable, "-m", "scene_factory", "workflow", *forwarded],
                               cwd=Path.cwd(), env=env)
