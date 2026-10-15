"""Native questions need neither separable mesh groups nor fabricated copies."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_test_augment", ROOT / "experiments/sol_augment/run.py")
augment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(augment)


class NativeSceneQaTests(unittest.TestCase):
    def item(self):
        return {"level": "medium", "target": "箱子", "copies": [], "question": "入口旁的箱子是什么颜色？",
                "start": augment.VIEWS[0], "reveal": augment.VIEWS[1]}

    def test_native_medium_is_legal_but_three_tier_augmentation_still_needs_copies(self):
        augment.validate_plan({"items": [self.item()]}, [], levels=("medium",))
        items = [{**self.item(), "level": level} for level in ("simple", "medium", "hard")]
        with self.assertRaises(ValueError):
            augment.validate_plan({"items": items}, [])

    def test_native_plan_cannot_reference_unrendered_region(self):
        item = self.item();item["reveal"] = "region_45"
        with self.assertRaises(ValueError):
            augment.validate_plan({"items": [item]}, [], levels=("medium",))

    def test_empty_groups_reach_question_generation_without_missing_region_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);source = root / "source.glb";source.write_bytes(b"source-fixture")
            out = root / "run";inspect = out / "inspect";inspect.mkdir(parents=True)
            for name in ("inventory.json", "groups.json"):(inspect / name).write_text("[]")
            (inspect / "groups.png").write_bytes(b"image-fixture")
            for view in augment.VIEWS:(inspect / (view + ".png")).write_bytes(b"image-fixture")
            class Remote:
                worker_sha256 = "test-worker"
                def run(self, source, built, plan):
                    tier = built / "medium";tier.mkdir(parents=True)
                    (tier / "scene.glb").write_bytes(struct.pack("<4sII", b"glTF", 2, 12))
                    (tier / "cameras.json").write_text(json.dumps({view: {"target": [0, 0, 0]} for view in augment.VIEWS}))
                    for view in augment.VIEWS:(tier / (view + ".png")).write_bytes(b"image-fixture")
            calls = []
            def call(folder, prompt, images, feedback=""):
                self.assertTrue(all(path.exists() for _, path in images));calls.append(prompt)
                if len(calls) == 1:return {"items": [self.item()]}
                return {"items": [{"level": "medium", "answer": "蓝色", "pass": True,
                                   "reason": "初始由墙面遮挡，侧视可见箱子。"}]}
            with patch.object(augment, "ensure_inspect"), patch.object(augment, "call", call):
                result = augment.run_scene(Remote(), source, out, focus="medium", rounds=1)
            self.assertEqual(result["items"][0]["scene_mode"], "native")
            self.assertEqual(result["items"][0]["answer"], "蓝色")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":unittest.main()
