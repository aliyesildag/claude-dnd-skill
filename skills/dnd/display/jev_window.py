#!/usr/bin/env python3
"""What a player may still spend, in the seconds after the die stops.

A roll that fails is not over. Heroic Inspiration rerolls it, Tactical Mind
spends Second Wind to add 1d10 to a failed ability check, a held Bardic
Inspiration die goes on after the number is known. Resolving the outcome the
instant the die lands deletes that window, and with it the part of the turn
the player actually plays.

So the roll and its consequence are separated by a short window, and this
module answers the only question the window needs: of everything this table
is carrying, what can legally be spent on *this* roll, right now.

Eligibility cannot be a table. `## Features & Traits` is prose, written per
character, in two languages, and the same feature reads differently on every
sheet. It is also not free text the model should improvise on: the answer is
a yes or no about a rule, so it is asked as one Noul per feature over the
sheet's own words, and the arithmetic behind it stays in code.

The question is deliberately about legality alone — the kind of roll, whether
it failed, and whether the holder is the one who rolled. Whether spending is
*wise* is the player's call, and keeping it out means an answer depends only
on the feature's text, so it is cached and the window opens instantly from
the second time a feature is seen onwards.

Two things code owns outright, because they are counters rather than
judgments: whether the character is holding Heroic Inspiration right now
(the display tracks it), and which die a follow-up roll uses.

    python3 jev_window.py --campaign temiz-kagit --roller Dilaver \
        --kind "yetenek testi" --failed --present Dilaver,Hisrayt

Prints the offers as JSON. Exit 0 always: no offers is a normal answer, and a
window that blocks the table because a service is down is worse than no
window at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import jev_check

# Offering a button is cheap and reversible — the player still has to press it,
# and a wrong offer costs one glance. Refusing a legal one costs the feature.
# So this floor sits below the one send.py uses to substitute a name unasked.
OFFER_MIN = 0.60

# Per character, and per window. A sheet with more than this has a formatting
# problem rather than that many spendable features, and the window has to open
# in the time it takes a die to settle.
MAX_FEATURES_PER_SHEET = 12
MAX_CHARACTERS = 6

# Feature text longer than this is a subsystem write-up, not a feature the
# player is about to spend. Truncating keeps the request small.
MAX_FEATURE_CHARS = 420

_ROLL_KINDS = ("yetenek testi", "saldırı atışı", "kurtarma atışı")

# Skills resolve to ability checks without asking anyone. The router already
# produces labels of the form "Survival — <declaration>", so this covers the
# path the window is actually opened from.
_SKILL_WORDS = {s.lower() for s in (
    "Acrobatics", "Animal Handling", "Arcana", "Athletics", "Deception", "History",
    "Insight", "Intimidation", "Investigation", "Medicine", "Nature", "Perception",
    "Performance", "Persuasion", "Religion", "Sleight of Hand", "Stealth", "Survival",
)}


def _runtime_dir() -> Path:
    try:
        from runtime_paths import rt
        return Path(rt(""))
    except Exception:
        return Path(os.environ.get("DND_CAMPAIGN_ROOT", str(Path.home() / ".claude" / "dnd"))) / ".runtime"


CACHE_FILE = _runtime_dir() / "jev_window_cache.json"


# ── sheets ───────────────────────────────────────────────────────────────────


def _sheet_path(campaign: str, character: str) -> "Path | None":
    root = Path(os.environ.get("DND_CAMPAIGN_ROOT", str(Path.home() / ".claude" / "dnd")))
    folder = root / "campaigns" / campaign / "characters"
    if not folder.is_dir():
        return None
    wanted = jev_check.fold(character)
    for sheet in sorted(folder.glob("*.md")):
        try:
            first = sheet.read_text(encoding="utf-8").lstrip().splitlines()[0]
        except (OSError, IndexError):
            continue
        if wanted in (jev_check.fold(first.lstrip("# ")), jev_check.fold(sheet.stem)):
            return sheet
    return None


def parse_features(sheet_text: str) -> "list[dict]":
    """Feature chunks from a sheet's `## Features & Traits` section.

    The section is not a list of features, it is how one person writes notes:
    a bullet may hold one feature with a bold name and three sentences, or
    four traits separated by `·`, and the origin feats sit outside the bullets
    as bold paragraphs. Splitting on any single one of those shapes loses the
    others, so each is handled where it appears.
    """
    lines = sheet_text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().lower().startswith("## features")), None)
    if start is None:
        return []
    body: "list[str]" = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        body.append(ln)

    chunks: "list[str]" = []
    for ln in body:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = ln[:1] in (" ", "\t")
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        text = bullet.group(1).strip() if bullet else stripped
        if indented and chunks:
            # Indented, bullet or not, it belongs to the feature above it: the
            # three uses listed under Monk's Focus, or a rule that wrapped.
            chunks[-1] += " " + text
        elif bullet:
            chunks.append(text)
        elif "**" in stripped:
            # The origin feats sit outside the bullet list as bold paragraphs.
            chunks.append(stripped)
        elif chunks:
            chunks[-1] += " " + stripped

    features: "list[dict]" = []
    for chunk in chunks:
        # A bold name means the whole chunk is one feature and its description.
        # Without one, `·` is this sheet's separator for a run of small traits.
        parts = [chunk] if "**" in chunk else [p.strip() for p in chunk.split("·") if p.strip()]
        for part in parts:
            name = _feature_name(part)
            if not name:
                continue
            features.append({"ad": name, "metin": part[:MAX_FEATURE_CHARS]})
    return features


def _feature_name(text: str) -> str:
    """The feature's name, as the sheet bolded it.

    A bold run ending in a colon is a label for what follows — the origin
    feats are written `**Origin Feat (Soldier):** **Savage Attacker** — …` —
    so the name is the first bold run that is not one of those.
    """
    bolds = re.findall(r"\*\*(.+?)\*\*", text)
    named = next((b for b in bolds if not b.strip().endswith(":")), bolds[0] if bolds else "")
    name = (named or text).strip().rstrip(".:")
    name = re.sub(r"\s*\(.*?\)\s*$", "", name).strip()
    return name[:60]


def roll_kind(label: str) -> str:
    """Which of the three roll kinds a request label describes.

    A skill name is decisive and covers every roll the router opens. Anything
    else falls back to the ability check, which is the kind the features in
    this window overwhelmingly attach to, and the DM can still override.
    """
    low = (label or "").lower()
    head = low.split("—")[0].split("-")[0].strip()
    if head in _SKILL_WORDS or any(s in low for s in _SKILL_WORDS):
        return "yetenek testi"
    if any(w in low for w in ("saving throw", "save", "kurtarma")):
        return "kurtarma atışı"
    if any(w in low for w in ("attack", "saldırı", "vuruş")):
        return "saldırı atışı"
    return "yetenek testi"


# ── cache ────────────────────────────────────────────────────────────────────


# Four players can miss the same round, so four windows resolve at once in
# four threads. They share this file.
_CACHE_LOCK = threading.Lock()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _merge_cache(new_entries: dict) -> None:
    """Fold new answers into the file without losing a sibling's.

    Read-modify-write from several threads drops whichever write finishes
    first, and a half-written file reads back as a parse error — which is
    survivable (the answer is simply asked again) but throws away work every
    time the table rolls together. The lock serialises this process's writers
    and the rename makes the file readable-or-old, never truncated.
    """
    if not new_entries:
        return
    with _CACHE_LOCK:
        merged = _load_cache()
        merged.update(new_entries)
        tmp = CACHE_FILE.with_suffix(".tmp")
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# Bumped whenever a question below is reworded. A cached answer is an answer to
# the question as it was asked, so an edit has to miss the cache rather than
# inherit the reading it was written to correct.
_PROMPT_VERSION = 2


def _legal_key(feature_text: str, kind: str, passed: bool, own: bool) -> str:
    return (f"legal:{_PROMPT_VERSION}:{_digest(feature_text)}:{kind}:"
            f"{'gecti' if passed else 'kaldi'}:{'kendi' if own else 'baskasi'}")


def _shape_key(feature_text: str) -> str:
    return f"shape:{_PROMPT_VERSION}:{_digest(feature_text)}"


# ── the judgment ─────────────────────────────────────────────────────────────

_EFFECTS = {
    "yeniden_at": "Zarı bütünüyle yeniden attırır; yeni sonuç geçerlidir.",
    "1d4_ekle": "Atılmış sonuca 1d4 eklenir.",
    "1d6_ekle": "Atılmış sonuca 1d6 eklenir.",
    "1d8_ekle": "Atılmış sonuca 1d8 eklenir.",
    "1d10_ekle": "Atılmış sonuca 1d10 eklenir.",
    "1d12_ekle": "Atılmış sonuca 1d12 eklenir.",
    "sabit_ekle": "Sonuca zar atmadan sabit bir sayı eklenir.",
    "avantaj": "Atış avantajla tekrarlanır.",
    "diger": "Bunların hiçbiri: etkisi başka türlü, DM elde çözer.",
}

_RESOURCES = {
    "heroic_inspiration": "Elde tutulan Heroic Inspiration harcanır.",
    "sinirli_kullanim": "Sınıfın ya da türün sınırlı kullanım sayısından biri harcanır (Second Wind, Focus Point, Bardic Inspiration gibi).",
    "buyu_slotu": "Bir büyü slotu harcanır.",
    "bedava": "Hiçbir kaynak harcanmaz, her zaman kullanılabilir.",
}


def _questions(pending_legal: "list[tuple[str, dict]]",
               pending_shape: "list[tuple[str, dict]]", kind: str = "") -> dict:
    """One Noul per feature whose legality is unknown, plus shape where needed.

    Every question here reads the same state and none depends on another's
    answer, so they go in one request and are answered in parallel.
    """
    q: dict = {}
    for qid, f in pending_legal:
        q[qid] = {
            "type": "noul",
            "instructions": (
                f"`ozellikler.{f['key']}.metin` bu özelliğin sheet'teki tam metni. "
                f"Sahibi: {f['sahip']}. Zarı atan: {f['atan']}. "
                f"Atılan zar bir {kind or 'd20 atışı'}; atıldı, sonucu görüldü. "
                f"Bu özellik şu anda, bu {kind or 'atışı'}ı değiştirmek için harcanabilir mi? "
                "Özelliğin hangi atış türüne yazıldığına dikkat et: metin yalnızca tek bir "
                "türü adlandırıyorsa, başka bir türde kullanılamaz."
            ),
            "criteria": {
                "true": (
                    f"Özellik metni ya açıkça {kind or 'bu atış türünü'} kapsıyor, ya da hiçbir "
                    "tür ayrımı yapmadan her d20 atışına uygulandığını söylüyor. Ayrıca sonucu "
                    "görüldükten SONRA devreye girebiliyor ve sahibi bu atışa müdahale edebiliyor. "
                    "Sahibi atanla aynı kişi değilse, özelliğin başkasının atışına etki ettiği "
                    "metinde açıkça yazıyor olmalı."
                ),
                "false": (
                    f"Metin başka bir atış türünü adlandırıyor ve {kind or 'bu türü'} kapsamıyor — "
                    "örneğin yalnızca 'ability check' diyen bir özellik kurtarma atışında ya da "
                    "saldırıda kullanılamaz. Ya da: zar atılmadan önce hazırlanması gereken bir şey "
                    "(önce bonus action ile verilen, önceden okunan bir büyü, önceden alınan "
                    "avantaj), hasara/iyileştirmeye ait, sürekli bir pasif bonus, tamamen ilgisiz, "
                    "ya da sahibinin bu atışa karışmasına imkân yok."
                ),
            },
        }
    for qid, f in pending_shape:
        base = qid[:-len("_etki")] if qid.endswith("_etki") else qid
        q[f"{base}_etki"] = {
            "type": "choice",
            "instructions": (
                f"`ozellikler.{f['key']}.metin` özelliği, atılmış bir d20 sonucunu "
                "mekanik olarak nasıl değiştirir? Metinde ne yazıyorsa onu seç."
            ),
            "criteria": dict(_EFFECTS),
        }
        q[f"{base}_kaynak"] = {
            "type": "choice",
            "instructions": (
                f"`ozellikler.{f['key']}.metin` özelliğini kullanmak neyi harcar?"
            ),
            "criteria": dict(_RESOURCES),
        }
    return q


def _resolve(features: "list[dict]", kind: str, passed: bool) -> "list[dict]":
    """Fill each feature's `legal`, `etki` and `kaynak`, from cache or model."""
    cache = _load_cache()
    fresh: dict = {}
    pending_legal, pending_shape = [], []
    for i, f in enumerate(features):
        f["key"] = f"f{i}"
        lk = _legal_key(f["metin"], kind, passed, f["sahip"] == f["atan"])
        sk = _shape_key(f["metin"])
        f["_lk"], f["_sk"] = lk, sk
        if lk in cache:
            f["legal"] = cache[lk]
        else:
            pending_legal.append((f"f{i}_uygun", f))
        if sk in cache:
            f.update(cache[sk])
        else:
            pending_shape.append((f"f{i}_etki", f))

    if not pending_legal and not pending_shape:
        return features

    state = {
        "atis": {
            "tur": kind,
            "sonuc": "başarılı" if passed else "başarısız",
            "not": ("Sonuç zaten görüldü. Soru, sonucu gördükten sonra hâlâ "
                    "harcanabilecek bir şey olup olmadığı."),
        },
        "ozellikler": {f["key"]: {"ad": f["ad"], "sahip": f["sahip"], "metin": f["metin"]}
                       for f in features},
    }
    # Short on purpose. The window is seconds long, so an answer that arrives
    # late is not a slow answer, it is the wrong answer — better to open with
    # nothing than to interrupt a DM who has already moved on.
    answers = jev_check._ask(state, _questions(pending_legal, pending_shape, kind), timeout=6)

    for qid, f in pending_legal:
        a = answers.get(qid) or {}
        if "noul" not in a:
            continue                       # no answer: leave the feature out
        f["legal"] = float(a["noul"]) >= OFFER_MIN
        fresh[f["_lk"]] = f["legal"]
    for qid, f in pending_shape:
        base = qid[:-len("_etki")]
        etki = ((answers.get(f"{base}_etki") or {}).get("choice") or "")
        kaynak = ((answers.get(f"{base}_kaynak") or {}).get("choice") or "")
        if not etki:
            continue
        shape = {"etki": etki, "kaynak": kaynak or "bedava"}
        f.update(shape)
        fresh[f["_sk"]] = shape

    _merge_cache(fresh)
    return features


