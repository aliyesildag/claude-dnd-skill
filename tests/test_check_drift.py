"""check_drift.py — the sheet is the source, and the copies have to agree.

This table keeps four copies of every PC's vitals, each for a reason: the
sheet the player owns, the party line the DM reads at a glance, the JSON the
sidebar loads at session start, and the data the printed sheet was generated
from. Four copies means four chances to disagree, and nobody reads four files
against each other by hand — which is how the printed sheets sat at XP 300
while everything else said 324.

What is pinned here: that the sheet always wins, that a stale *current* HP is
not an error (a fight is in progress), that a printed sheet is allowed to be
behind on XP but not on level, and that a name spelled four different ways —
`kizil-zenci.md`, "Kızıl Zenci", `Kızıl Zenci` — resolves to one character.

Pure functions over temp files; no display, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "dnd" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

def load(name: str = "check_drift_under_test"):
    """A fresh copy of check_drift.

    `paths` caches the campaign root at import, so a test that points the root
    at a scratch dir has to drop that cache and load again — otherwise it runs
    against the live campaign, which is how a test suite quietly edits someone's
    game. Loading a *new* module object rather than reloading keeps the
    module-level copy other tests use untouched.
    """
    for cached in ("paths", "runtime_paths"):
        sys.modules.pop(cached, None)
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "check_drift.py")
    mod = importlib.util.module_from_spec(spec)      # type: ignore
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                     # type: ignore
    return mod


cd = load()


SHEET = """# Kızıl Zenci

## Identity
- **Race:** Tiefling | **Class:** Monk | **Level:** 2 | **Background:** Hermit
- **Alignment:** Lawful Neutral | **XP:** 324 / 900

