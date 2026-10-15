"""Alternate Codex provider and plugin routing without any paid model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scene_factory import codex_backend, model_backend, workflow


class ModelInterfaceTests(unittest.TestCase):
    def test_configured_provider_keeps_its_credentials_and_skips_chatgpt_login(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text('model_provider = "mine"')
            with patch.dict(os.environ, {'SCENEQA_CODEX_AUTH': 'configured',
                    'SCENEQA_CODEX_HOME': directory, 'MY_PROVIDER_KEY': 'private-fixture',
                    'OPENAI_API_KEY': 'explicit-provider-fixture'}, clear=True), \
                    patch.object(codex_backend.shutil, 'which', return_value='/mock/codex'), \
                    patch.object(codex_backend.subprocess, 'run', return_value=SimpleNamespace(stdout='codex-test')) as run:
                identity = codex_backend.login_status()
                self.assertEqual(identity['auth_method'], 'configured-provider')
                self.assertEqual(codex_backend.codex_environment()['MY_PROVIDER_KEY'], 'private-fixture')
                self.assertIn('OPENAI_API_KEY', codex_backend.codex_environment())
                self.assertEqual(run.call_count, 1)
                self.assertNotIn('private-fixture', json.dumps(identity))
                config.write_text('model_provider = "another"')
                self.assertNotEqual(identity['account_fingerprint'], codex_backend.login_status()['account_fingerprint'])

    def test_custom_codex_exec_preserves_provider_and_can_omit_reasoning_override(self):
        status = {'binary': '/mock/codex', 'version': 'test', 'auth_method': 'configured-provider',
                  'account_fingerprint': 'test-config'}
        def start(command, **kwargs):
            self.assertNotIn('--ignore-user-config', command)
            self.assertIn('model_provider="mine"', command)
            self.assertNotIn('model_provider="openai"', command)
            self.assertFalse(any('model_reasoning_effort=' in v for v in command))
            self.assertEqual(kwargs['env']['MY_PROVIDER_KEY'], 'private-fixture')
            self.assertIn('--image', command)
            self.assertEqual(command[command.index('--model')+1], 'other-vision-model')
            Path(command[command.index('--output-last-message')+1]).write_text('{"content":"{\\"ok\\":true}"}')
            return SimpleNamespace(returncode=0, communicate=lambda *a, **k: ('', ''))
        with patch.dict(os.environ, {'SCENEQA_CODEX_AUTH': 'configured', 'SCENEQA_CODEX_PROVIDER': 'mine',
                'SCENEQA_CODEX_REASONING': 'inherit', 'MY_PROVIDER_KEY': 'private-fixture'}, clear=True), \
                patch.object(codex_backend.subprocess, 'Popen', side_effect=start):
            adapter = codex_backend.CodexBackend('other-vision-model', status=status)
            result = adapter.complete({'model': 'other-vision-model', 'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': 'return JSON'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1hZ2U='}}]}]})
        self.assertEqual(json.loads(result['choices'][0]['message']['content']), {'ok': True})

    def test_model_factory_receives_selected_name_and_requires_interface(self):
        adapter = SimpleNamespace(complete=lambda p: {}, status={'account_fingerprint':'plugin-v1'}, model='custom')
        calls = []
        module = SimpleNamespace(make=lambda **kwargs: (calls.append(kwargs) or adapter))
        with patch.dict(os.environ, {'SCENEQA_MODEL_BACKEND':'plugin', 'SCENEQA_MODEL_FACTORY':'example:make'}, clear=True), \
                patch.object(model_backend.importlib, 'import_module', return_value=module):
            self.assertIs(model_backend.create_model_backend('custom'), adapter)
        self.assertEqual(calls, [{'model':'custom'}])

    def test_relay_is_explicit_and_normalizes_public_configuration(self):
        with patch.dict(os.environ, {'SCENEQA_API_URL':'https://example.invalid/v1/chat/completions',
                'SCENEQA_API_KEY':'private-fixture', 'SCENEQA_MODEL':'another-model'}, clear=True):
            model_backend.configure_relay()
            self.assertEqual(os.environ['LUNA_MODEL'], 'another-model')
            self.assertEqual(os.environ['LUNA_API_KEY'], 'private-fixture')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'SCENEQA_API_URL'):
                model_backend.configure_relay()

    def test_workflow_config_neither_logs_in_nor_renders(self):
        with patch.dict(os.environ, {'SCENEQA_MODEL':'custom-vision-model'}, clear=True), \
                patch.object(codex_backend, 'login_status', side_effect=AssertionError('must not log in')), \
                patch('scene_factory.rendering.subprocess.Popen', side_effect=AssertionError('must not render')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workflow.main(['config']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['model']['name'], 'custom-vision-model')
        self.assertEqual(report['render']['backend'], 'local')

    def test_relay_cli_passes_selected_model_to_existing_stage_parser(self):
        import sys
        captured = []
        def run(*args, **kwargs):
            captured.extend(sys.argv)
        with patch.dict(os.environ, {'SCENEQA_MODEL_BACKEND':'relay', 'SCENEQA_MODEL':'other-vision-model',
                'SCENEQA_API_URL':'https://example.invalid/v1/chat/completions'}, clear=True), \
                patch.object(workflow.runpy, 'run_path', side_effect=run):
            workflow.main(['review', '--baseline', '/unused'])
        self.assertEqual(captured[-4:], ['--url', 'https://example.invalid/v1/chat/completions', '--model', 'other-vision-model'])
