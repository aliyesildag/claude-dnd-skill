"""The die a prescribed roll uses is the server's, not the page's.

This is the failure that looks correct in the log: the pad locks itself to the
die it was last told to roll, a stale page submits that one under the new
label, and `Dilaver rolls 1d20 — Tactical Mind — ek zar` reads like a real
line. It cost this table a d10 the first time the response window shipped.

The endpoint is exercised through Flask's test client rather than by calling
helpers, because the correction has to happen on the request path a browser
actually takes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:
    _HAS_FLASK = False


def _load_app(root: Path):
    for p in (str(DISPLAY), str(SKILL / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import os
    os.environ["DND_CAMPAIGN_ROOT"] = str(root)
    os.environ["DND_RESPONSE_WINDOW_SECONDS"] = "0"   # the window is not under test here
    spec = importlib.util.spec_from_file_location("dndapp_die", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dndapp_die"] = mod
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class PrescribedDie(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "campaigns").mkdir(parents=True)
        (root / ".runtime").mkdir()
        cls.app_mod = _load_app(root)
        cls.client = cls.app_mod.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _request(self, spec, label="Tactical Mind — ek zar"):
        r = self.client.post("/dice-request", json={"characters": ["Dilaver"], "spec": spec,
                                                    "modifier": 0, "label": label})
        return r.get_json()["request_id"]

    def _roll(self, spec, request_id, modifier=0):
        r = self.client.post("/player-input/dice",
                             json={"character": "Dilaver", "spec": spec, "modifier": modifier,
                                   "label": "Tactical Mind — ek zar", "request_id": request_id})
        return r.get_json()

    def test_a_stale_page_cannot_swap_the_die(self):
        rid = self._request("1d10")
        out = self._roll("1d20", rid)          # what a locked-to-d20 pad sends
        self.assertEqual(out["spec"], "1d10")
        self.assertTrue(1 <= out["total"] <= 10, out["total"])

    def test_the_correction_is_stated_on_the_feed(self):
        rid = self._request("1d8")
        out = self._roll("1d20", rid)
        self.assertIn("istenen zar 1d8", out["text"])
        self.assertIn("1d20 göndermişti", out["text"])

    def test_an_agreeing_page_is_left_alone(self):
        rid = self._request("1d10")
        out = self._roll("1d10", rid)
        self.assertEqual(out["spec"], "1d10")
        self.assertNotIn("istenen zar", out["text"])

    def test_a_free_roll_is_not_second_guessed(self):
        # No request_id: nobody prescribed anything, so the pad is the authority.
        r = self.client.post("/player-input/dice",
                             json={"character": "Dilaver", "spec": "2d6", "modifier": 0})
        out = r.get_json()
        self.assertEqual(out["spec"], "2d6")
        self.assertNotIn("istenen zar", out["text"])

    def test_an_unknown_request_id_is_not_second_guessed(self):
        out = self._roll("1d20", "deadbeef")
        self.assertEqual(out["spec"], "1d20")


if __name__ == "__main__":
    unittest.main()
