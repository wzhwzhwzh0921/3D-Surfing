"""A launcher must preserve model/backend choices and freeze resumable settings."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sceneqa_launcher_test', ROOT/'tools/start_followup_batch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def snapshot(out):
    out.mkdir(parents=True)
    launcher.save(out/'source-manifest.json', {})


class LauncherTests(unittest.TestCase):
    def test_custom_codex_model_and_local_rendering_survive_background_launch_and_resume(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'SCENEQA_MODEL':'other-vision-model', 'SCENEQA_CODEX_AUTH':'configured',
                    'MY_PROVIDER_KEY':'private-fixture', 'SCENEQA_CODEX_REASONING':'inherit'}, clear=True), \
                patch.object(launcher, 'model_identity', return_value={'auth_method':'configured-provider', 'account_fingerprint':'mine'}), \
                patch.object(launcher, 'snapshot', side_effect=snapshot), \
                patch.object(launcher.subprocess, 'Popen', return_value=SimpleNamespace(pid=123456)) as start, \
                contextlib.redirect_stdout(io.StringIO()):
            args = ['--out', directory, '--target', '4']
            launcher.main(args)
            _, kwargs = start.call_args
            self.assertEqual(kwargs['env']['SCENEQA_RENDER_BACKEND'], 'local')
            self.assertEqual(kwargs['env']['SCENEQA_MODEL'], 'other-vision-model')
            self.assertEqual(kwargs['env']['SCENEQA_CODEX_REASONING'], 'inherit')
            self.assertEqual(kwargs['env']['MY_PROVIDER_KEY'], 'private-fixture')
            receipt = json.loads((Path(directory)/'launch.json').read_text())
            self.assertNotIn('private-fixture', json.dumps(receipt))
            self.assertEqual(receipt['model'], 'other-vision-model')
            with patch.object(launcher.os, 'kill', side_effect=ProcessLookupError):
                launcher.main(args)
                self.assertEqual(start.call_count, 2)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    launcher.main([*args, '--render-backend', 'k8s'])
                self.assertEqual(start.call_count, 2)

    def test_k8s_is_explicit_and_relay_credentials_are_not_discarded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'SCENEQA_API_URL':'https://example.invalid/v1/chat/completions', 'SCENEQA_API_KEY':'private-fixture',
                'SCENEQA_RENDER_IMAGE':'example/render:v1'}, clear=True), \
                patch.object(launcher, 'model_identity', return_value={'auth_method':'api', 'account_fingerprint':'mine'}), \
                patch.object(launcher, 'snapshot', side_effect=snapshot), \
                patch.object(launcher.subprocess, 'Popen', return_value=SimpleNamespace(pid=123456)) as start, \
                contextlib.redirect_stdout(io.StringIO()):
            launcher.main(['--out',directory, '--target','4', '--backend','relay', '--model','other-model',
                           '--render-backend','k8s'])
            env = start.call_args.kwargs['env']
            self.assertEqual(env['LUNA_API_KEY'], 'private-fixture')
            self.assertEqual(env['LUNA_MODEL'], 'other-model')
            self.assertEqual(env['SCENEQA_RENDER_BACKEND'], 'k8s')
            receipt = json.loads((Path(directory)/'launch.json').read_text())
            self.assertEqual(receipt['model_backend'], 'relay')
            self.assertEqual(receipt['render_configuration']['image'], 'example/render:v1')

    def test_invalid_target_does_not_login_or_spawn(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(launcher, 'model_identity') as identity, \
                patch.object(launcher.subprocess, 'Popen') as start, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                launcher.main(['--out','/unused', '--target','0'])
        identity.assert_not_called()
        start.assert_not_called()
