"""Lazy, serialized rendering of model-requested 3D inspection views."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[2]


def worker_bundle() -> bytes:
    base = (ROOT / 'experiments/sol_augment/worker.py').read_text()
    lines = base.splitlines(keepends=True)
    guards = [node for node in ast.parse(base).body if isinstance(node, ast.If)
              and '__name__' in ast.unparse(node.test)]
    if len(guards) != 1:
        raise ValueError('Base worker entry point changed')
    guard = guards[0]
    base = ''.join(lines[:guard.lineno - 1] + lines[guard.end_lineno:])
    extra = (ROOT / 'experiments/sol_augment/screen_view_worker.py').read_text()
    return (base + '\n\n' + extra).encode()


class InspectionRenderer:
    def __init__(self, output: Path, *, pod: str | None = None):
        self.lock = threading.Lock()
        self.remote = None
        self.created = False
        self.body = worker_bundle()
        self.worker_sha256 = hashlib.sha256(self.body).hexdigest()
        self.name = pod or 'scene-rich-' + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:12]
        self.owned = pod is None
        self.augment = None

    def render(self, source: Path, output: Path, requests: list[dict]) -> list[dict]:
        with self.lock:
            if self.remote is None:
                path = ROOT / 'experiments/sol_augment/run.py'
                spec = importlib.util.spec_from_file_location('sceneqa_screen_remote', path)
                self.augment = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self.augment)
                self.remote = self.augment.Remote(self.name)
                self.remote.worker_bytes = self.body
                self.remote.worker_sha256 = self.worker_sha256
                self.remote.worker_path = '/tmp/screen-worker-' + self.worker_sha256[:12] + '.py'
            if not self.created:
                # Track ownership before creation, including failed readiness waits.
                self.created = True
                self.remote.create()
            output.mkdir(parents=True, exist_ok=True)
            plan = output / 'request.json'
            plan.write_text(json.dumps({'mode': 'screen_inspection', 'requests': requests}, ensure_ascii=False))
            self.remote.run(source, output, plan)
            records = json.loads((output / 'views.json').read_text())
            if [item['label'] for item in records] != [item['label'] for item in requests]:
                raise ValueError('Inspection response labels differ from request')
            for item in records:
                if not (output / item['image']).is_file():
                    raise ValueError('Missing requested inspection image')
                item['path'] = str((output / item['image']).resolve())
            return records

    def close(self):
        if self.created and self.owned:
            try:
                self.remote.close()
            finally:
                self.created = False
