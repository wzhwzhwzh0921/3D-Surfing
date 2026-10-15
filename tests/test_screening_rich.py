"""Richness screening protocol tests. No live calls or local 3D rendering."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import io
import urllib.error

from scene_factory.screening import judge, policy, rich
from scene_factory.screening.inspection import worker_bundle


class Reply:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps({"model": "test-model", "usage": {"prompt_tokens": 1},
                           "choices": [{"message": {"content": json.dumps(self.value)}}]}).encode()


class MockRenderer:
    worker_sha256 = "fixture-worker"

    def __init__(self):
        self.calls = []

    def render(self, source, output, requests):
        self.calls.append(requests)
        output.mkdir(parents=True)
        views = []
        for item in requests:
            path = output / (item["label"] + ".png")
            path.write_bytes(b"remote inspection fixture")
            views.append({"label": item["label"], "path": str(path), "request": item,
                          "camera": {"matrix": [[1, 0, 0, 0]] * 4, "yfov": .78}})
        return views


class RichScreeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.uid = "rich-test"
        self.config = judge.ScreenConfig(captures=self.root / "images", output=self.root / "out",
                                         model="test-model", url="https://test.invalid", version="v4",
                                         prompt=policy.prompt_text("v4"))
        folder = self.config.captures / self.uid
        folder.mkdir(parents=True)
        for label in policy.VIEWS:
            (folder / (label + '.png')).write_bytes(b"remote fixture")
        self.glb = self.root / "source.glb"
        self.glb.write_bytes(b"source fixture")
        self.source = {"source_glb": str(self.glb), "name": "must not become evidence"}
        self.renderer = MockRenderer()

    def decision(self, keep=False):
        labels = ["inspect_1_1"] if keep else [policy.VIEWS[0]]
        return {"uid": self.uid, "action": "decide", "decision": "keep" if keep else "reject",
                "reason": "完整内容丰富" if keep else "只有地形和相似树木，内容不足",
                "object_count_lower_bound": 24 if keep else 0,
                "categories": [{"name": "category" + str(i), "count_lower_bound": 3, "views": labels}
                               for i in range(8)] if keep else [],
                "regions": [{"name": "region" + str(i), "contents": ["visible items"], "views": labels}
                            for i in range(3)] if keep else [],
                "exploration_evidence": [{"description": "新视角下的台面物件", "views": labels}] if keep else []}

    def inspect(self, reference=None):
        return {"uid": self.uid, "action": "inspect", "requests": [{
            "reference_view": reference or policy.VIEWS[0], "bbox": [.2, .3, .6, .8],
            "yaw": 37.5, "pitch": -8, "zoom": 2.2, "purpose": "确认货架内容"}]}

    def run_model(self, values, source=None, config=None):
        with patch.object(rich.urllib.request, "urlopen", side_effect=[Reply(v) for v in values]) as http:
            record = judge.judge_scene(self.uid, config or self.config, "test-secret",
                                       self.source if source is None else source, renderer=self.renderer)
        return record, http

    def test_sparse_scene_rejected_without_render_and_no_name_leak(self):
        record, http = self.run_model([self.decision()])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["judgment"]["decision"], "reject")
        self.assertEqual(self.renderer.calls, [])
        self.assertNotIn(self.source["name"], http.call_args.args[0].data.decode())

    def test_keep_requires_real_inspection_and_preserves_model_parameters(self):
        record, http = self.run_model([self.inspect(), self.decision(True)])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.renderer.calls[0][0]["yaw"], 37.5)
        self.assertEqual(record["judgment"]["object_count_lower_bound"], 24)
        self.assertEqual(len(record["inspection_views"]), 1)

    def test_early_keep_gets_feedback_instead_of_being_accepted(self):
        early = self.decision(True)
        for field in ("categories", "regions", "exploration_evidence"):
            for item in early[field]:
                item["views"] = [policy.VIEWS[0]]
        record, http = self.run_model([early, self.inspect(), self.decision(True)])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(len(self.renderer.calls), 1)

    def test_missing_glb_is_processing_failure_not_quality_rejection(self):
        record, _ = self.run_model([self.inspect()], source={})
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("judgment", record)
        self.assertIn("inspection_unavailable", record["errors"][-1])

    def test_budget_exhaustion_is_not_silent_rejection(self):
        config = replace(self.config, inspection_rounds=1)
        record, _ = self.run_model([self.inspect(), self.inspect("inspect_1_1")], config=config)
        self.assertEqual(record["status"], "failed")
        self.assertIn("budget exhausted", record["errors"][-1])

    def test_unknown_bbox_and_nonfinite_parameters_rejected(self):
        known = {label: {} for label in policy.VIEWS}
        for change in ({"yaw": float('nan')}, {"bbox": [-1, 0, 1, 1]},
                       {"bbox": [1, 1, 0, 0]}, {"reference_view": "unseen"}, {"zoom": True}):
            value = self.inspect()
            value["requests"][0].update(change)
            with self.assertRaises(ValueError):
                rich.validate_inspection(value, self.uid, known, self.config, 1)

    def test_keep_below_threshold_or_inconsistent_sum_rejected(self):
        value = self.decision(True)
        known = {"inspect_1_1": {}}
        with self.assertRaises(ValueError):
            rich.validate_decision(value, self.uid, known, replace(self.config, min_objects=30))
        value["object_count_lower_bound"] = 99
        with self.assertRaises(ValueError):
            rich.validate_decision(value, self.uid, known, self.config)

    def test_cache_changes_with_glb_threshold_renderer_and_missing_inspection_image(self):
        first, _ = self.run_model([self.inspect(), self.decision(True)])
        self.assertEqual(first["status"], "ok")
        cached, http = self.run_model([])
        self.assertTrue(cached["cached"])
        http.assert_not_called()
        Path(first["inspection_views"][0]["path"]).unlink()
        new, http = self.run_model([self.decision()])
        self.assertFalse(new["cached"])
        self.assertEqual(http.call_count, 1)
        original = new["fingerprint"]
        new, _ = self.run_model([self.decision()], config=replace(self.config, min_objects=21))
        self.assertNotEqual(original, new["fingerprint"])
        self.glb.write_bytes(b"changed source")
        changed, _ = self.run_model([self.decision()], config=replace(self.config, min_objects=21))
        self.assertNotEqual(new["fingerprint"], changed["fingerprint"])
        self.renderer.worker_sha256 = "changed worker"
        changed_worker, _ = self.run_model([self.decision()], config=replace(self.config, min_objects=21))
        self.assertNotEqual(changed["fingerprint"], changed_worker["fingerprint"])

    def test_followup_reference_uses_trusted_camera_only(self):
        known = {"inspect_1_1": {"camera": {"matrix": "trusted"}}}
        value = self.inspect("inspect_1_1")
        value["requests"][0]["reference_camera"] = "model spoof"
        result = rich.validate_inspection(value, self.uid, known, self.config, 2)
        self.assertEqual(result[0]["reference_camera"], {"matrix": "trusted"})

    def test_429_retries_and_does_not_become_quality_rejection(self):
        error = urllib.error.HTTPError(self.config.url, 429, "busy", {"Retry-After": "7"},
                                       io.BytesIO(b'{"error":{"code":"account_capacity_exhausted"}}'))
        with patch.object(rich.urllib.request, 'urlopen', side_effect=[error, Reply(self.decision())]), \
                patch.object(rich.time, 'sleep') as sleep:
            record = judge.judge_scene(self.uid, self.config, 'test-secret', self.source, renderer=self.renderer)
        self.assertEqual(record['status'], 'ok')
        self.assertEqual(record['attempts'], 2)
        self.assertIn('account_capacity_exhausted', record['errors'][0])
        sleep.assert_called_once_with(7)

    def test_depth_focus_unprojects_visible_surface_and_rejects_empty_region(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy not installed in test environment')
        import ast
        worker = Path(__file__).resolve().parents[1] / 'experiments/sol_augment/screen_view_worker.py'
        func = next(n for n in ast.parse(worker.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == '_screen_focus')
        namespace = {'np': np}
        exec(compile(ast.Module(body=[func], type_ignores=[]), str(worker), 'exec'), namespace)
        depth = np.zeros((3, 3)); depth[1, 1] = 4
        matrix = np.eye(4); matrix[:3, 3] = [10, 1, 2]
        point, _ = namespace['_screen_focus'](depth, matrix, np.pi / 2, [0, 0, 1, 1])
        np.testing.assert_allclose(point, [10, 1, -2])
        with self.assertRaisesRegex(ValueError, 'No visible'):
            namespace['_screen_focus'](np.zeros((3, 3)), matrix, np.pi / 2, [0, 0, 1, 1])

    def test_worker_bundle_is_valid_python_without_importing_render_libraries(self):
        import ast
        tree = ast.parse(worker_bundle())
        guards = [n for n in tree.body if isinstance(n, ast.If) and '__name__' in ast.unparse(n.test)]
        self.assertEqual(len(guards), 1)
        self.assertIn('screen_view_main()', ast.unparse(guards[0]))

    def test_borrowed_initial_renderer_is_not_deleted_by_inspection(self):
        from scene_factory.screening.inspection import InspectionRenderer
        shared = InspectionRenderer(self.root, pod='caller-worker')
        shared.created = True
        shared.augment = Mock()
        shared.remote = Mock()
        shared.close()
        shared.remote.close.assert_not_called()
        owned = InspectionRenderer(self.root)
        owned.created = True
        owned.augment = Mock()
        owned.remote = Mock()
        owned.close()
        owned.remote.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
