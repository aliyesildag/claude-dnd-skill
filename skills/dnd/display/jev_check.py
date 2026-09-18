#!/usr/bin/env python3
"""Two guards that need meaning rather than string distance.

`send.py` already checks names against the campaign cast and the party, and
`difflib` catches a typo. What it cannot catch is the way a table actually
talks: the DM writes `şişeci` for the Kırk who carries the bottle, `muhtar`
for Özgür Özer, `Yarımkulak` for Savaş. Those are not misspellings, they are
the name the scene used, and every one of them fails a string match.

The dice guard covers the other silent failure: a request whose die does not
match what it is for. A hit die sent as `1d20` looks correct in every log and
is wrong at the table, which is exactly what happened when four players rolled
level-up HP on a d20.

Both are best effort. No key, no network, low confidence: the caller keeps its
own answer. A guard that blocks a session because a service is down is worse
than the mistake it prevents.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
KEY_FILE = Path.home() / ".config" / "typesafe" / "api.key"
TIMEOUT = 6

# A name is substituted into a live send, so it needs to be clearly the best
# reading and not merely the top of a flat distribution.
NAME_MIN_CONFIDENCE = 0.70
DIE_MIN_CONFIDENCE = 0.60


def _key() -> str:
    env = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ask(state, questions) -> dict:
    key = _key()
    if not key:
        return {}
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("answers", {})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {}


def cast_with_roles(campaign: str) -> dict:
    """Campaign names mapped to what each one is, from the campaign graph.

    A bare name list cannot answer "who is the muhtar" — the answer lives in
    the role, not the spelling. The graph already carries a one-line summary
    per node, which is exactly the description this judgment needs.
    """
    root = Path(os.environ.get("DND_CAMPAIGN_ROOT", Path.home() / ".claude" / "dnd"))
    graph = root / "campaigns" / campaign / "graph.json"
    try:
        nodes = json.loads(graph.read_text(encoding="utf-8")).get("nodes") or []
    except (OSError, ValueError):
        return {}
    return {n["name"]: (n.get("summary") or n["name"])
            for n in nodes if n.get("type") in ("npc", "pc") and n.get("name")}


def sheet_line(campaign: str, character: str) -> str:
    """One line of who this character is, for a judgment that needs the class.

    "Is 1d20 right for a level-up hit die" has no answer without knowing the
    class, and that is the exact question the dice guard has to settle.
    """
    root = Path(os.environ.get("DND_CAMPAIGN_ROOT", Path.home() / ".claude" / "dnd"))
    folder = root / "campaigns" / campaign / "characters"
    if not (campaign and character and folder.is_dir()):
        return ""
    wanted = character.strip().lower()
    for sheet in folder.glob("*.md"):
        try:
            text = sheet.read_text(encoding="utf-8")
        except OSError:
            continue
        title = text.lstrip().splitlines()[0].lstrip("# ").strip().lower()
        if wanted not in (title, sheet.stem.lower()):
            continue
        bits = [ln.strip("- ").strip() for ln in text.splitlines()
                if ln.startswith("- **Race:**") or ln.startswith("- **Hit Dice:**")]
        return " · ".join(bits)
    return ""


def skill_modifier(campaign: str, character: str, skill: str) -> "int | None":
    """The character's bonus for a skill, read off their own sheet.

    Arithmetic is not a judgment. The model says which check; the number behind
    it is already written down, and reading it here keeps the two from ever
    disagreeing.
    """
    root = Path(os.environ.get("DND_CAMPAIGN_ROOT", Path.home() / ".claude" / "dnd"))
    folder = root / "campaigns" / campaign / "characters"
    if not (campaign and character and skill and folder.is_dir()):
        return None
    wanted = character.strip().lower()
    for sheet in folder.glob("*.md"):
        try:
            text = sheet.read_text(encoding="utf-8")
        except OSError:
            continue
        title = text.lstrip().splitlines()[0].lstrip("# ").strip().lower()
        if wanted not in (title, sheet.stem.lower()):
            continue
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
            if len(cells) >= 3 and cells[0].lower() == skill.strip().lower():
                try:
                    return int(cells[2].replace("+", ""))
                except ValueError:
                    return None
    return None


def resolve_name(written: str, candidates, kind: str = "kişi") -> "tuple[str, float]":
    """Map what the DM wrote onto a name the campaign knows.

    Returns (canonical_name, confidence), or ("", 0.0) when the guard cannot
    help. `candidates` is the closed set the answer must come from — a list of
    names, or a {name: what they are} map, which is what lets a role, a trade
    or a nickname resolve at all.
    """
    if isinstance(candidates, dict):
        described = {k: v for k, v in candidates.items() if k}
    else:
        described = {c: c for c in candidates if c}
    names = list(described)
    if not names or not written.strip():
        return "", 0.0
    answers = _ask(
        {"yazilan": written, "taninan_kisiler": described},
        {
            "isim": {
                "type": "choice",
                "instructions": (
                    f"Yazılan ifade hangi {kind}yi kastediyor? Lakap, unvan, meslek adı ya da "
                    "sahnedeki tanımlama olabilir, birebir yazım beklenmiyor. Hiçbiri açıkça "
                    "uymuyorsa 'yok' seç."
                ),
                "criteria": {**described, "yok": "Listedeki hiçbiri kastedilmiyor."},
            }
        },
    )
    a = answers.get("isim") or {}
    name = a.get("choice") or ""
    conf = float(a.get("confidence") or 0.0)
    if name in ("", "yok") or conf < NAME_MIN_CONFIDENCE:
        return "", conf
    return name, conf


def check_dice(label: str, spec: str, character: str = "", sheet: str = "") -> "tuple[bool, str, float]":
    """Does this die fit what the roll is for?

    Returns (ok, expected_spec, confidence). `ok` is False only when the guard
    is confident the die is wrong; anything softer leaves the call alone.
    """
    if not label.strip() or not spec.strip():
        return True, "", 0.0
    dice = {
        "1d20": "Yetenek testi, saldırı, saving throw, initiative. Belirsiz sonucu olan her test.",
        "1d4": "d4 hasar ya da küçük bir zar: hançer, bazı iyileştirmeler.",
        "1d6": "d6 hasar ya da Bardic Inspiration gibi bir yardım zarı.",
        "1d8": "d8: orta silah hasarı, ya da d8 can zarı olan bir sınıfın seviye atlama can zarı (druid, bard, monk, rogue).",
        "1d10": "d10: ağır silah hasarı, ya da d10 can zarı olan bir sınıfın seviye atlama can zarı (fighter, paladin, ranger).",
        "1d12": "d12: en ağır silah hasarı ya da barbarın can zarı.",
    }
    answers = _ask(
        {"istek_basligi": label, "gonderilen_zar": spec,
         "karakter": character or "(belirtilmedi)",
         "karakter_kunyesi": sheet or "(sınıf bilgisi verilmedi)"},
        {
            "beklenen": {
                "type": "choice",
                "instructions": (
                    "Bu başlıkta anlatılan atış için doğru zar hangisi? Başlığın ne için "
                    "istendiğine bak, gönderilen zara göre karar verme."
                ),
                "criteria": dice,
            }
        },
    )
    a = answers.get("beklenen") or {}
    expected = a.get("choice") or ""
    conf = float(a.get("confidence") or 0.0)
    if not expected or conf < DIE_MIN_CONFIDENCE:
        return True, expected, conf
    return (expected == spec.strip().lower()), expected, conf


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Jev guards for names and dice.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("name", help="resolve a written name against a candidate list")
    n.add_argument("--written", required=True)
    n.add_argument("--candidates", default="", help="comma-separated; omit to use --campaign")
    n.add_argument("--campaign", default="", help="pull names and roles from this campaign's graph")
    n.add_argument("--kind", default="kişi")

    d = sub.add_parser("dice", help="check a dice spec against its label")
    d.add_argument("--label", required=True)
    d.add_argument("--spec", required=True)
    d.add_argument("--character", default="")
    d.add_argument("--sheet", default="", help="one line: class and hit die, e.g. 'Druid 2, hit die d8'")

    args = ap.parse_args()
    if args.cmd == "name":
        pool = (cast_with_roles(args.campaign) if args.campaign
                else [c.strip() for c in args.candidates.split(",")])
        name, conf = resolve_name(args.written, pool, args.kind)
        print(json.dumps({"isim": name or None, "guven": round(conf, 3)}, ensure_ascii=False))
    else:
        ok, expected, conf = check_dice(args.label, args.spec, args.character, args.sheet)
        print(json.dumps({"uyumlu": ok, "beklenen": expected or None, "guven": round(conf, 3)},
                         ensure_ascii=False))
