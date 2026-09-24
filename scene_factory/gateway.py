"""Local, protocol-preserving API recorder. It does not expose agent tools."""
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

RETRYABLE = {429, 500, 502, 503, 504}


def normalize_usage(usage, protocol):
    if protocol == 'openai':
        total = usage.get('prompt_tokens')
        cached = usage.get('prompt_tokens_details', {}).get('cached_tokens')
        if cached is None:
            cached = usage.get('prompt_cache_hit_tokens')
        output = usage.get('completion_tokens')
        created = None
    else:
        uncached = usage.get('input_tokens')
        cached = usage.get('cache_read_input_tokens')
        created = usage.get('cache_creation_input_tokens')
        deepseek_hit = usage.get('prompt_cache_hit_tokens')
        deepseek_miss = usage.get('prompt_cache_miss_tokens')
        if cached is None and deepseek_hit is not None:
            cached = deepseek_hit
            total = deepseek_hit + deepseek_miss if deepseek_miss is not None else max(uncached or 0, deepseek_hit)
        else:
            total = None if uncached is None else uncached + (cached or 0) + (created or 0)
        output = usage.get('output_tokens')
    return {'input_tokens': total, 'cached_tokens': cached, 'cache_creation_tokens': created,
            'output_tokens': output, 'cache_hit_rate': cached / total if total and cached is not None else None,
            'raw_usage': usage, 'cost_usd': None}


def summarize_usage(path):
    records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    complete = [r for r in records if r.get('complete') and r.get('status') == 200]
    known = [r for r in complete if r.get('input_tokens') is not None and r.get('cached_tokens') is not None]
    denominator = sum(r['input_tokens'] for r in known)
    return {'api_attempts': len(records), 'completed_api_calls': len(complete),
            'retryable_errors': sum(r.get('status') in RETRYABLE for r in records),
            'input_tokens': sum(r.get('input_tokens') or 0 for r in complete),
            'cached_tokens': sum(r['cached_tokens'] for r in known),
            'output_tokens': sum(r.get('output_tokens') or 0 for r in complete),
            'cache_hit_rate': sum(r['cached_tokens'] for r in known) / denominator if denominator else None,
            'cache_measurement_calls': len(known), 'usage_missing_calls': len(complete) - len(known),
            'cost_usd': None}


