"""Real transport plumbing with a tiny Python fixture, never a 3D renderer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scene_factory import rendering

# No 3D imports: test immutable script delivery, paths, flags and environment.
FIXTURE = b'''import json, os, sys
from pathlib import Path
out = Path(sys.argv[2])
(out / 'received.json').write_text(json.dumps({'args': sys.argv[1:],
    'threads': os.environ['OPENBLAS_NUM_THREADS'],
    'backend': os.environ['SCENEQA_RENDER_BACKEND']}))
'''


class RenderBackendTests(unittest.TestCase):
    def test_local_run_preserves_source_and_supports_every_worker_mode_without_kubectl(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                patch.object(rendering, 'command', side_effect=AssertionError('must not access cluster')):
            root = Path(directory)
            source = root/'original.glb'
            source.write_bytes(b'unchanged native asset')
            plan = root/'plan.json'
            plan.write_text('{}')
            for mode, kwargs, suffix in [('screen', {'screen_only': True}, '--screen-only'),
                                          ('groups', {'groups_only': True}, '--groups-only'),
                                          ('build', {'plan': plan}, str(plan))]:
                worker = rendering.create_render_worker('fixture', FIXTURE)
                self.assertIsInstance(worker, rendering.LocalWorker)
                worker.create()
                worker.run(source, root/mode, **kwargs)
                worker.close()
                receipt = json.loads((root/mode/'received.json').read_text())
                self.assertEqual(receipt['args'][0], str(source))
                self.assertEqual(receipt['args'][-1], suffix)
                self.assertEqual(receipt['backend'], 'local')
                self.assertEqual(receipt['threads'], '2')
            self.assertEqual(source.read_bytes(), b'unchanged native asset')

    def test_k8s_requires_explicit_image_before_accessing_cluster(self):
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'k8s'}, clear=True), \
                patch.object(rendering, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'SCENEQA_RENDER_IMAGE'):
                rendering.create_render_worker('fixture', FIXTURE).create()
        command.assert_not_called()

    def test_k8s_resources_and_namespace_are_configurable_without_private_secrets(self):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if 'get' in args:
                return '{}'
            if '-c' in args:
                return hashlib.sha256(FIXTURE).hexdigest()
            return ''
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'k8s', 'SCENEQA_NAMESPACE': 'mine',
                'SCENEQA_RENDER_IMAGE': 'example/render:v1', 'SCENEQA_RENDER_GPUS': '0'}, clear=True), \
                patch.object(rendering, 'command', side_effect=command):
            worker = rendering.create_render_worker('fixture', FIXTURE)
            worker.create()
            worker.close()
        pod = json.loads(next(kwargs['input'] for args, kwargs in calls if 'apply' in args))
        self.assertEqual(pod['metadata']['namespace'], 'mine')
        self.assertNotIn('imagePullSecrets', pod['spec'])
        container = pod['spec']['containers'][0]
        self.assertEqual(container['image'], 'example/render:v1')
        self.assertNotIn('nvidia.com/gpu', container['resources']['requests'])
        self.assertEqual({v['name']:v['value'] for v in container['env']}['SCENEQA_RENDER_BACKEND'], 'k8s')
        self.assertTrue(all('secret' not in args for args, _ in calls))

    def test_local_cleanup_never_touches_cluster(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(rendering, 'command') as command:
            rendering.cleanup_workers(['fixture'])
        command.assert_not_called()

    def test_timeout_terminates_only_the_owned_process_group(self):
        process = type('Process', (), {'pid': 1234})()
        from unittest.mock import Mock
        process.wait = Mock(side_effect=[subprocess.TimeoutExpired('fixture', 1), 0])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                patch.object(rendering.subprocess, 'Popen', return_value=process), \
                patch.object(rendering.os, 'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                rendering.LocalWorker('fixture', FIXTURE).run(Path(directory)/'asset.glb', Path(directory)/'out')
        kill.assert_called_once_with(1234, rendering.signal.SIGTERM)
