import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest

from scene_factory.audit_sample import environment, sample_catalog, write_sample
from scene_factory.audit_gallery import render_audit_gallery


class AuditSampleTests(unittest.TestCase):
    def test_balanced_reproducible_and_diverse(self):
        rows = []
        categories = {"indoor": ["居家室内", "办公与公共室内", "商业公共空间",
                                  "多房间与多层住宅"],
                      "outdoor": ["林地营地", "庭院花园", "街角巷道", "城镇村落"],
                      "unclassified": ["其他主题场景（待细分）", "住宅与公寓"]}
        for tier in ("simple", "complex"):
            for kind, names in categories.items():
                for index in range(9):
                    uid = f"{tier}-{kind}-{index}"
                    rows.append({"uid": uid, "tier": tier,
                                 "category": names[index % len(names)],
                                 "author": f"author-{uid}", "bytes": 1_000,
                                 "download_url": f"https://example.test/{uid}.glb"})
        rows += [{"uid": "oversize", "tier": "simple", "category": "林地营地",
                  "author": "large", "bytes": 101_000_000,
                  "download_url": "https://example.test/oversize.glb"}]
        first = sample_catalog(rows, seed=42)
        self.assertEqual(first, sample_catalog(list(reversed(rows)), seed=42))
        self.assertEqual(len(first), 20)
        self.assertEqual(len({x["uid"] for x in first}), 20)
        self.assertEqual(len({x["author"] for x in first}), 20)
        self.assertTrue(all(x["uid"] != "oversize" for x in first))
        self.assertLessEqual(max(Counter(x["category"] for x in first).values()), 2)
        self.assertEqual(Counter((x["tier"], x["audit_environment"]) for x in first),
                         Counter({(tier, kind): quota
                                  for tier in ("simple", "complex")
                                  for kind, quota in (("indoor", 4), ("outdoor", 4),
                                                      ("unclassified", 2))}))

    def test_unknown_categories_are_not_forced_into_indoor_or_outdoor(self):
        self.assertEqual(environment("住宅与公寓"), "unclassified")
        self.assertEqual(environment("街角巷道"), "outdoor")
        self.assertEqual(environment("居家室内"), "indoor")

    def test_followup_wave_excludes_prior_sample(self):
        rows = []
        for tier in ("simple", "complex"):
            for kind, categories in (("indoor", ("居家室内", "办公与公共室内",
                                                   "商业公共空间", "多房间与多层住宅")),
                                     ("outdoor", ("林地营地", "庭院花园", "街角巷道", "城镇村落")),
                                     ("unclassified", ("其他主题场景（待细分）", "住宅与公寓"))):
                for index in range(30):
                    uid = f"{tier}-{kind}-{index}"
                    rows.append({"uid": uid, "tier": tier,
                                 "category": categories[index % len(categories)],
                                 "author": uid, "bytes": 100,
                                 "download_url": f"https://example.test/{uid}.glb"})
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.jsonl"
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            catalog.write_text("".join(json.dumps(row) + "\n" for row in rows))
            write_sample(catalog, first, seed=7)
            result = write_sample(catalog, second, seed=8, exclude_catalogs=[first])
            first_ids = {json.loads(line)["uid"] for line in first.read_text().splitlines()}
            second_ids = {json.loads(line)["uid"] for line in second.read_text().splitlines()}
            self.assertFalse(first_ids & second_ids)
            self.assertEqual(result["excluded_scenes"], 20)

    def test_insufficient_quota_fails_without_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.jsonl"
            output = Path(tmp) / "sample.jsonl"
            catalog.write_text(json.dumps({"uid": "one", "tier": "simple",
                                           "category": "居家室内", "bytes": 100,
                                           "download_url": "https://example.test/one.glb"}) + "\n")
            with self.assertRaisesRegex(ValueError, "Not enough eligible"):
                write_sample(catalog, output)
            self.assertFalse(output.exists())

    def test_gallery_links_existing_previews_and_escapes_source_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "sample.jsonl"
            catalog.write_text(json.dumps({"uid": "one", "tier": "simple", "category": "居家室内",
                                           "audit_environment": "indoor", "bytes": 100,
                                           "name": "<script>bad</script>",
                                           "source_url": "https://example.test/one"}) + "\n")
            inspect = root / "runs/one/inspect"
            inspect.mkdir(parents=True)
            for view in ("front", "back", "side", "top"):
                (inspect / f"source_{view}.png").write_bytes(b"PNG")
            page = root / "runs/index.html"
            result = render_audit_gallery(catalog, root / "runs", page)
            self.assertEqual(result["previews_complete"], 1)
            self.assertIn("one/inspect/source_front.png", page.read_text())
            self.assertNotIn("<script>bad</script>", page.read_text())


if __name__ == "__main__":
    unittest.main()