# ── follow-up rolls ──────────────────────────────────────────────────────────


def follow_up(etki: str, spec: str, modifier: int, advantage: str) -> "dict | None":
    """The roll an offer implies, or None when the DM resolves it by hand.

    Which die, and whether the modifier rides along, are rules arithmetic —
    the model said what the feature does, and the numbers are decided here.
    """
    if etki == "yeniden_at":
        return {"spec": spec, "modifier": modifier, "advantage": advantage, "kind": "yeniden"}
    if etki == "avantaj":
        return {"spec": spec, "modifier": modifier, "advantage": "advantage", "kind": "yeniden"}
    m = re.fullmatch(r"(\d*d\d+)_ekle", etki)
    if m:
        # The bonus die is added to a total that already carries the modifier,
        # so it rolls bare.
        return {"spec": m.group(1), "modifier": 0, "advantage": "normal", "kind": "ek"}
    return None


# ── offers ───────────────────────────────────────────────────────────────────


def offers(campaign: str, roller: str, present: "list[str]", kind: str, passed: bool,
           inspiration: "dict | None" = None) -> "list[dict]":
    """Everything the table may legally spend on this roll, as offers.

    `inspiration` maps character name → whether they are holding Heroic
    Inspiration right now. That is a counter the display already keeps, so it
    is read rather than inferred, and it is the one offer that does not come
    off a sheet: Resourceful grants the inspiration, the inspiration is what
    gets spent, and asking a model to walk that chain would be asking it to
    restate a rule that never changes.
    """
    inspiration = inspiration or {}
    out: "list[dict]" = []

    for name in list(present)[:MAX_CHARACTERS]:
        if inspiration.get(name) and name == roller:
            out.append({
                "id": f"hi:{name}",
                "character": name,
                "feature": "Heroic Inspiration",
                "detail": "Zarı yeniden at — yeni sonuç geçerli.",
                "etki": "yeniden_at",
                "kaynak": "heroic_inspiration",
            })

    features: "list[dict]" = []
    for name in list(present)[:MAX_CHARACTERS]:
        sheet = _sheet_path(campaign, name)
        if sheet is None:
            continue
        try:
            text = sheet.read_text(encoding="utf-8")
        except OSError:
            continue
        for f in parse_features(text)[:MAX_FEATURES_PER_SHEET]:
            features.append({**f, "sahip": name, "atan": roller})

    if features:
        try:
            features = _resolve(features, kind, passed)
        except Exception:
            features = []

    for f in features:
        if not f.get("legal") or f.get("etki") in (None, "", "diger"):
            continue
        out.append({
            "id": f"sheet:{f['sahip']}:{_digest(f['metin'])}",
            "character": f["sahip"],
            "feature": f["ad"],
            "detail": _EFFECTS.get(f.get("etki", ""), ""),
            "etki": f.get("etki", ""),
            "kaynak": f.get("kaynak", "bedava"),
        })

    # One feature can only be offered once, and the hand-written Heroic
    # Inspiration offer wins over a sheet bullet that describes the same thing.
    seen, unique = set(), []
    for o in out:
        key = (o["character"], o["feature"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(o)
    return unique


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="What can still be spent on a roll that has landed.")
    ap.add_argument("--campaign", default="temiz-kagit")
    ap.add_argument("--roller", required=True)
    ap.add_argument("--present", default="", help="comma-separated characters at the table")
    ap.add_argument("--kind", default="yetenek testi", choices=list(_ROLL_KINDS))
    ap.add_argument("--label", default="", help="the request label; used to infer --kind when unset")
    ap.add_argument("--failed", action="store_true", help="the roll came in under the DC")
    ap.add_argument("--inspiration", default="",
                    help="comma-separated characters currently holding Heroic Inspiration")
    args = ap.parse_args()

    present = [p.strip() for p in args.present.split(",") if p.strip()] or [args.roller]
    kind = roll_kind(args.label) if args.label else args.kind
    held = {n.strip(): True for n in args.inspiration.split(",") if n.strip()}
    print(json.dumps(
        offers(args.campaign, args.roller, present, kind, not args.failed, held),
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
