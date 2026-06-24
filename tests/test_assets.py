"""Asset integrity: side_quests.json schema + referenced images exist, and every
Python source compiles. (No network, no pickles — safe in CI.)"""
from __future__ import annotations

import json
import py_compile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WEB = ROOT / "web"

_TYPES = {"eatery", "park", "neighborhood", "viewpoint", "market", "landmark", "historic", "art"}
_REQUIRED = {"id", "name", "type", "lat", "lon", "neighborhood", "avg_dwell_min",
             "blurb_fr", "blurb_en", "website", "wikipedia", "image", "attribution", "source"}


class TestAssets(unittest.TestCase):
    def test_side_quests_schema(self):
        quests = json.loads((WEB / "side_quests.json").read_text(encoding="utf-8"))
        self.assertIsInstance(quests, list)
        self.assertTrue(100 <= len(quests) <= 250, f"unexpected count {len(quests)}")
        ids = set()
        for q in quests:
            self.assertEqual(_REQUIRED - set(q), set(), f"{q.get('id')} missing keys")
            self.assertIn(q["type"], _TYPES, q["id"])
            self.assertTrue(45.2 <= q["lat"] <= 45.8, f"{q['id']} lat {q['lat']}")
            self.assertTrue(-74.2 <= q["lon"] <= -73.2, f"{q['id']} lon {q['lon']}")
            self.assertIsInstance(q["avg_dwell_min"], int)
            self.assertGreater(q["avg_dwell_min"], 0)
            self.assertTrue(q["blurb_fr"] and q["blurb_en"], f"{q['id']} empty blurb")
            ids.add(q["id"])
        self.assertEqual(len(ids), len(quests), "duplicate quest ids")

    def test_quest_images_exist(self):
        quests = json.loads((WEB / "side_quests.json").read_text(encoding="utf-8"))
        for q in quests:
            img = q["image"]
            if img and not img.startswith("http"):
                self.assertTrue((WEB / img).exists(), f"missing image {img} for {q['id']}")

    def test_python_sources_compile(self):
        files = []
        for d in ("engine", "scripts", "server", "tests"):
            files += (ROOT / d).glob("*.py")
        files.append(ROOT / "config.py")
        for f in files:
            with self.subTest(file=f.name):
                py_compile.compile(str(f), doraise=True)


if __name__ == "__main__":
    unittest.main()
