"""The window between a failed roll and its consequence.

What is tested here is the part that has to be right when the model is not
reachable and when it is: which features a sheet actually yields, which roll
a spend implies, and that a second identical window costs nothing.

The model itself is stubbed. These are not tests of Jev's judgment — they are
tests that the code around it asks once, caches the answer, respects the
floor, and never offers a resource the display says is already spent.
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
if str(DISPLAY) not in sys.path:
    sys.path.insert(0, str(DISPLAY))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, str(DISPLAY / f"{name}.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


jev_check = _load("jev_check")
jev_window = _load("jev_window")


# A sheet written the way this table writes them: bold-named bullets with the
# rule spelled out, a `·` run of small traits, an indented sub-list, and the
# origin feat as a bold paragraph outside the bullets.
SHEET = """# Dilaver
**Player:** Feyza  **Campaign:** temiz-kagit

## Features & Traits

### Fighter (Class)
- **Second Wind.** Bonus Action ile 1d10+1 HP. **2 kullanım**.
- **Tactical Mind (2. seviye).** Bir ability check'te başarısız olursan Second Wind
  kullanımlarından birini harcayıp zara **1d10** ekleyebilirsin.
- **Monk's Focus.** 2 Focus Point.
  - *Flurry of Blows:* 1 point, Bonus Action.
  - *Patient Defense:* 1 point, Disengage + Dodge.

