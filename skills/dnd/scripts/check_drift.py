#!/usr/bin/env python3
"""check_drift.py — one home for a character's vitals, and a check that the copies agree.

INVARIANT:
    A mutable vital — Level, XP, max HP, AC — has exactly one home: the
    character sheet, `characters/<name>.md`. Everything else is a copy, and a
    copy is the only thing that can drift. This table keeps more copies than
    most, each for a good reason, which is exactly why they need checking:

      state.md                      the at-a-glance party line the DM reads
      display-players.json          what push_stats loads into the sidebar at session start
      <runtime>/stats.json          what the sidebar is showing right now
      kagit-verisi/sv2/<name>.json  what the printed character sheet was generated from

    Found on the first run: the printed sheet's data said XP 300 while the
    sheet, state.md and the display all said 324. Nobody had noticed, because
    nobody reads four files against each other by hand.

WHAT IT DOES:
    Reads the canonical vitals from each PC sheet and compares every copy it
    can find. The sheet always wins; this script never edits anything.

      DRIFT      a copy disagrees with the sheet            → exit 1
      STALE      current HP differs (live state, informational only)
      MISSING    a source has no entry for this PC          → noted, not an error

USAGE:
    python3 check_drift.py --campaign <name> [--players-json FILE] [--sheet-data DIR] [--quiet]

    `--players-json` and `--sheet-data` point at the table's repo, which lives
    outside the campaign dir (defaults: $DND_TABLE_DIR/display-players.json and
    $DND_TABLE_DIR/kagit-verisi/sv2). Wired into /dm:dnd load and /dm:dnd save.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from paths import find_campaign, runtime_dir  # noqa: E402

# Canonical vital patterns on the sheet (the templates' own conventions).
_SHEET = {
    "level":  re.compile(r"\*\*Level:\*\*\s*(\d+)"),
    "xp":     re.compile(r"\*\*XP:\*\*\s*(\d+)\s*/\s*(\d+)"),
    "hp":     re.compile(r"\*\*HP:\*\*\s*(\d+)\s*/\s*(\d+)"),
    "ac":     re.compile(r"\*\*AC:\*\*\s*(\d+)"),
}
STRICT = ("level", "xp", "xp_next", "hp_max", "ac")      # must match
LIVE = ("hp_cur",)                                       # may differ mid-session
# A printed sheet is a snapshot by nature: XP moves every session and nobody
# reprints for it. Level, max HP and AC on paper still have to be right — a
# level-up that was never reprinted is a real drift at the table.
PRINTED_RELAXED = ("xp", "xp_next")


def fold(name: str) -> str:
    """Compare names across files that spell them differently (`kizil-zenci.md`
    vs "Kızıl Zenci"): strip diacritics — Turkish dotless ı included, which
    str.lower() gets wrong — and drop everything but letters and digits."""
    table = str.maketrans({"ı": "i", "İ": "i", "I": "i", "ğ": "g", "Ğ": "g", "ş": "s", "Ş": "s",
                           "ö": "o", "Ö": "o", "ü": "u", "Ü": "u", "ç": "c", "Ç": "c"})
    s = unicodedata.normalize("NFKD", name.translate(table))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── the canonical source ──────────────────────────────────────────────────────

def sheet_vitals(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict = {}
    m = _SHEET["level"].search(text)
    if m: out["level"] = int(m.group(1))
    m = _SHEET["xp"].search(text)
    if m: out["xp"], out["xp_next"] = int(m.group(1)), int(m.group(2))
    m = _SHEET["hp"].search(text)
    if m: out["hp_cur"], out["hp_max"] = int(m.group(1)), int(m.group(2))
    m = _SHEET["ac"].search(text)
    if m: out["ac"] = int(m.group(1))
    return out


def sheets(camp: pathlib.Path) -> dict:
    """{display-ish name: vitals} from characters/*.md — the name is the file's
    H1 when it has one, else the stem."""
    out = {}
    for p in sorted((camp / "characters").glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
        name = m.group(1).strip() if m else p.stem
        v = sheet_vitals(p)
        if v:
            out[name] = v
    return out


# ── the copies ────────────────────────────────────────────────────────────────

def from_state_md(camp: pathlib.Path, name: str) -> dict:
    """Numbers on a state.md line that names this PC, read the way the party
    line writes them: `**2** | HP 24/24 | AC 14 | XP 324/900`."""
    try:
        text = (camp / "state.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    want = fold(name)
    for line in text.splitlines():
        if fold(line).find(want) < 0 or "HP" not in line:
            continue
        out: dict = {}
        m = re.search(r"HP\s*(\d+)\s*/\s*(\d+)", line)
        if m: out["hp_cur"], out["hp_max"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"\bAC\s*(\d+)", line)
        if m: out["ac"] = int(m.group(1))
        m = re.search(r"XP\s*(\d+)\s*/\s*(\d+)", line)
        if m: out["xp"], out["xp_next"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"\*\*(\d+)\*\*", line)
        if m: out["level"] = int(m.group(1))
        return out
    return {}