class Recorder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, upstream, key, model, output, max_tokens=65536, retry_delays=(20, 40, 60), timeout=900):
        super().__init__(('127.0.0.1', 0), Handler)
        self.upstream = upstream.rstrip('/')
        self.key, self.model, self.output = key, model, Path(output)
        self.max_tokens, self.retry_delays, self.timeout = max_tokens, retry_delays, timeout
        self.token = secrets.token_urlsafe(32)
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_port}'

    def record(self, value):
        with self.lock, self.output.open('a') as f:
            f.write(json.dumps(value, ensure_ascii=False) + '\n')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get('x-api-key') or self.headers.get('Authorization', '').removeprefix('Bearer ')
        return secrets.compare_digest(supplied, self.server.token)

    def do_GET(self):
        if not self.authorized():
            return self.answer(401, {'error': 'Unauthorized'})
        if urlsplit(self.path).path == '/v1/models':
            return self.answer(200, {'object': 'list', 'data': [{'id': self.server.model, 'object': 'model'}]})
        self.answer(404, {'error': 'Unknown route'})

    def do_POST(self):
        server = self.server
        if not self.authorized():
            return self.answer(401, {'error': 'Unauthorized'})
        path = urlsplit(self.path).path
        if path not in ('/v1/chat/completions', '/v1/messages', '/v1/messages/count_tokens'):
            return self.answer(404, {'error': 'Unknown route'})
        protocol = 'openai' if path == '/v1/chat/completions' else 'anthropic'
        try:
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        except (ValueError, TypeError):
            return self.answer(400, {'error': 'Invalid JSON'})
        requested_model = payload.get('model')
        payload['model'] = server.model
        is_count = path.endswith('/count_tokens')
        if not is_count:
            if protocol == 'openai':
                payload.pop('max_tokens', None)
                payload['max_completion_tokens'] = server.max_tokens
            else:
                payload['max_tokens'] = server.max_tokens
        system = payload.get('system') if protocol == 'anthropic' else [m for m in payload.get('messages', []) if m.get('role') == 'system']
        prefix = {'model': server.model, 'system': system, 'tools': payload.get('tools', [])}
        cache_key = 'graphics-agent-' + hashlib.sha256(json.dumps(prefix, sort_keys=True).encode()).hexdigest()[:32]
        if protocol == 'openai':
            payload['prompt_cache_key'] = cache_key
        headers = {'Authorization': f'Bearer {server.key}', 'x-api-key': server.key,
                   'Content-Type': 'application/json', 'X-Session-ID': cache_key,
                   'anthropic-version': self.headers.get('anthropic-version', '2023-06-01')}
        if self.headers.get('anthropic-beta'):
            headers['anthropic-beta'] = self.headers['anthropic-beta']
        body = json.dumps(payload).encode()
        base = server.upstream.removesuffix('/v1')
        tool_names = [t.get('function', t).get('name') for t in payload.get('tools', [])]
        metadata = {'protocol': protocol, 'path': path, 'requested_model': requested_model, 'model': server.model,
                    'max_output_tokens': None if is_count else server.max_tokens, 'tool_names': tool_names,
                    'request_sha256': hashlib.sha256(body).hexdigest(), 'cache_key': cache_key,
                    'message_count': len(payload.get('messages', [])), 'stream': bool(payload.get('stream'))}
        for attempt in range(len(server.retry_delays) + 1):
            start = time.time()
            record = {**metadata, 'time': start, 'attempt': attempt + 1, 'complete': False}
            try:
                response = requests.post(base + path, data=body, headers=headers,
                                         stream=True, timeout=(30, server.timeout))
            except requests.RequestException as exc:
                server.record({**record, 'status': None, 'error': type(exc).__name__, 'elapsed': time.time() - start})
                if attempt < len(server.retry_delays) and not server.stop_event.wait(server.retry_delays[attempt]):
                    continue
                return self.answer(502, {'error': {'type': 'api_error', 'message': 'Upstream connection failed'}})
            if response.status_code in RETRYABLE and attempt < len(server.retry_delays):
                server.record({**record, 'status': response.status_code, 'elapsed': time.time() - start})
                response.close()
                delay = server.retry_delays[attempt]
                try:
                    delay = max(delay, min(float(response.headers.get('retry-after', 0)), 300))
                except ValueError:
                    pass
                if server.stop_event.wait(delay):
                    return self.answer(503, {'error': 'Run stopped'})
                continue
            record['status'] = response.status_code
            usage, returned_tools, stop_reason = {}, [], None
            try:
                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                    self.send_response(response.status_code)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    saw_stop = False
                    for line in response.iter_lines(chunk_size=1):
                        self.wfile.write(line + b'\n')
                        self.wfile.flush()
                        if not line.startswith(b'data: '):
                            continue
                        content = line[6:]
                        if content == b'[DONE]':
                            saw_stop = True
                            continue
                        try:
                            event = json.loads(content)
                        except ValueError:
                            continue
                        usage.update(event.get('usage') or event.get('message', {}).get('usage') or {})
                        if event.get('type') == 'message_stop':
                            saw_stop = True
                        block = event.get('content_block', {})
                        if block.get('type') == 'tool_use':
                            returned_tools.append(block.get('name'))
                        stop_reason = event.get('delta', {}).get('stop_reason') or stop_reason
                    record['complete'] = saw_stop
                else:
                    raw = response.content
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = {}
                    usage = value.get('usage') or {}
                    choices = value.get('choices') or [{}]
                    returned_tools = [t.get('function', {}).get('name') for t in choices[0].get('message', {}).get('tool_calls', [])]
                    returned_tools += [t.get('name') for t in value.get('content', []) if t.get('type') == 'tool_use']
                    stop_reason = value.get('stop_reason') or choices[0].get('finish_reason')
                    self.send_response(response.status_code)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    record['complete'] = True
            except (requests.RequestException, BrokenPipeError, ConnectionResetError) as exc:
                record['error'] = type(exc).__name__
            finally:
                response.close()
                server.record({**record, **normalize_usage(usage, protocol), 'returned_tools': returned_tools,
                               'stop_reason': stop_reason, 'elapsed': time.time() - start})
            return


@contextmanager
def gateway(**kwargs):
    server = Recorder(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
