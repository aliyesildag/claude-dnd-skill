#!/usr/bin/env python3
"""Log-only lint of what the table was actually shown.

Every line a player sees passes through the display's /chunk, typed and
addressed. That makes the display the one honest place to check the DM's
narration against the rules in SKILL.md — not the chat transcript, which the
players never see, and not a regex over a heredoc pulled back out of a shell
command. What arrives here is what reached the seats, nothing else.

The display also holds the two facts the hardest rules turn on and a transcript
does not: which dice requests are still open and what DC each carries (so a
leak is checked against the real number, not any number), and which lines went
to one character privately (the only per-character record of what a PC has
been shown — a public line that treats a whisper as common knowledge is a leak
the server can prove).

Two tiers of detector. The mechanical ones are patterns and run inline. The
ones that are judgments — did this sentence imply the outcome of a die not yet
rolled, is this prose spoken or written, does this public line lean on a
private one — go to Jev, one Noul each, off the request thread, the same way
the response window asks its legality questions.

Log-only, always: findings append to `<campaign>/.lint-log.jsonl` and nothing
blocks, nothing narrates. Precision is measured between sessions before any
finding is allowed to interrupt a turn. Opt out per campaign with
`turn_lint: off` in `state.md → ## Session Flags`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import jev_check as _jev
except Exception:                       # no key, no module — patterns still run
    _jev = None                         # type: ignore

LOG_NAME = ".lint-log.jsonl"
EXCERPT = 160

# A request the DM issued and nobody has answered yet is "open" for this long
# at most; after that it is stale, not a rule the narration can still break.
OPEN_REQUEST_TTL = 180.0
# Words of narration tolerated after a roll request — room for "…ya da başka
# bir şey?" without calling it resolution.
ROLL_TAIL_WORDS = 40
# A hot scene's narration should be short. Above this, while a fight is on,
# the length rule has been broken whatever the prose is like.
HOT_WORD_CAP = 130
# How many recent private lines a public one is checked against.
PRIVATE_WINDOW = 12

# Per-question bars, from a calibration pass over hand-written lines. The leak
# question separates cleanly (0.07 clean vs 0.48–0.63 leaking) but sits lower
# than the others; log-only means a lower bar costs a log line, not a turn.
JEV_MIN = {"sonuc_imasi": 0.60, "bilgi_sizintisi": 0.45, "roman_registeri": 0.55}
# Turkish is compact; the register of a line shows well before 25 words.
REGISTER_MIN_WORDS = 12

_DC = re.compile(r"\bDC\s*:?\s*(\d{1,2})\b|\bzorluk(?:\s*(?:derecesi|sınıfı))?\s*:?\s*(\d{1,2})\b", re.I)
# "yapıyorsun" / "yapacaksın" / "yapıyorsunuz" — the person suffix is -sun
# after -ıyor and -sın after -acak, so both vowels are allowed.
_ROTE = re.compile(
    r"(?:ne\s+yap(?:ıyor|acak)s[ıu]n(?:uz|ız)?|what\s+(?:do|will)\s+you\s+do)\s*\??\s*$", re.I)
# "1d20", "d20", "2d20" — a digit before the d is not a word boundary.
_D20_LINE = re.compile(r"(?<![A-Za-z])\d*d20\b", re.I)


def _now() -> float:
    return time.time()


def _words(text: str) -> int:
    return len(text.split())


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[:EXCERPT - 1] + "…"


def _kind(entry: dict) -> str:
    for k in ("player", "npc", "dice", "tutor", "action"):
        if entry.get(k):
            return k
    return "narration"


class Linter:
    """One per display process. Fed by /chunk and the dice paths, writes a log."""

    def __init__(self, campaign_dir_for, party_names, turn_active, ask=None) -> None:
        # Callables rather than values: the campaign can change under a running
        # display, and the party and the turn order change every fight.
        self._campaign_dir_for = campaign_dir_for   # (campaign) -> Path
        self._party_display = party_names           # () -> set[str], as the sheet spells them
        self._party_names = lambda: {_fold(n) for n in party_names()}   # for matching
        self._turn_active = turn_active             # () -> bool
        self._ask = ask or (getattr(_jev, "_ask", None))
        self._lock = threading.Lock()
        self._open: dict = {}                       # request_id → {ts, character, dc, label}
        self._private: list = []                    # recent (ts, to, text)
        self._flags_cache: dict = {}                # campaign → (mtime, flags)

    # ── what the server tells us ──────────────────────────────────────────

    def note_request(self, request_id: str, characters, dc, label: str = "") -> None:
        with self._lock:
            self._open[request_id] = {"ts": _now(), "characters": list(characters or []),
                                      "dc": dc if isinstance(dc, int) else None,
                                      "label": label or ""}

    def note_resolved(self, request_id: str) -> None:
        with self._lock:
            self._open.pop(request_id, None)

    def _open_requests(self) -> list:
        cutoff = _now() - OPEN_REQUEST_TTL
        with self._lock:
            stale = [k for k, v in self._open.items() if v["ts"] < cutoff]
            for k in stale:
                self._open.pop(k, None)
            return list(self._open.values())

    def _remember_private(self, to: str, text: str) -> None:
        with self._lock:
            self._private.append((_now(), to, text))
            del self._private[:-PRIVATE_WINDOW]

    def _recent_private(self) -> list:
        with self._lock:
            return list(self._private)

    # ── flags ─────────────────────────────────────────────────────────────

    def flags(self, campaign: str) -> dict:
        """`state.md → ## Session Flags`, as key: value, cached on mtime."""
        try:
            path = Path(self._campaign_dir_for(campaign)) / "state.md"
            mtime = path.stat().st_mtime
        except Exception:
            return {}
        cached = self._flags_cache.get(campaign)
        if cached and cached[0] == mtime:
            return cached[1]
        flags: dict = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^## Session Flags\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
            for line in (m.group(1) if m else "").splitlines():
                mm = re.match(r"^\s*-?\s*`?([A-Za-z_]+)`?\s*:\s*`?([^`\n]+?)`?\s*$", line)
                if mm:
                    flags[mm.group(1).lower()] = mm.group(2).strip().lower()
        except Exception:
            pass
        self._flags_cache[campaign] = (mtime, flags)
        return flags

    def enabled(self, campaign: str) -> bool:
        return self.flags(campaign).get("turn_lint", "on") not in ("off", "false", "disabled", "0")

    # ── the mechanical tier ───────────────────────────────────────────────

    def pattern_findings(self, entry: dict, campaign: str) -> list:
        text = str(entry.get("text") or "")
        kind = _kind(entry)
        to = str(entry.get("to") or "")
        out = []
        if kind in ("tutor", "player", "action"):
            return out          # not the DM's narration

        if kind in ("narration", "npc", "dice"):
            for m in _DC.finditer(text):
                num = m.group(1) or m.group(2)
                open_dcs = {r["dc"] for r in self._open_requests() if r["dc"] is not None}
                out.append({"rule": "dc_leak", "confidence": 1.0,
                            "detail": f"DC {num}" + (" — açık isteğin DC'si" if int(num) in open_dcs else ""),
                            "excerpt": _excerpt(text)})
                break

        if kind == "narration":
            if _ROTE.search(text.strip()):
                out.append({"rule": "rote_closer", "confidence": 1.0,
                            "detail": "tur 'ne yapıyorsun?' ile kapanıyor",
                            "excerpt": _excerpt(text[-EXCERPT:])})
            open_reqs = [r for r in self._open_requests() if r["characters"]]
            if open_reqs and not to and _words(text) > ROLL_TAIL_WORDS:
                who = ", ".join(c for r in open_reqs for c in r["characters"])
                out.append({"rule": "roll_not_final", "confidence": 0.8,
                            "detail": f"{_words(text)} kelime anlatım, açık zar isteği: {who}",
                            "excerpt": _excerpt(text)})
            if self._turn_active() and _words(text) > HOT_WORD_CAP:
                out.append({"rule": "length_heat", "confidence": 0.9,
                            "detail": f"savaşta {_words(text)} kelime (> {HOT_WORD_CAP})",
                            "excerpt": _excerpt(text)})

        if kind == "dice" and self.flags(campaign).get("roll_mode", "players") == "players":
            party = self._party_names()
            folded = _fold(text)
            named = [p for p in party if p and p in folded]
            if named and _D20_LINE.search(text) and "rolls" in text.lower():
                out.append({"rule": "pc_auto_roll", "confidence": 0.6,
                            "detail": f"roll_mode=players iken PC adına d20 satırı: {', '.join(named)}",
                            "excerpt": _excerpt(text)})
        return out

    # ── the judgment tier ─────────────────────────────────────────────────

    def judgment_questions(self, entry: dict) -> "tuple[dict, dict]":
        """(state, questions) for Jev, or ({}, {}) when nothing needs asking."""
        text = str(entry.get("text") or "")
        kind = _kind(entry)
        to = str(entry.get("to") or "")
        if kind not in ("narration", "npc") or _words(text) < 6:
            return {}, {}
        state: dict = {"anlatim": text, "tur": "NPC repliği" if kind == "npc" else "DM anlatımı"}
        q: dict = {}

        open_reqs = [r for r in self._open_requests() if r["characters"]]
        if open_reqs and not to:
            state["acik_atis"] = [{"karakter": ", ".join(r["characters"]), "etiket": r["label"]}
                                  for r in open_reqs]
            q["sonuc_imasi"] = {
                "type": "noul",
                "instructions": (
                    "`acik_atis` listesindeki zar(lar) istendi ama henüz atılmadı ya da "
                    "sonucu bilinmiyor. `anlatim` bu zarın SONUCUNU, başarı ihtimalini ya "
                    "da ne kadar zor olduğunu söylüyor ya da ima ediyor mu?"),
                "criteria": {
                    "true": ("Anlatım, zar düşmeden sonucu belli ediyor: 'beceriyle "
                             "kazanılmayacak', 'kolay olmayacak', 'kilit direniyor', 'başarır"
                             "sın' gibi — ya da sahneyi zarın ötesine ilerletiyor."),
                    "false": ("Anlatım yalnızca DENEMEYİ anlatıyor: karakterin fiziksel olarak "
                              "ne yaptığı, uzanışı, gerilimi — sonuç, ihtimal, zorluk hakkında "
                              "hiçbir şey söylemiyor."),
                },
            }

        private = [(t, txt) for (_, t, txt) in self._recent_private() if t]
        if private and not to:
            state["gizli"] = [{"kime": t, "metin": txt} for t, txt in private]
            state["oyuncu_karakterleri"] = sorted(self._party_display())
            q["bilgi_sizintisi"] = {
                "type": "noul",
                "instructions": (
                    "`gizli` listesindeki satırlar yalnızca `kime` alanındaki karaktere "
                    "gösterildi; masanın geri kalanı görmedi. `anlatim` HERKESE gidiyor. "
                    "`oyuncu_karakterleri` oyuncuların karakterleri — DM onların ağzından "
                    "konuşmaz. Anlatım, gizli satırlardan birindeki bir bilgiyi — bir isim, "
                    "bir yer, bir ipucu, bir niyet — herkesin bildiği bir şeymiş gibi "
                    "adlandırıyor, üstüne kuruyor ya da bir OYUNCU karakterine söyletiyor mu?"),
                "criteria": {
                    "true": ("Anlatım gizli satırdaki özel bilgiyi açıkça kullanıyor: masanın onu "
                             "bildiğini varsayıyor, ya da sırrı tutan karakter dışında bir oyuncu "
                             "karakterinin ağzından söyletiyor (bu, DM'in PC adına konuşması ve "
                             "sızıntı — ikisi birden)."),
                    "false": ("Anlatım gizli satırlardan bağımsız; ya da o bilgiyi adlandırmadan "
                              "yalnızca tarif ediyor; ya da bilgiyi bir NPC (oyuncu karakteri "
                              "OLMAYAN biri) yüksek sesle söylüyor ve böylece herkese yeni veriyor."),
                },
            }
            # Who voices it is a separate, easier question, and the answer
            # changes the verdict: an NPC saying a secret out loud is how
            # secrets legitimately become public; a PC being made to say it,
            # or the narrator assuming it, is not. Asked as a choice so the
            # split is made in code, not inside one blurred confidence.
            # No "not present" option on purpose: the noul above already says
            # whether the secret is used, and a "yok" here swallowed the answer
            # even when the secret was quoted verbatim. This only asks "by whom".
            q["sizinti_kaynagi"] = {
                "type": "choice",
                "instructions": (
                    "`gizli` satırlardaki bilgiye `anlatim` içinde en yakın düşen ifadeyi "
                    "kim dile getiriyor ya da kim biliyor sayılıyor?"),
                "criteria": {
                    "npc": "Oyuncu karakteri olmayan biri (tüccar, muhtar, düşman) yüksek sesle söylüyor.",
                    "oyuncu_karakteri": "`oyuncu_karakterleri` listesindeki biri söylüyor ya da düşünüyor.",
                    "anlatici": "Kimse söylemiyor; anlatıcı sesi bilgiyi zaten biliniyor gibi kullanıyor.",
                },
            }

        if kind == "narration" and _words(text) >= REGISTER_MIN_WORDS:
            q["roman_registeri"] = {
                "type": "noul",
                "instructions": (
                    "`anlatim` masada bir arkadaşa yüksek sesle söylenir gibi mi yazılmış, "
                    "yoksa bir roman sayfası gibi mi? Dile bakma, biçime bak."),
                "criteria": {
                    "true": ("Roman sayfası: fiilsiz parça cümleler üst üste ('Boş eyer. "
                             "Sürüklenen dizginler.'), iki nokta yığınları ('Dışarıda: çamurda "
                             "botlar.'), sözlük kelimeleri, '...kokuyor' ile açılan betimleme, "
                             "kimsenin ağzından çıkmayacak süslü söz dizimi."),
                    "false": ("Konuşma: tam cümleler, sıradan kelimeler, birinin karşısındakine "
                              "anlatacağı ritim — kısa da olabilir uzun da, ama SÖYLENİR."),
                },
            }
        return (state, q) if q else ({}, {})

    def judgment_findings(self, entry: dict) -> list:
        state, questions = self.judgment_questions(entry)
        if not questions or self._ask is None:
            return []
        answers = self._ask(state, questions, timeout=8) or {}
        # The leak verdict is two answers folded into one: the secret is used,
        # and not by an NPC. "sizinti_kaynagi" is never a finding on its own.
        who = answers.pop("sizinti_kaynagi", None) or {}
        probs = who.get("probabilities") or {}
        source = max(probs, key=probs.get) if probs else (who.get("choice") or "")
        if source == "npc" and "bilgi_sizintisi" in answers:
            answers.pop("bilgi_sizintisi")
        labels = {
            "sonuc_imasi": "zar düşmeden sonuç ima edildi",
            "bilgi_sizintisi": "özel satırdaki bilgi herkese açık kullanıldı",
            "roman_registeri": "anlatım konuşma değil sayfa gibi",
        }
        out = []
        for qid, a in answers.items():
            val = (a or {}).get("noul")
            if val is None:
                continue
            try:
                conf = float(val)
            except (TypeError, ValueError):
                continue
            if conf >= JEV_MIN.get(qid, 0.60):
                detail = labels.get(qid, qid)
                if qid == "bilgi_sizintisi" and source:
                    detail += f" ({source})"
                out.append({"rule": qid, "confidence": round(conf, 2), "detail": detail,
                            "excerpt": _excerpt(str(entry.get("text") or ""))})
        return out

    # ── entry point from /chunk ───────────────────────────────────────────

    def observe(self, entry: dict, campaign: str, sync: bool = False) -> "list | None":
        """Called for every /chunk. Patterns now; judgments on a thread.

        Returns the pattern findings (for tests and the /lint endpoint); the
        judgment findings land in the log when Jev answers. With `sync=True`
        everything runs inline and the full list comes back — for tests.
        """
        if not campaign or not self.enabled(campaign):
            return None
        to = str(entry.get("to") or "")
        if to:
            # A private line is knowledge, not a violation — file it and stop.
            self._remember_private(to, str(entry.get("text") or ""))
        findings = self.pattern_findings(entry, campaign)
        if findings:
            self._write(campaign, findings, entry)
        if sync:
            more = self.judgment_findings(entry)
            if more:
                self._write(campaign, more, entry)
            return findings + more
        if self._ask is not None:
            threading.Thread(target=self._judge_and_log, args=(entry, campaign),
                             daemon=True).start()
        return findings

    def _judge_and_log(self, entry: dict, campaign: str) -> None:
        try:
            more = self.judgment_findings(entry)
            if more:
                self._write(campaign, more, entry)
        except Exception:
            pass                        # a lint failure must never surface at the table

    def _write(self, campaign: str, findings: list, entry: dict) -> None:
        try:
            path = Path(self._campaign_dir_for(campaign)) / LOG_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            with open(path, "a", encoding="utf-8") as f:
                for v in findings:
                    f.write(json.dumps({"ts": ts, "campaign": campaign, "kind": _kind(entry),
                                        "to": entry.get("to") or "", **v},
                                       ensure_ascii=False) + "\n")
        except Exception:
            pass

    def tail(self, campaign: str, n: int = 20) -> list:
        try:
            path = Path(self._campaign_dir_for(campaign)) / LOG_NAME
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _fold(s: str) -> str:
    if _jev is not None and hasattr(_jev, "fold"):
        return _jev.fold(s)
    return s.lower()