def from_players_json(path: pathlib.Path, name: str) -> dict:
    """display-players.json and the runtime stats.json share one shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    want = fold(name)
    for p in data.get("players", []):
        if fold(str(p.get("name", ""))) != want:
            continue
        hp, xp = p.get("hp") or {}, p.get("xp") or {}
        out = {"level": _int(p.get("level")), "ac": _int(p.get("ac")),
               "hp_cur": _int(hp.get("current")), "hp_max": _int(hp.get("max")),
               "xp": _int(xp.get("current")), "xp_next": _int(xp.get("next"))}
        return {k: v for k, v in out.items() if v is not None}
    return {}


def from_sheet_data(dirpath: pathlib.Path, name: str) -> dict:
    """The printed sheet's source (make_character_sheet.py's sv2 JSON)."""
    want = fold(name)
    for p in sorted(dirpath.glob("*.json")):
        if fold(p.stem) != want:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {"level": _int(d.get("id_level")), "ac": _int(d.get("combat_ac")),
               "hp_cur": _int(d.get("combat_hp_current")), "hp_max": _int(d.get("combat_hp_max"))}
        m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(d.get("id_xp", "")))
        if m:
            out["xp"], out["xp_next"] = int(m.group(1)), int(m.group(2))
        return {k: v for k, v in out.items() if v is not None}
    return {}


# ── the comparison ────────────────────────────────────────────────────────────

def compare(canon: dict, copy: dict, relaxed=()) -> "tuple[list, list]":
    """(drift, stale): fields where the copy disagrees with the sheet.
    `relaxed` fields are reported as stale rather than drift."""
    def diff(keys):
        return [(k, canon[k], copy[k]) for k in keys if k in canon and k in copy and canon[k] != copy[k]]
    strict = tuple(k for k in STRICT if k not in relaxed)
    return diff(strict), diff(LIVE + tuple(k for k in STRICT if k in relaxed))


def run(campaign: str, players_json=None, sheet_data=None, quiet: bool = False) -> int:
    camp = find_campaign(campaign)
    canon = sheets(camp)
    if not canon:
        print(f"check_drift: no character sheets under {camp / 'characters'}")
        return 2

    sources = [("state.md", lambda n: from_state_md(camp, n))]
    if players_json and pathlib.Path(players_json).exists():
        sources.append(("display-players.json", lambda n, p=pathlib.Path(players_json): from_players_json(p, n)))
    rt_stats = runtime_dir() / "stats.json"
    if rt_stats.exists():
        sources.append(("runtime stats.json", lambda n: from_players_json(rt_stats, n)))
    if sheet_data and pathlib.Path(sheet_data).is_dir():
        sources.append(("printed sheet data", lambda n, d=pathlib.Path(sheet_data): from_sheet_data(d, n)))
    relaxed_for = {"printed sheet data": PRINTED_RELAXED}

    drifted = 0
    for name, vitals in canon.items():
        lines = []
        for label, reader in sources:
            copy = reader(name)
            if not copy:
                lines.append(f"    {label:22s} —  missing")
                continue
            drift, stale = compare(vitals, copy, relaxed_for.get(label, ()))
            if drift:
                drifted += 1
                detail = ", ".join(f"{k} {c}≠{v}" for k, c, v in drift)
                lines.append(f"    {label:22s} ✗  DRIFT  {detail}   (sheet wins)")
            elif stale:
                detail = ", ".join(f"{k} {c}/{v}" for k, c, v in stale)
                why = "print is behind, reprint at level-up" if label == "printed sheet data" else "live HP, fine"
                lines.append(f"    {label:22s} ~  stale  {detail}   ({why})")
            else:
                lines.append(f"    {label:22s} ✓")
        if not quiet or any("DRIFT" in l for l in lines):
            summary = " · ".join(f"{k}={v}" for k, v in vitals.items())
            print(f"{name}  [{summary}]")
            print("\n".join(lines))
    if drifted:
        print(f"\ncheck_drift: {drifted} DRIFT — fix the copies from the sheet (the sheet is the source).")
        return 1
    if not quiet:
        print("\ncheck_drift: no drift.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Check every copy of a PC's vitals against the sheet.")
    ap.add_argument("--campaign", required=True)
    table = os.environ.get("DND_TABLE_DIR", "").strip()
    ap.add_argument("--players-json", default=(os.path.join(table, "display-players.json") if table else None),
                    help="display-players.json in the table's repo (default: $DND_TABLE_DIR/display-players.json)")
    ap.add_argument("--sheet-data", default=(os.path.join(table, "kagit-verisi", "sv2") if table else None),
                    help="directory of the printed sheets' JSON (default: $DND_TABLE_DIR/kagit-verisi/sv2)")
    ap.add_argument("--quiet", action="store_true", help="print only characters with drift")
    a = ap.parse_args()
    return run(a.campaign, a.players_json, a.sheet_data, a.quiet)


if __name__ == "__main__":
    sys.exit(main())
