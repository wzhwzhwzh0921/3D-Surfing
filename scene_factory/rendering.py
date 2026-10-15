"""Run the same GLB worker locally (default) or on an opt-in Kubernetes backend.

No 3D engine is imported into the orchestrator. Both transports implement the
create/run/close interface and accept an immutable worker snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Protocol

from .render_policy import render_backend


class RenderWorker(Protocol):
    name: str
    worker_bytes: bytes
    worker_sha256: str
    worker_path: str

    def create(self) -> None: ...
    def run(self, source: Path, out: Path, plan: Path | None = None, *,
            screen_only: bool = False, groups_only: bool = False) -> None: ...
    def close(self) -> None: ...


def render_settings():
    """Public configuration participates in production/cache fingerprints."""
    if render_backend() == 'local':
        return {'backend': 'local', 'python': os.getenv('SCENEQA_RENDER_PYTHON', sys.executable),
                'platform': os.getenv('PYOPENGL_PLATFORM', 'egl'),
                'opengl_env': {name: os.getenv(name, '') for name in (
                    'EGL_DEVICE_ID', 'LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER',
                    '__EGL_VENDOR_LIBRARY_FILENAMES')},
                'threads': int(os.getenv('SCENEQA_RENDER_THREADS', '2')),
                'timeout': int(os.getenv('SCENEQA_RENDER_TIMEOUT', '1800'))}
    return {'backend': 'k8s', 'namespace': os.getenv('SCENEQA_NAMESPACE', 'default'),
            'image': os.getenv('SCENEQA_RENDER_IMAGE', ''),
            'image_pull_secret': os.getenv('SCENEQA_IMAGE_PULL_SECRET', ''),
            'gpu_count': int(os.getenv('SCENEQA_RENDER_GPUS', '1')),
            'cpu_request': os.getenv('SCENEQA_RENDER_CPU_REQUEST', '2'),
            'cpu_limit': os.getenv('SCENEQA_RENDER_CPU_LIMIT', '4'),
            'memory_request': os.getenv('SCENEQA_RENDER_MEMORY_REQUEST', '4Gi'),
            'memory_limit': os.getenv('SCENEQA_RENDER_MEMORY_LIMIT', '10Gi'),
            'timeout': int(os.getenv('SCENEQA_RENDER_TIMEOUT', '1800'))}


def command(args, **kwargs):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or str(exc))[-2400:]) from exc


class LocalWorker:
    def __init__(self, name, worker_bytes):
        self.name, self.worker_bytes = name, worker_bytes
        self.worker_sha256 = hashlib.sha256(worker_bytes).hexdigest()
        self.worker_path = ''

    def create(self):
        config = render_settings()
        if config['threads'] < 1 or config['timeout'] < 1:
            raise ValueError('Render threads and timeout must be positive')
        if not shutil.which(config['python']):
            raise RuntimeError('Local renderer Python not found; set SCENEQA_RENDER_PYTHON')

    def run(self, source, out, plan=None, *, screen_only=False, groups_only=False):
        self.create()
        config = render_settings()
        source, out = Path(source).resolve(), Path(out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, SCENEQA_RENDER_BACKEND='local', PYOPENGL_PLATFORM=config['platform'])
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                    'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'LP_NUM_THREADS'):
            env[key] = str(config['threads'])
        with tempfile.TemporaryDirectory(prefix='sceneqa-render-') as directory:
            snapshot = Path(directory) / 'worker.py'
            snapshot.write_bytes(self.worker_bytes)
            args = [config['python'], str(snapshot), str(source), str(out)]
            if plan:
                args.append(str(Path(plan).resolve()))
            elif screen_only:
                args.append('--screen-only')
            elif groups_only:
                args.append('--groups-only')
            log_path = out / 'local.log'
            with log_path.open('w') as log:
                process = subprocess.Popen(args, cwd=directory, env=env, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=config['timeout'])
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
            if code:
                raise RuntimeError(f'Local render worker exited {code}; see {log_path}')

    def close(self):
        pass  # Each local run waits for and owns its child process.


class KubernetesWorker:
    def __init__(self, name, worker_bytes):
        self.name, self.worker_bytes = name, worker_bytes
        self.worker_sha256 = hashlib.sha256(worker_bytes).hexdigest()
        self.worker_path = '/tmp/worker-' + self.worker_sha256[:12] + '.py'

    def create(self):
        config = render_settings()
        if not config.get('image'):
            raise ValueError('K8s rendering requires SCENEQA_RENDER_IMAGE; there is no private default image')
        namespace = config['namespace']
        current = json.loads(command(['kubectl', '-n', namespace, 'get', 'pod', self.name,
                                      '--ignore-not-found', '-o', 'json']) or '{}')
        if current.get('status', {}).get('phase') in ('Failed', 'Succeeded') or current.get('metadata', {}).get('deletionTimestamp'):
            cleanup_workers([self.name], wait=True)
        resources = {'requests': {'cpu': config['cpu_request'], 'memory': config['memory_request']},
                     'limits': {'cpu': config['cpu_limit'], 'memory': config['memory_limit']}}
        if config['gpu_count'] < 0:
            raise ValueError('SCENEQA_RENDER_GPUS must be nonnegative')
        if config['gpu_count']:
            for value in resources.values():
                value['nvidia.com/gpu'] = str(config['gpu_count'])
        pod_spec = {'restartPolicy': 'Never', 'containers': [{'name': 'worker', 'image': config['image'],
            'command': ['sleep', 'infinity'], 'env': [
                {'name': 'SCENEQA_RENDER_BACKEND', 'value': 'k8s'},
                {'name': 'NVIDIA_DRIVER_CAPABILITIES', 'value': 'graphics,utility,compute,display'}],
            'resources': resources}]}
        if config['image_pull_secret']:
            pod_spec['imagePullSecrets'] = [{'name': config['image_pull_secret']}]
        pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': self.name,
               'namespace': namespace, 'labels': {'sceneqa-workload': 'factory'}}, 'spec': pod_spec}
        command(['kubectl', 'apply', '-f', '-'], input=json.dumps(pod))
        command(['kubectl', '-n', namespace, 'wait', '--for=condition=Ready', 'pod/' + self.name, '--timeout=240s'])
        with tempfile.TemporaryDirectory(prefix='sceneqa-worker-') as directory:
            snapshot = Path(directory) / 'worker.py'
            snapshot.write_bytes(self.worker_bytes)
            command(['kubectl', '-n', namespace, 'cp', str(snapshot), self.name + ':' + self.worker_path])
        actual = command(['kubectl', '-n', namespace, 'exec', self.name, '--', 'python', '-c',
            'import hashlib;print(hashlib.sha256(open(' + repr(self.worker_path) + ",'rb').read()).hexdigest())"]).strip()
        if actual != self.worker_sha256:
            raise RuntimeError('Remote worker SHA does not match the uploaded snapshot')

    def run(self, source, out, plan=None, *, screen_only=False, groups_only=False):
        config = render_settings()
        namespace = config['namespace']
        out = Path(out)
        base = '/tmp/qa-' + hashlib.sha256(str(out.resolve()).encode()).hexdigest()[:16]
        try:
            command(['kubectl', '-n', namespace, 'exec', self.name, '--', 'mkdir', '-p', base])
            command(['kubectl', '-n', namespace, 'cp', str(source), self.name + ':' + base + '/source.glb'])
            args = ['python', self.worker_path, base + '/source.glb', base + '/output']
            if plan:
                command(['kubectl', '-n', namespace, 'cp', str(plan), self.name + ':' + base + '/plan.json'])
                args.append(base + '/plan.json')
            elif screen_only:
                args.append('--screen-only')
            elif groups_only:
                args.append('--groups-only')
            log = command(['kubectl', '-n', namespace, 'exec', self.name, '--', *args], timeout=config['timeout'])
            out.mkdir(parents=True, exist_ok=True)
            (out / 'remote.log').write_text(log)
            command(['kubectl', '-n', namespace, 'cp', self.name + ':' + base + '/output/.', str(out)])
        finally:
            try:
                command(['kubectl', '-n', namespace, 'exec', self.name, '--', 'rm', '-rf', base], timeout=45)
            except Exception as exc:
                out.mkdir(parents=True, exist_ok=True)
                (out / 'remote_cleanup.json').write_text(json.dumps({'status': 'failed', 'reason': type(exc).__name__}))

    def close(self):
        cleanup_workers([self.name])


def create_render_worker(name, worker_bytes) -> RenderWorker:
    factory = LocalWorker if render_backend() == 'local' else KubernetesWorker
    return factory(name, worker_bytes)


def cleanup_workers(names, *, wait=False):
    if render_backend() == 'local' or not names:
        return
    command(['kubectl', '-n', os.getenv('SCENEQA_NAMESPACE', 'default'), 'delete', 'pod',
             *names, '--ignore-not-found', '--wait=' + str(wait).lower(), '--timeout=60s'], timeout=70)
