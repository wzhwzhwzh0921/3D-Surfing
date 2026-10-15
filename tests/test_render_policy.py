"""Render location is explicit; these tests never render real 3D assets."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scene_factory.render_policy import render_backend, require_render_environment
from scene_factory.runner import blender


class RenderPolicyTests(unittest.TestCase):
    def test_default_is_local_without_cluster_dependencies(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(render_backend(), 'local')
            require_render_environment()

    def test_explicit_k8s_never_starts_local_blender(self):
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'k8s'}, clear=True), \
                patch('scene_factory.runner.subprocess.run') as launch:
            with self.assertRaisesRegex(RuntimeError, 'No local fallback'):
                blender('inspect', Path('/unused.glb'), Path('/unused-output'))
        launch.assert_not_called()

    def test_environment_marker_alone_is_insufficient_in_k8s_mode(self):
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'k8s', 'KUBERNETES_SERVICE_HOST': 'fixture'}), \
                patch('pathlib.Path.is_file', return_value=False):
            with self.assertRaises(RuntimeError):
                require_render_environment()

    def test_cluster_marker_and_service_account_allow_worker(self):
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'k8s', 'KUBERNETES_SERVICE_HOST': 'fixture'}), \
                patch('pathlib.Path.is_file', return_value=True):
            require_render_environment()

    def test_invalid_backend_is_not_a_silent_fallback(self):
        with patch.dict(os.environ, {'SCENEQA_RENDER_BACKEND': 'typo'}, clear=True):
            with self.assertRaises(ValueError):
                render_backend()

    def test_k8s_workers_fail_before_importing_3d_engines_outside_cluster(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, SCENEQA_RENDER_BACKEND='k8s')
        env.pop('KUBERNETES_SERVICE_HOST', None)
        for script in ('experiments/sol_augment/worker.py', 'scene_factory/blender_worker.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(root/script)], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('No local fallback', result.stderr)
                self.assertNotIn('ModuleNotFoundError', result.stderr)