### Human (Species)
- Darkvision 60 · Fey Ancestry (Charmed'e karşı advantage) · Trance (4 saat)

**Origin Feat (Soldier):** **Savage Attacker** — turda bir kez hasar zarlarını iki kez atar.

## Equipment & Inventory
- Longsword
"""


class ParseFeatures(unittest.TestCase):
    def setUp(self):
        self.features = jev_window.parse_features(SHEET)
        self.names = [f["ad"] for f in self.features]

    def test_bold_named_bullets_are_one_feature_each(self):
        self.assertIn("Second Wind", self.names)
        self.assertIn("Tactical Mind", self.names)

    def test_rule_text_travels_with_the_feature(self):
        tactical = next(f for f in self.features if f["ad"] == "Tactical Mind")
        self.assertIn("1d10", tactical["metin"])
        self.assertIn("ability check", tactical["metin"])

    def test_interpunct_run_splits_into_separate_traits(self):
        self.assertIn("Darkvision 60", self.names)
        self.assertIn("Trance", self.names)

    def test_indented_sublist_stays_with_its_parent(self):
        focus = next(f for f in self.features if f["ad"] == "Monk's Focus")
        self.assertIn("Flurry of Blows", focus["metin"])
        self.assertNotIn("Flurry of Blows", self.names)

    def test_origin_feat_paragraph_is_picked_up(self):
        self.assertIn("Savage Attacker", self.names)

    def test_sections_after_features_are_not_read(self):
        self.assertFalse(any("Longsword" in f["metin"] for f in self.features))

    def test_a_sheet_without_the_section_yields_nothing(self):
        self.assertEqual(jev_window.parse_features("# Nobody\n\n## Equipment\n- rope\n"), [])


class RollKind(unittest.TestCase):
    def test_router_label_resolves_to_an_ability_check(self):
        self.assertEqual(jev_window.roll_kind("Survival — kara özü topluyorum"),
                         "yetenek testi")

    def test_saves_and_attacks_are_distinguished(self):
        self.assertEqual(jev_window.roll_kind("DEX saving throw"), "kurtarma atışı")
        self.assertEqual(jev_window.roll_kind("Longsword attack"), "saldırı atışı")

    def test_unknown_label_falls_back_to_the_common_case(self):
        self.assertEqual(jev_window.roll_kind(""), "yetenek testi")


class FollowUp(unittest.TestCase):
    def test_reroll_keeps_the_original_die_and_modifier(self):
        self.assertEqual(
            jev_window.follow_up("yeniden_at", "1d20", 5, "normal"),
            {"spec": "1d20", "modifier": 5, "advantage": "normal", "kind": "yeniden"})

    def test_advantage_rerolls_with_advantage(self):
        self.assertEqual(jev_window.follow_up("avantaj", "1d20", 5, "normal")["advantage"],
                         "advantage")

    def test_a_bonus_die_rolls_bare(self):
        # The total it is added to already carries the modifier.
        self.assertEqual(jev_window.follow_up("1d10_ekle", "1d20", 5, "normal"),
                         {"spec": "1d10", "modifier": 0, "advantage": "normal", "kind": "ek"})

    def test_anything_else_is_the_dms_to_resolve(self):
        self.assertIsNone(jev_window.follow_up("diger", "1d20", 0, "normal"))
        self.assertIsNone(jev_window.follow_up("", "1d20", 0, "normal"))


class Offers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        chars = root / "campaigns" / "temiz-kagit" / "characters"
        chars.mkdir(parents=True)
        (chars / "dilaver.md").write_text(SHEET, encoding="utf-8")

        self._env = jev_window.os.environ.get("DND_CAMPAIGN_ROOT")
        jev_window.os.environ["DND_CAMPAIGN_ROOT"] = str(root)
        jev_window.CACHE_FILE = root / "cache.json"

        self.calls = []
        jev_check._ask = self._ask                      # noqa: SLF001 — the seam under test

    def tearDown(self):
        if self._env is None:
            jev_window.os.environ.pop("DND_CAMPAIGN_ROOT", None)
        else:
            jev_window.os.environ["DND_CAMPAIGN_ROOT"] = self._env
        self.tmp.cleanup()

    def _ask(self, state, questions, timeout=None):
        """Answer as the model would: Tactical Mind applies, nothing else does."""
        self.calls.append(questions)
        out = {}
        for qid in questions:
            key = qid.split("_")[0]
            is_tactical = state["ozellikler"][key]["ad"] == "Tactical Mind"
            if qid.endswith("_uygun"):
                out[qid] = {"type": "noul", "noul": 0.91 if is_tactical else 0.04}
            elif qid.endswith("_etki"):
                out[qid] = {"choice": "1d10_ekle" if is_tactical else "diger", "confidence": 0.88}
            elif qid.endswith("_kaynak"):
                out[qid] = {"choice": "sinirli_kullanim" if is_tactical else "bedava",
                            "confidence": 0.8}
        return out

    def test_only_the_legal_feature_is_offered(self):
        offers = jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"],
                                   "yetenek testi", passed=False)
        self.assertEqual([o["feature"] for o in offers], ["Tactical Mind"])
        self.assertEqual(offers[0]["etki"], "1d10_ekle")

    def test_heroic_inspiration_comes_from_the_counter_not_the_sheet(self):
        offers = jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"],
                                   "yetenek testi", passed=False,
                                   inspiration={"Dilaver": True})
        self.assertEqual(offers[0]["feature"], "Heroic Inspiration")
        self.assertEqual(offers[0]["etki"], "yeniden_at")

    def test_inspiration_already_spent_is_not_offered(self):
        offers = jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"],
                                   "yetenek testi", passed=False,
                                   inspiration={"Dilaver": False})
        self.assertNotIn("Heroic Inspiration", [o["feature"] for o in offers])

    def test_an_identical_window_asks_nothing(self):
        jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "yetenek testi", passed=False)
        before = len(self.calls)
        again = jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "yetenek testi",
                                  passed=False)
        self.assertEqual(len(self.calls), before)          # the window opens off the cache
        self.assertEqual([o["feature"] for o in again], ["Tactical Mind"])

    def test_a_different_roll_kind_is_a_different_question(self):
        jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "yetenek testi", passed=False)
        before = len(self.calls)
        jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "kurtarma atışı", passed=False)
        self.assertGreater(len(self.calls), before)
        # …and only legality is re-asked; what the feature does has not changed.
        asked = self.calls[-1]
        self.assertTrue(all(q.endswith("_uygun") for q in asked), asked.keys())

    def test_cache_survives_a_new_process(self):
        jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "yetenek testi", passed=False)
        self.assertTrue(jev_window.CACHE_FILE.exists())
        stored = json.loads(jev_window.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertTrue(any(k.startswith("legal:") for k in stored))
        self.assertTrue(any(k.startswith("shape:") for k in stored))

    def test_no_answer_means_no_offer(self):
        jev_check._ask = lambda state, questions, timeout=None: {}
        self.assertEqual(
            jev_window.offers("temiz-kagit", "Dilaver", ["Dilaver"], "yetenek testi",
                              passed=False),
            [])

    def test_a_missing_sheet_is_not_an_error(self):
        self.assertEqual(
            jev_window.offers("temiz-kagit", "Kimse", ["Kimse"], "yetenek testi", passed=False),
            [])


if __name__ == "__main__":
    unittest.main()