## Combat Stats
- **HP:** 12 / 17 | **Temp HP:** 0
- **AC:** 15 (unarmored) | **Initiative:** +3 | **Speed:** 40 ft
"""

CANON = {"level": 2, "xp": 324, "xp_next": 900, "hp_cur": 12, "hp_max": 17, "ac": 15}


class Folding(unittest.TestCase):
    """One character, four spellings."""

    def test_turkish_dotless_i_folds_to_the_same_key(self):
        for spelling in ("Kızıl Zenci", "kizil-zenci", "KIZIL ZENCI", "Kızıl  Zenci"):
            with self.subTest(spelling=spelling):
                self.assertEqual(cd.fold(spelling), "kizilzenci")

    def test_other_turkish_letters_fold_too(self):
        self.assertEqual(cd.fold("Ayıboğan"), cd.fold("ayibogan"))
        self.assertEqual(cd.fold("Zülfücan"), cd.fold("zulfucan"))

    def test_different_names_do_not_collide(self):
        self.assertNotEqual(cd.fold("Dilaver"), cd.fold("Dilara"))


class SheetReading(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.camp = Path(self.tmp.name)
        (self.camp / "characters").mkdir()
        (self.camp / "characters" / "kizil-zenci.md").write_text(SHEET, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_every_vital_is_read(self):
        v = cd.sheet_vitals(self.camp / "characters" / "kizil-zenci.md")
        self.assertEqual(v, CANON)

    def test_the_display_name_comes_from_the_heading_not_the_filename(self):
        self.assertEqual(list(cd.sheets(self.camp)), ["Kızıl Zenci"])

    def test_a_sheet_with_no_vitals_is_skipped(self):
        (self.camp / "characters" / "notes.md").write_text("# Notlar\nbir şey yok\n", encoding="utf-8")
        self.assertEqual(list(cd.sheets(self.camp)), ["Kızıl Zenci"])


class ReadingTheCopies(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.camp = Path(self.tmp.name)
        (self.camp / "characters").mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_the_party_line_in_state_md_is_parsed(self):
        (self.camp / "state.md").write_text(
            "## Live State Flags\n"
            "- **Party:** Kızıl Zenci — Tiefling Monk **2** | HP 12/17 | AC 15 | XP 324/900 · Focus 2/2\n",
            encoding="utf-8")
        self.assertEqual(cd.from_state_md(self.camp, "Kızıl Zenci"), CANON)

    def test_a_line_naming_the_pc_without_vitals_is_not_mistaken_for_one(self):
        (self.camp / "state.md").write_text(
            "- Kızıl Zenci 3 gün önce Söğüt'e geldi, 2 kişiyle konuştu.\n", encoding="utf-8")
        self.assertEqual(cd.from_state_md(self.camp, "Kızıl Zenci"), {})

    def test_the_players_json_shape_is_read(self):
        p = self.camp / "display-players.json"
        p.write_text(json.dumps({"players": [
            {"name": "Dilaver", "level": 2, "ac": 14},
            {"name": "Kızıl Zenci", "level": 2, "ac": 15,
             "hp": {"current": 12, "max": 17}, "xp": {"current": 324, "next": 900}}]}),
            encoding="utf-8")
        self.assertEqual(cd.from_players_json(p, "Kızıl Zenci"), CANON)

    def test_an_absent_character_reads_as_missing_not_as_zeros(self):
        p = self.camp / "display-players.json"
        p.write_text(json.dumps({"players": [{"name": "Dilaver", "level": 2}]}), encoding="utf-8")
        self.assertEqual(cd.from_players_json(p, "Kızıl Zenci"), {})

    def test_the_printed_sheets_own_shape_is_read(self):
        d = self.camp / "sv2"
        d.mkdir()
        (d / "kizil-zenci.json").write_text(json.dumps({
            "id_name": "Kızıl Zenci", "id_level": "2", "id_xp": "300 / 900",
            "combat_hp_current": "12", "combat_hp_max": "17", "combat_ac": "15"}), encoding="utf-8")
        self.assertEqual(cd.from_sheet_data(d, "Kızıl Zenci"),
                         {**CANON, "xp": 300})

    def test_unreadable_sources_are_missing_not_fatal(self):
        bad = self.camp / "broken.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(cd.from_players_json(bad, "Kızıl Zenci"), {})
        self.assertEqual(cd.from_state_md(self.camp, "Kızıl Zenci"), {})


class Comparing(unittest.TestCase):

    def test_agreement_is_silence(self):
        self.assertEqual(cd.compare(CANON, dict(CANON)), ([], []))

    def test_a_changed_vital_is_drift(self):
        drift, stale = cd.compare(CANON, {**CANON, "level": 3})
        self.assertEqual(drift, [("level", 2, 3)])
        self.assertEqual(stale, [])

    def test_current_hp_is_stale_not_drift(self):
        # A fight is in progress and the sidebar is ahead of the sheet. That is
        # the system working, not a bug.
        drift, stale = cd.compare(CANON, {**CANON, "hp_cur": 4})
        self.assertEqual(drift, [])
        self.assertEqual(stale, [("hp_cur", 12, 4)])

    def test_a_printed_sheet_may_lag_on_xp_but_not_on_level(self):
        copy = {**CANON, "xp": 300, "level": 1}
        drift, stale = cd.compare(CANON, copy, relaxed=cd.PRINTED_RELAXED)
        self.assertEqual(drift, [("level", 2, 1)], "unreprinted level-up must be drift")
        self.assertIn(("xp", 324, 300), stale, "XP on paper is a snapshot, not drift")

    def test_a_field_the_copy_does_not_carry_is_not_compared(self):
        self.assertEqual(cd.compare(CANON, {"ac": 15}), ([], []))


class EndToEnd(unittest.TestCase):
    """run() over a whole campaign, as /dm:dnd load will call it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.camp = self.root / "campaigns" / "test-masa"
        (self.camp / "characters").mkdir(parents=True)
        (self.camp / "characters" / "kizil-zenci.md").write_text(SHEET, encoding="utf-8")
        (self.camp / "state.md").write_text(
            "- **Party:** Kızıl Zenci — Tiefling Monk **2** | HP 12/17 | AC 15 | XP 324/900\n",
            encoding="utf-8")
        self.players = self.root / "display-players.json"
        self.addCleanup(self.tmp.cleanup)
        # find_campaign and runtime_dir are resolved through paths.py; point
        # both at the scratch root rather than the live campaign.
        import os
        self._env = {k: os.environ.get(k) for k in ("DND_CAMPAIGN_ROOT", "DND_RUNTIME_DIR")}
        os.environ["DND_CAMPAIGN_ROOT"] = str(self.root)
        os.environ["DND_RUNTIME_DIR"] = str(self.root / ".runtime")
        (self.root / ".runtime").mkdir(exist_ok=True)
        self.cd = load("check_drift_e2e")
        self.assertEqual(self.cd.find_campaign("test-masa"), self.camp,
                         "test would have run against the live campaign")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        import os
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_players(self, **over):
        row = {"name": "Kızıl Zenci", "level": 2, "ac": 15,
               "hp": {"current": 12, "max": 17}, "xp": {"current": 324, "next": 900}}
        row.update(over)
        self.players.write_text(json.dumps({"players": [row]}), encoding="utf-8")

    def test_a_clean_campaign_exits_zero(self):
        self._write_players()
        self.assertEqual(self.cd.run("test-masa", str(self.players), None, quiet=True), 0)

    def test_a_drifting_copy_exits_one(self):
        self._write_players(ac=11)
        self.assertEqual(self.cd.run("test-masa", str(self.players), None, quiet=True), 1)

    def test_a_campaign_with_no_sheets_says_so(self):
        for p in (self.camp / "characters").glob("*.md"):
            p.unlink()
        self.assertEqual(self.cd.run("test-masa", None, None, quiet=True), 2)

    def test_a_missing_optional_source_is_not_an_error(self):
        self.assertEqual(self.cd.run("test-masa", str(self.root / "nope.json"), None, quiet=True), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
