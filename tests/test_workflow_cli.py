"""Every current command must expose help without credentials, API or workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scene_factory.workflow import STAGES


class WorkflowCliTests(unittest.TestCase):
    def test_current_and_compatible_commands_expose_help_from_other_directory(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root))
        for name in ("LUNA_API_KEY", "LUNA_KEY_FILE", "TOKENHUB_API_KEY"):
            env.pop(name, None)
        commands = [[sys.executable, "-m", "scene_factory", "workflow", name, "--help"]
                    for name in STAGES]
        commands += [[sys.executable, str(root / "tools" / name), "--help"]
                     for name in ("scene_pipeline.py", "scene_screen_batch.py")]
        with tempfile.TemporaryDirectory() as directory:
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)
            result = subprocess.run([sys.executable, "-m", "scene_factory", "workflow", "list", "--json"],
                                    cwd=directory, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)), set(STAGES))


if __name__ == "__main__":
    unittest.main()
