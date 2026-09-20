"""
app.py — DnD DM display server

Receives text chunks from wrapper.py, detects scene context from keywords,
and pushes both to the browser via Server-Sent Events.

Endpoints:
    GET  /                   → serves index.html
    POST /chunk              → receives text chunk from wrapper.py
    POST /stats              → receives character/combat stat updates (merged, persisted)
    GET  /stream             → SSE stream to browser (text + scene + stats events)
    GET  /ping               → health check
    POST /clear              → wipe text log and broadcast clear event
    POST /player-input         → legacy queue endpoint (check_input.py compat)
    POST /player-input/drain   → drain legacy queue (check_input.py compat)
    POST /player-input/stage   → stage an action for review before firing
    POST /player-input/ready   → mark a staged action as ready
    POST /player-input/unstage → remove a staged action
    POST /player-input/skip    → skip a character's turn (stages + readies a skip entry)
    GET  /srd-lookup           → look up a spell/item/feature/condition by name
"""

import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import unicodedata
import urllib.parse
from collections import deque
from typing import Optional
from flask import Flask, Response, request, render_template, jsonify, send_from_directory
from flask_cors import CORS

# This file lives at <code-root>/display/ — resolve dirs from its location so
# paths work in any install mode (plugin, standalone skill, or dev clone).
# Writable runtime state goes to rt() (the update-safe runtime dir), NOT here.
_HERE         = os.path.dirname(os.path.abspath(__file__))
_ROOT         = os.path.dirname(_HERE)
SCRIPTS_DIR   = os.path.join(_ROOT, "scripts")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from runtime_paths import rt          # resolves <data-root>/.runtime
LOG_FILE      = rt("text_log.json")

# SRD lookup module — degrades silently if dataset not built
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
try:
    import lookup as _lookup
    _SRD_AVAILABLE = True
except Exception:
    _lookup = None          # type: ignore
    _SRD_AVAILABLE = False

from paths import find_campaign as _find_campaign

import dialogue as _dialogue

# Turn lint — log-only checks of what the table was shown, against SKILL.md.
# Optional like everything else on this side: no module, no lint, no log.
try:
    import turn_lint as _turn_lint
except Exception:
    _turn_lint = None           # type: ignore

# Battle map — the tactical grid drawn from its spec. Optional: without grid.py
# on the path the display simply has no /battle-map and everything else runs.
try:
    import battle_map as _battle_map_mod
except Exception:
    _battle_map_mod = None      # type: ignore
from utf8io import read_text as _read_text

# Response window — what a player may still spend after a roll has landed.
# Optional: no module, no key, no network and the roll resolves the way it
# always did.
try:
    import jev_window as _jev_window
except Exception:
    _jev_window = None      # type: ignore

# Audio module — degrades silently if numpy not installed
_AUDIO_DIR = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
if _AUDIO_DIR not in _sys.path:
    _sys.path.insert(0, _AUDIO_DIR)
try:
    import audio as _audio
    _audio.init()
except Exception:
    _audio = None   # type: ignore

# TTS module — degrades silently if Gemini API key not configured
try:
    import tts as _tts
except Exception:
    _tts = None   # type: ignore


def _apply_campaign_sfx_languages() -> None:
    """Read sfx_languages from the active campaign's state.md Session Flags.

    state.md line shape:  `sfx_languages: en,zh,es`
    Takes precedence over the DND_SFX_LANGUAGES env var when present; both
    fall back to English-only if neither is set.
    """
    if _audio is None:
        return
    try:
        camp = open(rt(".campaign"), encoding="utf-8").read().strip()
        if not camp:
            return
        state_md = _find_campaign(camp) / "state.md"
        if not state_md.exists():
            return
        text = state_md.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return
    m = re.search(r"^\s*sfx_languages:\s*([\w,\s\-]+)$", text, re.MULTILINE)
    if not m:
        return
    langs = [l.strip() for l in m.group(1).split(",") if l.strip()]
    valid = [l for l in langs if l in _audio.available_languages()]
    if valid:
        _audio.set_sfx_languages(valid)


_apply_campaign_sfx_languages()

HELP_LOCK     = rt(".help-lock")
CAMP_FILE     = rt(".campaign")
STATS_FILE    = rt("stats.json")
MINIMAP_FILE  = rt("minimap.json")
BATTLE_MAP_FILE = rt("battle_map.json")
TOKEN_FILE    = rt(".token")
INPUT_FILE    = rt("player_input.json")
TRIGGER_FILE  = rt(".input_trigger")
QUEUE_FILE    = rt(".input_queue")
DEVICES_FILE         = rt(".approved_devices.json")
PENDING_DEVICES_FILE = rt(".pending_devices.json")

# ─── LAN / TLS mode ───────────────────────────────────────────────────────────
# Pass --lan to bind on 0.0.0.0 and protect write endpoints with a token.
# Pass --tls (requires --lan) to enable HTTPS with a self-signed cert.
# Without --lan the server binds to localhost only; no token is required.

_LAN_MODE: bool = "--lan" in sys.argv
_TLS_MODE: bool = "--tls" in sys.argv
if _LAN_MODE:
    sys.argv.remove("--lan")   # prevent Flask from seeing an unknown flag
if _TLS_MODE:
    sys.argv.remove("--tls")


def _get_or_create_token() -> str:
    """Load or generate the LAN token. Upgrades short legacy tokens to 64-char."""
    try:
        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if len(token) >= 48:   # 48+ chars = already long enough
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)   # 64-char hex — brute force infeasible
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


_lan_token: Optional[str] = _get_or_create_token() if _LAN_MODE else None


# ─── Rate limiting ────────────────────────────────────────────────────────────
# Simple in-process sliding window: max 20 write requests per IP per minute.
# Prevents spam injection and brute-force token guessing on write endpoints.

import time as _time

_rate_buckets: dict[str, list] = {}
_rate_lock = threading.Lock()
_RATE_WINDOW = 60    # seconds
_RATE_MAX    = 20    # requests per window per IP


def _client_ip() -> str:
    """The real client address, seeing through a local reverse proxy.

    Behind `cloudflared tunnel` every request reaches Flask from the connector
    on 127.0.0.1, so remote_addr collapses all players onto one address. That
    breaks two things: the rate bucket becomes shared (four phones exhaust a
    20-per-minute budget between them and 429 each other), and _device_ok's
    localhost auto-approve fires for anyone with the URL.

    CF-Connecting-IP is set by Cloudflare and cannot be spoofed past it — but a
    direct LAN client could forge the header, so it is only honored when the
    connection itself came from loopback, i.e. from a proxy running on this
    host. Anything else falls back to remote_addr.
    """
    peer = request.remote_addr or "?"
    if peer in ("127.0.0.1", "::1"):
        for header in ("CF-Connecting-IP", "X-Real-IP"):
            v = (request.headers.get(header) or "").strip()
            if v:
                return v[:64]
    return peer


def _rate_ok(ip: str) -> bool:
    now = _time.time()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(ip, []) if now - t < _RATE_WINDOW]
        if len(bucket) >= _RATE_MAX:
            return False
        bucket.append(now)
        _rate_buckets[ip] = bucket
    return True


# ─── Input validation helpers ─────────────────────────────────────────────────

_PRINTABLE    = re.compile(
    "[^"
    "\x20-\x7E"                 # ASCII printable
    " -ɏ"             # Latin-1 + Latin Extended A/B (é ñ ö ć ş ž etc.)
    "Ͱ-Ͽ"             # Greek
    "Ѐ-ӿ"             # Cyrillic (Russian, Ukrainian)
    "֐-׿"             # Hebrew
    "؀-ۿ"             # Arabic
    "ݐ-ݿ"             # Arabic Supplement
    "ऀ-ॿ"             # Devanagari (Hindi, Marathi)
    "ঀ-৿"             # Bengali
    "஀-௿"             # Tamil
    "ఀ-౿"             # Telugu
    "฀-๿"             # Thai
    "Ḁ-ỿ"             # Latin Extended Additional (Vietnamese diacritics)
    "　-〿"             # CJK Symbols and Punctuation
    "぀-ゟ"             # Hiragana
    "゠-ヿ"             # Katakana
    "㐀-䶿"             # CJK Extension A
    "一-鿿"             # CJK Unified Ideographs
    "가-힯"             # Hangul Syllables
    "＀-￯"             # Halfwidth / Fullwidth
    "]"
)
_SHELL_CHARS  = re.compile(r'[$`\\;|&><()\[\]{}!]')
# Unicode \w covers letters from all scripts above. Allow space, apostrophe, hyphen
# inside the name; trim 1-2 char names to a separate branch.
_CHAR_NAME_RE = re.compile(r"^\w[\w '\-]{0,48}\w$|^\w{1,2}$", re.UNICODE)


def _sanitize_input(text: str) -> str:
    """Strip control chars and shell metacharacters from player input text."""
    text = _SHELL_CHARS.sub("", text)
    text = _PRINTABLE.sub("", text)
    return text[:500].strip()


def _char_ok(name: str, known: set) -> bool:
    """Return True if character name is syntactically valid and in the party."""
    if not _CHAR_NAME_RE.match(name):
        return False
    if known and name not in known and name != "Everybody":
        return False
    return True


# ─── Device approval system ───────────────────────────────────────────────────
# Each browser generates a UUID device ID (localStorage). On first input attempt
# from an unseen LAN device, the request is held and the DM sees an Approve/Deny
# card on the display. Localhost is auto-approved. Denied devices are blocked for
# the session.

_approved_devices: set[str]       = set()
_denied_devices:   set[str]       = set()
_pending_devices:  dict[str, dict] = {}  # device_id -> {ip, first_seen}
_devices_lock = threading.Lock()


def _persist_approved_devices() -> None:
    """Persist approved devices to disk. Must be called WITHOUT _devices_lock held."""
    try:
        with _devices_lock:
            data = list(_approved_devices)
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(DEVICES_FILE, 0o600)
    except Exception:
        pass


def _load_approved_devices() -> None:
    try:
        with open(DEVICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _devices_lock:
            for d in data:
                _approved_devices.add(str(d))
    except Exception:
        pass


def _persist_pending_devices() -> None:
    """Persist pending devices to disk so they survive app restarts. Must be called WITHOUT _devices_lock held."""
    try:
        with _devices_lock:
            data = list(_pending_devices.values())
        with open(PENDING_DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(PENDING_DEVICES_FILE, 0o600)
    except Exception:
        pass


def _load_pending_devices() -> None:
    try:
        with open(PENDING_DEVICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _devices_lock:
            for d in data:
                if isinstance(d, dict) and d.get("id"):
                    # Skip if already approved/denied during this run
                    if d["id"] not in _approved_devices and d["id"] not in _denied_devices:
                        _pending_devices[d["id"]] = d
    except Exception:
        pass


_load_approved_devices()
_load_pending_devices()


# A casual home-LAN game doesn't need a per-device approval gate — it's friction
# (every phone sits on "Awaiting approval" until the DM taps a card). Default:
# trust any device that can already reach the server. Set DND_REQUIRE_APPROVAL=1
# to restore the approve/deny gate (e.g. on an untrusted/shared network).
_REQUIRE_APPROVAL = os.environ.get("DND_REQUIRE_APPROVAL", "").strip().lower() in ("1", "true", "yes", "on")


def _device_ok(device_id: str, ip: str) -> str:
    """Return 'approved', 'pending', or 'denied' for a given device."""
    if not device_id:
        return "denied"
    _need_persist_approved = False
    _need_persist_pending  = False
    with _devices_lock:
        if device_id in _approved_devices:
            return "approved"
        if device_id in _denied_devices:
            return "denied"
        # Auto-approve localhost always, and every reachable device unless the
        # approval gate is explicitly required.
        if not _REQUIRE_APPROVAL or ip in ("127.0.0.1", "::1"):
            _approved_devices.add(device_id)
            _need_persist_approved = True
        # New LAN device with the gate on — hold and notify DM
        elif device_id not in _pending_devices:
            _pending_devices[device_id] = {
                "id":         device_id,
                "ip":         ip,
                "first_seen": _time.time(),
            }
            _need_persist_pending = True
            _broadcast({"device_request": {"id": device_id, "ip": ip}})
    # Persist outside the lock to avoid deadlock (Lock is not reentrant)
    if _need_persist_approved:
        _persist_approved_devices()
        return "approved"
    if _need_persist_pending:
        _persist_pending_devices()
    return "pending"


# ─── Staged input system ──────────────────────────────────────────────────────
# Players stage their actions from the display companion UI. When all expected
# players mark ready, the combined action is written to TRIGGER_FILE for
# wrapper.py to inject into Claude's PTY stdin.

_staged: dict[str, dict] = {}   # {char_name: {text, ready, timestamp}}
_staged_lock = threading.Lock()
_expected_count = 1             # updated when stats arrive; min 1
_autorun_threshold: Optional[int] = None  # overrides _expected_count when set via push_stats --autorun-threshold

# Tracks which character names are currently sitting in .input_queue waiting
# for the DM to press Enter. Set when queue is written, cleared when wrapper
# POSTs /queue/consumed after injection. Persists through page reloads via SSE
# initial data and is broadcast to all connected clients on change.
_queue_status: list = []
_queue_status_lock = threading.Lock()

# Last autorun cycle broadcast — replayed on SSE reconnect so late-joining
# clients start the countdown from the correct elapsed position.
# Cleared when autorun_waiting=false (turn resolved or autorun disabled).
_autorun_cycle: Optional[dict] = None
_autorun_cycle_lock = threading.Lock()


def _normalize_slot(slot: dict) -> None:
    """Coerce a spell-slot entry to the canonical {used, max} shape in place.

    Tolerates legacy/alt payloads that use `remaining` instead of `used`.
    Without this, _slot_use/_slot_restore raise KeyError on a slot stored
    under the alt schema (e.g. after a long-rest --spell-slots full-replace).
    """
    if "used" in slot:
        return
    mx = slot.get("max", 0)
    if "remaining" in slot:
        slot["used"] = max(mx - int(slot.get("remaining", 0)), 0)
    else:
        slot["used"] = 0


def _staged_snapshot() -> dict:
    """Return a serialisable copy of the staged dict (no IP field)."""
    return {k: {"text": v["text"], "ready": v["ready"]} for k, v in _staged.items()}


def _check_auto_trigger() -> None:
    """Move staged-and-ready actions into the DM-gated queue file (.input_queue).

    .input_queue is NOT injected immediately — wrapper.py picks it up the next
    time the DM presses Enter (or Claude explicitly triggers via .input_trigger).
    This gives the DM control over when player actions enter Claude's context.
    """
    with _staged_lock:
        if not _staged:
            return
        everybody_ready = "Everybody" in _staged and _staged["Everybody"]["ready"]
        all_ready       = all(v["ready"] for v in _staged.values())
        threshold       = _autorun_threshold if _autorun_threshold is not None else _expected_count
        enough          = len(_staged) >= threshold or everybody_ready
        if not (all_ready and enough):
            return
        char_names = list(_staged.keys())
        lines      = [f'[{c}]: {e["text"]}' for c, e in _staged.items()]
        content    = "\n".join(lines)
        _staged.clear()

    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        char_names = []

    if char_names:
        with _queue_status_lock:
            _queue_status.clear()
            _queue_status.extend(char_names)
    _broadcast({"staged_inputs": {}, "queue_status": list(char_names)})


def _token_ok() -> bool:
    """Return True if the request carries the correct LAN token (or we're in localhost mode)."""
    if _lan_token is None:
        return True   # localhost mode — no token required
    provided = request.headers.get("X-DND-Token", "")
    return hmac.compare_digest(provided, _lan_token)


app = Flask(__name__)

app.config['TEMPLATES_AUTO_RELOAD'] = True
CORS(app)

# Wire audio broadcast after _broadcast is defined (see bottom of file)
# — done lazily via set_broadcast() called after app is created.

# ─── Scene definitions ────────────────────────────────────────────────────────
# Each scene: keywords (weighted — more = higher priority hit),
# gradient colors [top, bottom], accent color, particle type, display label.

SCENES: dict[str, dict] = {
    "tavern": {
        "keywords": [
            "tavern", "inn", "guttered", "common room", "hearth",
            "fireplace", "ale", "mead", "barkeep", "innkeeper",
            "candle", "tallow", "flagon", "stool", "bar",
        ],
        "colors": ["#1a0800", "#2e1400"],
        "accent": "#c8601a",
        "particles": "embers",
        "label": "The Inn",
    },
    "dungeon": {
        "keywords": [
            "dungeon", "corridor", "stone floor", "torch", "iron gate",
            "portcullis", "cell", "shackle", "pit", "dank",
        ],
        "colors": ["#080818", "#12082e"],
        "accent": "#6a3aaa",
        "particles": "dust",
        "label": "The Dungeon",
    },
    "mine": {
        "keywords": [
            "mine", "seam", "shaft", "tunnel", "ore", "pickaxe",
            "foreman", "deep seam", "ashstone", "cart", "vein",
        ],
        "colors": ["#0a0a0a", "#1a1008"],
        "accent": "#806040",
        "particles": "dust",
        "label": "The Mine",
    },
    "cave": {
        "keywords": [
            "cave", "cavern", "stalactite", "stalagmite", "underground",
            "grotto", "dripping", "echo", "subterranean",
        ],
        "colors": ["#0a1520", "#0a1030"],
        "accent": "#2060a0",
        "particles": "mist",
        "label": "The Cavern",
    },
    "forest": {
        "keywords": [
            "forest", "wood", "tree", "branch", "leaves", "undergrowth",
            "hollow wood", "canopy", "root", "bark", "moss", "fern",
            "thicket", "grove",
        ],
        "colors": ["#041008", "#081a04"],
        "accent": "#40a040",
        "particles": "leaves",
        "label": "The Forest",
    },
    "castle": {
        "keywords": [
            "castle", "rampart", "battlement", "keep", "parapet",
            "drawbridge", "moat", "throne", "great hall", "manor",
        ],
        "colors": ["#0e0e1a", "#1a1a2e"],
        "accent": "#8080c0",
        "particles": "dust",
        "label": "The Castle",
    },
    "mountain": {
        "keywords": [
            "mountain", "snow", "peak", "blizzard", "frost", "glacier",
            "avalanche", "ridge", "cliff", "altitude", "wind",
        ],
        "colors": ["#0a1020", "#1a2040"],
        "accent": "#a0c0e0",
        "particles": "snow",
        "label": "The Mountains",
    },
    "ocean": {
        "keywords": [
            "ocean", "sea", "ship", "wave", "sailor", "port", "harbour",
            "dock", "tide", "storm", "mast", "hull", "water",
        ],
        "colors": ["#000d1a", "#001a33"],
        "accent": "#0060a0",
        "particles": "ripples",
        "label": "The Sea",
    },
    "desert": {
        "keywords": [
            "desert", "sand", "dune", "oasis", "scorching", "arid",
            "mirage", "camel", "sphinx",
        ],
        "colors": ["#1a0f00", "#2e1a00"],
        "accent": "#c08030",
        "particles": "sand",
        "label": "The Desert",
    },
    "ruins": {
        "keywords": [
            "ruins", "ruin", "crumble", "crumbling", "rubble", "ancient",
            "overgrown", "collapsed", "forgotten", "desolate", "remnant",
        ],
        "colors": ["#100e04", "#1e1a08"],
        "accent": "#806830",
        "particles": "dust",
        "label": "The Ruins",
    },
    "swamp": {
        "keywords": [
            "swamp", "marsh", "bog", "mud", "murky", "fetid", "reed",
            "mire", "sludge", "stagnant",
        ],
        "colors": ["#080e04", "#0e1808"],
        "accent": "#406020",
        "particles": "mist",
        "label": "The Swamp",
    },
    "crypt": {
        "keywords": [
            "crypt", "tomb", "grave", "coffin", "undead", "bones",
            "skeleton", "lich", "mausoleum", "burial", "sarcophagus",
            "dead", "death",
        ],
        "colors": ["#08000a", "#140014"],
        "accent": "#602060",
        "particles": "smoke",
        "label": "The Crypt",
    },
    "fire": {
        "keywords": [
            "fire", "flame", "burn", "blaze", "inferno", "conflagration",
            "ember", "char", "smoke", "ash cloud",
        ],
        "colors": ["#1a0500", "#2e0800"],
        "accent": "#ff4400",
        "particles": "embers",
        "label": "The Fire",
    },
    "arcane": {
        "keywords": [
            "arcane", "magic", "spell", "enchant", "rune", "glyph",
            "mystical", "ritual", "incantation", "ward", "sigil",
            "thaumaturgy", "sorcery",
        ],
        "colors": ["#080020", "#12003a"],
        "accent": "#8040ff",
        "particles": "sparks",
        "label": "The Arcane",
    },
    "city": {
        "keywords": [
            "city", "market", "street", "crowd", "village", "town",
            "square", "cobble", "district", "quarter", "merchant",
            "ashenveil",
        ],
        "colors": ["#0a0f1a", "#15202e"],
        "accent": "#6080a0",
        "particles": "rain",
        "label": "The Town",
    },
    "night": {
        "keywords": [
            "night", "midnight", "moon", "star", "dark sky",
            "constellation", "celestial", "dusk", "twilight",
        ],
        "colors": ["#000008", "#04000f"],
        "accent": "#4060a0",
        "particles": "stars",
        "label": "The Night",
    },
    "temple": {
        "keywords": [
            "temple", "shrine", "altar", "holy", "sacred", "chapel",
            "prayer", "cleric", "incense", "lantern", "pew", "nave",
            "pale flame",
        ],
        "colors": ["#0e0c18", "#1a1428"],
        "accent": "#c0a060",
        "particles": "smoke",
        "label": "The Temple",
    },
}

# Priority order — checked in sequence; first match wins per chunk
SCENE_PRIORITY = [
    "mine", "crypt", "arcane", "fire", "temple", "dungeon", "cave",
    "forest", "swamp", "castle", "ocean", "mountain", "desert", "ruins",
    "tavern", "city", "night",
]

# ─── ANSI / TUI chrome stripping ─────────────────────────────────────────────

class _ANSIState:
    """Character-by-character ANSI escape-sequence state machine.

    Regex approaches fail when the PTY delivers bytes one at a time, splitting
    sequences like \\x1b[4;2m across chunk boundaries.  This state machine
    carries its state across calls so cross-chunk splits are handled correctly.

    States
    ------
    normal   → emitting regular characters
    esc      → saw ESC (0x1B), waiting to see what kind of sequence follows
    csi      → inside CSI sequence (ESC [ … letter)
    osc      → inside OSC sequence (ESC ] … BEL or ST)
    osc_esc  → inside OSC, just saw ESC — might be the ST terminator (ESC \\)
    """

    __slots__ = ("_s",)

    def __init__(self) -> None:
        self._s: str = "normal"

    def feed(self, text: str) -> str:
        out: list[str] = []
        s = self._s
        for ch in text:
            c = ord(ch)
            if s == "normal":
                if c == 0x1B:
                    s = "esc"
                elif c >= 0x20 or c in (0x09, 0x0A):   # printable / tab / newline
                    out.append(ch)
                # else: other control char (bell, etc.) — discard
            elif s == "esc":
                if ch == "[":
                    s = "csi"
                elif ch == "]":
                    s = "osc"
                else:
                    s = "normal"    # 2-char ESC sequence; discard both bytes
            elif s == "csi":
                if 0x40 <= c <= 0x7E:   # final byte of CSI
                    s = "normal"
                elif c == 0x1B:         # unexpected ESC — start fresh
                    s = "esc"
                # else: parameter / intermediate byte, keep consuming
            elif s == "osc":
                if c == 0x07:           # BEL terminates OSC
                    s = "normal"
                elif c == 0x1B:
                    s = "osc_esc"
                # else: OSC payload, keep consuming
            elif s == "osc_esc":
                s = "normal" if ch == "\\" else "osc"
        self._s = s
        return "".join(out)


_ansi = _ANSIState()
_ansi_lock = threading.Lock()

_BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼━═║╔╗╚╝")
_BOX_CHAR_STRIP = "╭╮╰╯│─┌┐└┘├┤┬┴┼━═║╔╗╚╝"  # same set as string for str.strip()

# Characters used by Claude CLI spinner / prompt / UI
_SPINNER_CHARS = set("✽⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒◌◎●")
_PROMPT_STARTS = ("❯", ">", "·", "▸", "ℹ", "✓", "⚠", "✗", "⟳", "↳")


def _handle_cr(text: str) -> str:
    """Handle carriage returns the way a real terminal would.

    Two distinct cases:
      \\r\\n  — a real newline (\\r\\n line ending).  Normalise to \\n first
                so the content is preserved.
      bare \\r — cursor-to-column-0 for in-place token updates.  Claude CLI
                streams each token by rewriting the current line:
                  "The" → \\r"The Gut" → \\r"The Gutte" → …
                Keep only the last segment (= the final written state).
    """
    # Step 1: treat \\r\\n as a real newline — must come before bare-\\r logic
    text = text.replace("\r\n", "\n")

    # Step 2: handle remaining bare \\r (in-place rewrites)
    lines = text.split("\n")
    result = []
    for line in lines:
        if "\r" in line:
            parts = line.split("\r")
            result.append(parts[-1])   # last segment = final state of the line
        else:
            result.append(line)
    return "\n".join(result)


def _strip_ansi(text: str) -> str:
    text = _handle_cr(text)
    with _ansi_lock:
        text = _ansi.feed(text)
    return text


def _is_chrome(line: str) -> bool:
    """Return True for lines that are TUI chrome, not DM narration.

    The Claude CLI wraps responses in a box:
        ╭──────────────────╮
        │ narration text   │
        ╰──────────────────╯
    We strip the border characters from line edges first so that content
    lines like "│ The tavern smells of ale │" are NOT filtered — only pure
    border rows (all box chars, no letters) are treated as chrome.
    """
    stripped = line.strip()

    if not stripped:
        return False   # keep blank lines — they separate paragraphs

    # Strip leading/trailing box-drawing border chars to expose the real content.
    # "│ The tavern smells of ale │" → "The tavern smells of ale"
    content = stripped.strip(_BOX_CHAR_STRIP + " ")

    # If nothing remains, the line was entirely box-drawing chrome (a border row).
    if not content:
        return True

    # All remaining checks operate on content (without box border decoration).
    c = content

    # CLI prompt / spinner lines
    if c[0] in _SPINNER_CHARS:
        return True
    if c.startswith(_PROMPT_STARTS):
        return True

    # Common spinner word patterns (e.g. "Thinking…")
    if re.match(r"^[A-Z][a-z]+ing…?$", c):
        return True

    # Claude branding / metadata
    if "claude.ai" in c.lower():
        return True

    # Session-resume instructions emitted at end of response
    if c.startswith("Resume this session with:") or re.match(r"^claude\s+--resume\s+", c):
        return True

    # Status-bar patterns: cost, token counts, rate-limit bars
    # Note: "Tokens300/0" has no space — use \s* not \s+
    if re.search(r"Tokens\s*\d|5hr:|7d:|Session:|Total:\s*\$", c):
        return True

    # Model/plan header lines ("Sonnet 4.6", "Claude Pro", "Professional", etc.)
    if re.search(r"Sonnet|Haiku|Opus|Claude\s*(Pro|Max|Team|Code)\b|Professional\b|claude-\d", c, re.I):
        return True

    # Tool-use labels emitted by Claude CLI ("Bash command", "Read command", etc.)
    if re.match(r"^(Bash|Read|Write|Edit|Glob|Grep|WebFetch|WebSearch|TaskCreate|TaskUpdate|TaskGet|TaskList|NotebookEdit|Agent|ToolSearch|ExitPlanMode|EnterPlanMode|ScheduleWakeup|Monitor|RemoteTrigger|CronCreate|CronDelete|CronList|AskUserQuestion)(\s+(command|tool|result|call))?$", c, re.I):
        return True

    # Timestamp-prefixed lines ("3ts ago …", "2m ago …") — UI timestamps concatenated with content
    if re.match(r"^\d+\s*[smhdt]+s?\s*(ago\s*)?[A-Z]", c):
        return True

    # Bare numbers (token counts, cursor column positions, etc.)
    if re.match(r"^\d+$", c):
        return True

    # Single stray characters that are ANSI/escape remnants, not real words
    if len(c) == 1 and not c.isalpha():
        return True

    # Very short non-alpha fragments (≤3 chars with no letters = not narration)
    if len(c) <= 3 and not re.search(r"[a-zA-Z]{2}", c):
        return True

    return False


def _clean(text: str) -> str:
    text = _strip_ansi(text)
    lines = text.split("\n")
    kept = []
    for line in lines:
        if _is_chrome(line):
            continue
        # Strip box-border chars from edges so content reaches the browser clean.
        s = line.strip().strip(_BOX_CHAR_STRIP + " ")
        # Blank line → preserve as paragraph separator
        kept.append(s if s else "")
    # Collapse runs of more than two consecutive blank lines
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return result


# ─── Scene detection ──────────────────────────────────────────────────────────

_current_scene_name: str = "tavern"   # default — we start in the inn
_scene_buffer: list[str] = []
_BUFFER_WINDOW = 20   # analyse last N cleaned chunks together


def _detect_scene(text: str) -> Optional[dict]:
    global _current_scene_name, _scene_buffer

    _scene_buffer.append(text.lower())
    if len(_scene_buffer) > _BUFFER_WINDOW:
        _scene_buffer.pop(0)

    window = " ".join(_scene_buffer)

    scores: dict[str, int] = {}
    for scene_name in SCENE_PRIORITY:
        scene = SCENES[scene_name]
        score = sum(window.count(kw) for kw in scene["keywords"])
        if score > 0:
            scores[scene_name] = score

    if not scores:
        return None

    best = max(scores, key=lambda k: scores[k])
    if best == _current_scene_name:
        return None   # no change

    _current_scene_name = best
    return SCENES[best] | {"name": best}


# ─── SSE client registry ─────────────────────────────────────────────────────

_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

# ─── Broadcast journal (polling fallback) ────────────────────────────────────
# Some proxies buffer a long-lived response instead of streaming it — a
# Cloudflare quick tunnel does exactly this, so /stream stays open and silent
# while ordinary requests pass fine. Every broadcast is therefore also recorded
# here with a sequence number, and a browser that never receives an SSE event
# falls back to polling /tail?since=<seq> for the same payloads.
_broadcast_journal: "deque[tuple[int, dict]]" = deque(maxlen=500)
_broadcast_seq = 0
_journal_lock = threading.Lock()


def _record_broadcast(payload: dict) -> None:
    global _broadcast_seq
    with _journal_lock:
        _broadcast_seq += 1
        _broadcast_journal.append((_broadcast_seq, payload))
# Maps a connected SSE client (queue) → the character it's bound to, if any.
# Phones connect to /stream?character=<name>; the main display has no character.
# Lets a dice-request know whether a target PC has a live phone (→ route there)
# or not (→ open the on-screen roller). Guarded by _clients_lock.
_client_chars: "dict[queue.Queue, str]" = {}


# Polling clients hold no SSE connection, so presence is tracked by the
# timestamp of their last /tail poll instead. Two poll intervals of slack.
_polling_chars: dict[str, float] = {}
_polling_lock = threading.Lock()
POLLING_PRESENCE_TTL = 6.0


def _note_polling_char(char: str) -> None:
    c = (char or "").strip().lower()[:48]
    if not c:
        return
    with _polling_lock:
        _polling_chars[c] = _time.time()


def _phone_present(char: str) -> bool:
    """True if a phone bound to this character is connected — by SSE or polling."""
    c = (char or "").strip().lower()
    if not c:
        return False
    with _clients_lock:
        if c in _client_chars.values():
            return True
    with _polling_lock:
        return (_time.time() - _polling_chars.get(c, 0.0)) < POLLING_PRESENCE_TTL

# ─── Text replay log ──────────────────────────────────────────────────────────
# Stores cleaned text chunks so late-connecting browsers can catch up.
# Persisted to LOG_FILE so it survives Flask restarts (Chromecast reconnects, new sessions).
# Full body retention (player requirement, 2026-08-18): cap raised 60 → 2000 with
# full reload at startup (_load_log), so cross-session body text stays recoverable
# and archivable in both formats. On plugin updates, re-check these three spots.
_text_log: deque = deque(maxlen=2000)
_text_log_lock = threading.Lock()

# ─── Session tail buffer ──────────────────────────────────────────────────────
# Rolling buffer of the last 30 text events — written to session_tail.json after
# every /chunk POST so it survives crashes. Read at /dnd load for display replay.
# Path is campaign-specific so tails from different campaigns don't overwrite each other.
#
# ROBUSTNESS GUARANTEES (after the 2026-05-01 wipe-bug fix):
#   1. _load_tail is NON-DESTRUCTIVE: it never wipes the in-memory buffer based
#      on an empty/filtered-out load. If the file is empty, missing, or every
#      entry is filtered out by campaign mismatch, the existing buffer stays.
#   2. _persist_tail SKIPS ON EMPTY: it never overwrites an existing non-empty
#      file with an empty buffer. This breaks the "filter zeros buffer →
#      persist writes [] → file lost" failure chain.
#   3. _persist_tail uses ATOMIC WRITES: writes to a tempfile and atomically
#      renames into place, so a partial/crashed write can never produce a
#      truncated or zero-byte file.
#   4. The legacy fallback path is GONE. Tails only ever land in the campaign-
#      specific file. If CAMP_FILE is missing/empty when persist would fire,
#      we keep the buffer in memory and skip the write rather than dropping
#      events into a shared file that bleeds across campaigns.
_tail_buffer: deque = deque(maxlen=30)
_tail_lock   = threading.Lock()


def _get_tail_file() -> "str | None":
    """Return the campaign-specific tail path, or None if no campaign is set.

    Previously this fell back to a process-local path on the skill side. That
    fallback caused tail bleed across campaigns and made the wipe-on-load bug
    much harder to diagnose. The new contract: campaign-specific or nothing.
    """
    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
        if camp:
            return str(_find_campaign(camp) / "session_tail.json")
    except Exception:
        pass
    return None


def _persist_tail() -> None:
    """Write _tail_buffer to disk. Refuses to overwrite content with empty.

    Atomic-write guarantee: writes to <path>.tmp then renames, so observers
    (the next /dnd load reading the file) never see a partial or zero-byte
    state.
    """
    path = _get_tail_file()
    if not path:
        # No active campaign — keep the buffer in memory, skip disk.
        return
    try:
        with _tail_lock:
            data = list(_tail_buffer)
        # Skip-on-empty guard: never blank a file that currently has content.
        if not data and os.path.exists(path):
            try:
                if os.path.getsize(path) > 2:  # 2 bytes = "[]"
                    print(f"_persist_tail: skipping empty write — {path} has content",
                          file=sys.stderr)
                    return
            except OSError:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception as e:
        print(f"_persist_tail: write failed: {e}", file=sys.stderr)


def _load_tail() -> None:
    """Load tail from disk into the buffer. NON-DESTRUCTIVE on empty/mismatch.

    Old behavior: cleared the buffer first, then re-appended filtered entries.
    This created the wipe bug: if every entry was filtered out (campaign
    mismatch) the buffer ended up empty and the next persist wrote [] to disk.

    New behavior: build the candidate buffer first, then ONLY swap it into
    place if at least one entry survived filtering. If nothing survives, the
    in-memory buffer is left alone — preserves whatever was already loaded.
    """
    path = _get_tail_file()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return  # No file yet — keep in-memory state
    except (OSError, json.JSONDecodeError) as e:
        print(f"_load_tail: read failed for {path}: {e}", file=sys.stderr)
        return
    if not isinstance(data, list):
        print(f"_load_tail: file content is not a list — leaving buffer alone",
              file=sys.stderr)
        return

    try:
        current_camp = open(CAMP_FILE, encoding="utf-8").read().strip()
    except Exception:
        current_camp = ""

    candidate: list = []
    for item in data[-30:]:
        if not isinstance(item, dict):
            continue
        item_camp = item.get("_camp", "")
        # If we know the campaign and the entry stamps a different campaign,
        # skip it. Entries with no stamp are kept (legacy data + tolerance).
        if current_camp and item_camp and item_camp != current_camp:
            continue
        candidate.append(item)

    if not candidate:
        # Loaded data filtered down to nothing. DO NOT replace the buffer —
        # this is the wipe-bug guard.
        return

    with _tail_lock:
        _tail_buffer.clear()
        for item in candidate:
            _tail_buffer.append(item)


_load_tail()


def _persist_log() -> None:
    """Write the current text log to disk. Called after every chunk."""
    try:
        with _text_log_lock:
            data = list(_text_log)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_log() -> None:
    """Load a previously persisted text log on startup.
    Handles both old string format and new dict format."""
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _text_log_lock:
            _text_log.clear()
            for item in data[:]:
                # Migrate old plain-string entries to dict format
                if isinstance(item, str):
                    item = {"text": item}
                _text_log.append(item)
    except Exception:
        pass


_load_log()


# ─── Character / combat stats ─────────────────────────────────────────────────
# Stored as {"players": [...], "turn_order": {...}|null}
# Players are merged by name so partial updates (just HP, just XP) work.

_current_stats: dict = {}
_stats_lock = threading.Lock()


def _persist_stats() -> None:
    try:
        with _stats_lock:
            data = dict(_current_stats)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_stats() -> None:
    global _expected_count
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _stats_lock:
            _current_stats.update(data)
        # Initialise expected player count from persisted stats so solo-mode
        # detection is correct immediately after restart, without waiting for
        # the next /stats POST.
        loaded_players = data.get("players", [])
        if loaded_players:
            _expected_count = max(1, len(loaded_players))
    except Exception:
        pass


_load_stats()


# ─── Player input queue ───────────────────────────────────────────────────────
# Stores actions submitted from the display companion (iPad etc.) until the DM
# triggers the next turn. Drained by check_input.py via /player-input/drain.

_input_queue: list[dict] = []
_input_lock = threading.Lock()

# Pending DM-issued dice requests: request_id → {chars: set[str], meta: {...}, started_at: float}
# A request is "complete" when its chars set is empty (every prescribed player rolled).
# send.py --wait polls GET /dice-request/<id> to know when the DM can move on.
_dice_pending: dict = {}
_dice_pending_lock = threading.Lock()


def _dice_pending_snapshot() -> list:
    with _dice_pending_lock:
        return [
            {"request_id": rid, "pending": sorted(e["chars"]), "label": e["meta"].get("label", "")}
            for rid, e in _dice_pending.items() if e["chars"]
        ]


# ─── Response window ─────────────────────────────────────────────────────────
# A failed roll is not a resolved roll. Heroic Inspiration, Tactical Mind, a
# held Bardic Inspiration die — all of them are spent *after* the number is
# known, and a display that resolves the outcome the instant the die stops
# takes that decision away from the table.
#
# So a roll that comes in under its DC opens a short window instead. The offers
# come from jev_window (what is legal), the countdown is the player's, and
# nothing downstream moves until they choose or it expires. Set the seconds to
# 0 to switch the whole thing off.

# 30 is a floor, not a preference: the window is the only thing standing
# between a failed roll and its consequence, and a remote player talking over
# voice needs longer to notice it than one sitting at the table. Anything
# shorter reads as a card that flickered past. 0 switches the window off
# entirely — that is the only way below the floor.
try:
    RESPONSE_WINDOW_SECONDS = int(os.environ.get("DND_RESPONSE_WINDOW_SECONDS", "45"))
except ValueError:
    RESPONSE_WINDOW_SECONDS = 45
if RESPONSE_WINDOW_SECONDS != 0:
    RESPONSE_WINDOW_SECONDS = max(30, RESPONSE_WINDOW_SECONDS)

_resp_windows: dict = {}
_resp_lock = threading.Lock()


def _resp_snapshot() -> list:
    """Open windows, for the on-connect burst.

    A phone that reconnects mid-countdown has to get its buttons back; the
    window is the one piece of display state where a dropped frame costs the
    player the decision.
    """
    now = _time.time()
    with _resp_lock:
        return [dict(w, remaining=max(0.0, round(w["expires_at"] - now, 1)))
                for w in _resp_windows.values() if w["expires_at"] > now]


def _party_inspiration() -> "tuple[list, dict]":
    """Who is at the table, and which of them is holding Heroic Inspiration.

    Both come off the stats the display already keeps, so the window never
    offers a reroll to someone who has nothing to spend.
    """
    with _stats_lock:
        players = list(_current_stats.get("players", []))
    names = [p.get("name", "") for p in players if p.get("name")]
    held = {p.get("name", ""): bool(p.get("inspiration")) for p in players if p.get("name")}
    return names, held


# Conditions that take the holder out of the decision entirely. The window is
# a choice, and in these states there is nobody left to make one.
_NO_ACTION_CONDITIONS = ("incapacitated", "unconscious", "paralyzed",
                         "petrified", "stunned", "dead")


def _player_row(name: str) -> dict:
    """This character's stats as the display currently holds them."""
    want = _fold_name(name)
    with _stats_lock:
        return next((dict(p) for p in _current_stats.get("players", [])
                     if _fold_name(p.get("name", "")) == want), {})


def _find_counter(feature: str, row: dict) -> "dict | None":
    """The limited-use counter a named feature draws on, if one is declared.

    A feature and the resource it spends are usually not the same word:
    Tactical Mind spends a use of Second Wind, and Second Wind spends it too.
    Nothing on the wire says so — the offer carries the feature's name and the
    model only says *that* a limited use is spent, not which. So the link is
    declared rather than guessed: a counter may list the features that feed on
    it, and a feature with no counter of its own is looked up there.

        "uses": {"Second Wind": {"left": 2, "max": 2,
                                 "feeds": ["Tactical Mind"]}}
    """
    want = _fold_name(feature)
    counters = row.get("uses") or {}
    for name, counter in counters.items():
        if _fold_name(name) == want and isinstance(counter, dict):
            return counter
    for counter in counters.values():
        if not isinstance(counter, dict):
            continue
        if any(_fold_name(f) == want for f in (counter.get("feeds") or [])):
            return counter
    return None


def _uses_left(feature: str, row: dict) -> bool:
    """Has this feature got a charge left, where the display keeps one?"""
    counter = _find_counter(feature, row)
    if counter is not None:
        try:
            return int(counter.get("left", 0)) > 0
        except (TypeError, ValueError):
            return True
    # Second Wind predates the generic counter and has had its own flag in the
    # sidebar all along. Read it rather than leave the one charge this table
    # spends most as the only unchecked resource on the screen.
    if _fold_name(feature) == _fold_name("Second Wind") \
            and row.get("second_wind") is not None:
        return bool(row.get("second_wind"))
    return True          # untracked — see _can_afford on why that is a yes


def _can_afford(offer: dict, row: dict) -> bool:
    """Does the display's own bookkeeping say this offer is still payable?

    Jev answers whether a feature *may* legally be spent on a roll of this
    kind. That is a reading of the feature's text, it is the same answer on
    every failed roll, and it is why the answer can be cached. Whether the
    holder still has the charge, the slot, or their wits is not a reading of
    anything — it is a counter, it changes between one roll and the next, and
    counters are the display's to keep. That split is not new here: Heroic
    Inspiration was always answered on this side for exactly this reason.

    Untracked resources are a yes. Refusing a legal feature costs the player
    the feature; offering one they cannot pay costs a glance, with the DM
    watching the same screen — the same asymmetry OFFER_MIN is set by.
    """
    held = {_fold_name(c) for c in (row.get("conditions") or [])}
    if held & {_fold_name(c) for c in _NO_ACTION_CONDITIONS}:
        return False

    kaynak = offer.get("kaynak", "")
    if kaynak == "heroic_inspiration":
        return bool(row.get("inspiration"))
    if kaynak == "buyu_slotu":
        return any(int(slot.get("max", 0)) > int(slot.get("used", 0))
                   for slot in (row.get("spell_slots") or {}).values()
                   if isinstance(slot, dict))
    if kaynak == "sinirli_kullanim":
        return _uses_left(offer.get("feature", ""), row)
    return True          # bedava, or a resource nobody named


def _note_concentration(offer: dict, row: dict) -> dict:
    """Price a concentration offer against what the holder is already holding.

    Nobody concentrates on two things, so spending this drops the other one.
    That is a cost, not an illegality — refusing the offer would take the
    decision away, which is the one thing the window exists not to do. So the
    button stays and says what it will cost; `_switch_concentration` is what
    makes sure only one is ever held.

    The warning rides in `detail`, which the client already prints under the
    feature's name — a price nobody reads is not a price.
    """
    held = str(row.get("concentration") or "").strip()
    if not offer.get("konsantrasyon") or not held:
        return offer
    if _fold_name(held) == _fold_name(offer.get("feature", "")):
        return offer          # re-upping the same effect costs nothing
    return {**offer,
            "detail": f"{offer.get('detail', '')} "
                      f"{held} üzerindeki konsantrasyonun düşer.".strip(),
            "drops_concentration": held}


def _switch_concentration(name: str, spell: str) -> None:
    """Move a character's concentration onto `spell`, dropping what it was on.

    Enforced here rather than left to the DM's memory: this is the one moment
    the display knows a concentration effect just started, and a table that
    ends up holding two has nothing that would notice.
    """
    snapshot, dropped = None, ""
    with _stats_lock:
        match = next((p for p in _current_stats.get("players", [])
                      if _fold_name(p.get("name", "")) == _fold_name(name)), None)
        if match is not None:
            held = str(match.get("concentration") or "").strip()
            if _fold_name(held) != _fold_name(spell):
                dropped = held
                if held:
                    match["effects"] = [
                        e for e in match.get("effects", [])
                        if _fold_name(e.get("name", "")) != _fold_name(held)]
                match["concentration"] = spell
                snapshot = dict(_current_stats)
    if snapshot is not None:
        _persist_stats()
        _broadcast({"stats": snapshot})
    if dropped:
        _feed_line(f"{name} — {spell} için {dropped} üzerindeki "
                   f"konsantrasyonunu bıraktı.")


def _spend_limited_use(feature: str, name: str) -> None:
    """Drop one charge of a named feature, by the counter that offered it.

    The mirror of _spend_inspiration, and for the same reason: a resource the
    window spent has to come down where the window read it, or the next failed
    roll offers a charge that is already gone.
    """
    snapshot = None
    want = _fold_name(feature)
    with _stats_lock:
        match = next((p for p in _current_stats.get("players", [])
                      if _fold_name(p.get("name", "")) == _fold_name(name)), None)
        if match:
            counter = _find_counter(feature, match)
            if counter is not None:
                counter["left"] = max(0, int(counter.get("left", 0)) - 1)
                snapshot = dict(_current_stats)
            elif want == _fold_name("Second Wind") and match.get("second_wind"):
                match["second_wind"] = False
                snapshot = dict(_current_stats)
    if snapshot is not None:
        _persist_stats()
        _broadcast({"stats": snapshot})


def _open_response_window(roller: str, meta: dict, total: int, request_id: str) -> None:
    """Work out the offers and broadcast the window. Runs off the request thread.

    The phone is mid-animation when this starts: the roll result has already
    gone back over HTTP, and the window arrives a beat later. From the second
    time a feature is seen it comes out of jev_window's cache, so that beat is
    usually the SSE hop alone.
    """
    if _jev_window is None or RESPONSE_WINDOW_SECONDS <= 0:
        return
    dc = meta.get("dc")
    if not isinstance(dc, int) or total >= dc:
        return                      # nothing to rescue
    try:
        campaign = open(CAMP_FILE, encoding="utf-8").read().strip()
    except Exception:
        campaign = ""
    if not campaign:
        return

    present, held = _party_inspiration()
    if roller not in present:
        present = present + [roller]
    started = _time.time()
    try:
        offers = _jev_window.offers(
            campaign, roller, present,
            _jev_window.roll_kind(meta.get("label", "")),
            passed=False, inspiration=held)
    except Exception:
        return
    # Legal is not the same as payable. Drop what the sheet permits but the
    # counters no longer cover, before deciding there is a window at all: an
    # offer nobody can afford is not a window, it is a button that fails.
    offers = [o for o in offers if _can_afford(o, _player_row(o["character"]))]
    offers = [_note_concentration(o, _player_row(o["character"])) for o in offers]
    if not offers:
        return
    # Working out the offers took longer than the window would have lasted, so
    # the table has already moved on. Opening now would interrupt the narration
    # rather than precede it.
    if _time.time() - started > RESPONSE_WINDOW_SECONDS:
        return

    window = {
        "window_id": secrets.token_hex(6),
        "roller": roller,
        "label": meta.get("label", ""),
        "total": total,
        "dc": dc,
        "spec": meta.get("spec", "1d20"),
        "modifier": int(meta.get("modifier", 0) or 0),
        "advantage": meta.get("advantage", "normal"),
        "request_id": request_id,
        "offers": offers,
        "seconds": RESPONSE_WINDOW_SECONDS,
        "expires_at": _time.time() + RESPONSE_WINDOW_SECONDS,
    }
    with _resp_lock:
        _resp_windows[window["window_id"]] = window
    _broadcast({"response_window": dict(window, remaining=float(RESPONSE_WINDOW_SECONDS))})

    def _expire():
        _time.sleep(RESPONSE_WINDOW_SECONDS + 0.5)
        _close_response_window(window["window_id"], "timeout")

    threading.Thread(target=_expire, daemon=True).start()


def _close_response_window(window_id: str, reason: str, note: str = "") -> "dict | None":
    """Close a window once. Returns the window if this call is the one that closed it."""
    with _resp_lock:
        window = _resp_windows.pop(window_id, None)
    if window is None:
        return None
    _broadcast({"response_window_closed": {"window_id": window_id, "reason": reason,
                                           "note": note}})
    # Nobody spent, so the number the DM already saw is the final one. Say so
    # anyway: under this flow the DM narrated the attempt and is waiting to be
    # told how it ended, and "nothing happened" is an answer they need.
    if reason in ("timeout", "passed"):
        dc = window.get("dc")
        verdict = (f" vs DC {dc} — {'geçti' if window['total'] >= dc else 'kaldı'}"
                   if isinstance(dc, int) else "")
        _resolve_outcome(f"{window['roller']} — {window['total']}{verdict} "
                         f"(kimse bir şey harcamadı)")
    return window


OUTCOME_FILE = rt(".roll_outcomes")
_outcome_lock = threading.Lock()


def _resolve_outcome(text: str) -> None:
    """The last word on a roll that went through a window.

    The DM is released the moment the die lands, so under this table's flow
    they narrate the *attempt* and leave the outcome open. This is the line
    that closes it: on the feed for the table, and queued for the DM's next
    turn, because the spend usually lands after the narration is already
    written.

    It is a separate file from `.input_queue` on purpose — that one is the
    players' declarations and is rewritten wholesale when a turn is staged.
    """
    _feed_line(text)
    try:
        with _outcome_lock:
            with open(OUTCOME_FILE, "a", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _feed_line(text: str) -> None:
    """Put a line on the feed the way send.py would, so it survives replay."""
    entry = {"text": text}
    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
        if camp:
            entry["_camp"] = camp
    except Exception:
        pass
    with _text_log_lock:
        _text_log.append(entry)
    with _tail_lock:
        _tail_buffer.append(entry)
    _persist_log()
    _persist_tail()
    _broadcast({"text": text})


def _load_input_queue() -> None:
    global _input_queue
    try:
        with open(INPUT_FILE, encoding="utf-8") as f:
            _input_queue = json.load(f)
    except Exception:
        _input_queue = []


def _persist_input_queue() -> None:
    try:
        with open(INPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(_input_queue, f)
    except Exception:
        pass


_load_input_queue()


def _visible_to(payload: dict, char: str) -> bool:
    """Is this payload visible to a viewer bound to `char`?

    A payload with no "to" is public. A payload addressed to someone is seen by
    that player and by the DM screen (which binds no character); everyone else
    never receives it. The check is the only thing standing between a private
    line and the whole table, so it fails closed: an unrecognised viewer with a
    bound name that does not match sees nothing.
    """
    viewer = (char or "").strip().lower()
    # The other private address: not one character, but the screen that binds
    # none. A hidden token's bytes must not reach a seat, and "to" cannot say
    # that — it names a player, and the DM is not one.
    if payload.get("dm_only"):
        return not viewer
    target = (payload.get("to") or "").strip().lower()
    if not target:
        return True
    if not viewer:
        return True          # DM screen — binds no character, sees everything
    return viewer == target


def _broadcast(payload: dict) -> None:
    _record_broadcast(payload)
    with _clients_lock:
        dead = []
        for q in _clients:
            if not _visible_to(payload, _client_chars.get(q, "")):
                continue
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)
            _client_chars.pop(q, None)


# ─── Minimap ──────────────────────────────────────────────────────────────────
# A map pinned in the corner of every screen, so "where are we, how far is it"
# is answered by looking rather than by asking. Sticky: stored here and replayed
# in the on-connect burst, because a map that vanishes on refresh is not a map.

_minimap: dict = {}
_minimap_lock = threading.Lock()


def _persist_minimap() -> None:
    """Keep the pinned map across a display restart — like stats, it is table
    state, and having it vanish when the server is bounced mid-session is the
    one thing a pinned map must not do."""
    try:
        with _minimap_lock:
            data = dict(_minimap)
        with open(MINIMAP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_minimap() -> None:
    global _minimap
    try:
        with open(MINIMAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("image"):
            _minimap = data
    except Exception:
        pass


_load_minimap()


# ─── Battle map ───────────────────────────────────────────────────────────────
# The tactical grid. Same slot on screen as the minimap, same sticky contract:
# kept here, persisted, replayed on connect — a fight that loses its board on a
# refresh is a fight nobody can adjudicate. Positions live in this one dict and
# nowhere else on the display; grid.py's verdicts are the DM's to act on, and
# the moves the DM then makes land here through /battle-map.

_battle_map: dict = {}
_battle_map_lock = threading.Lock()


def _persist_battle_map() -> None:
    try:
        with _battle_map_lock:
            data = dict(_battle_map)
        with open(BATTLE_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _load_battle_map() -> None:
    global _battle_map
    try:
        with open(BATTLE_MAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("spec"):
            _battle_map = data
    except Exception:
        pass


_load_battle_map()


def _active_turn_name() -> str:
    with _stats_lock:
        to = _current_stats.get("turn_order")
    if isinstance(to, dict):
        return str(to.get("current") or "")
    return ""


def _battle_map_payloads() -> "list[dict]":
    """The board as the wire carries it: the seats' view, then the DM's.

    Two payloads only when something is hidden. Order matters on the DM screen,
    which receives both and keeps the last — so the full view goes second.
    """
    with _battle_map_lock:
        state = dict(_battle_map)
    if not state or _battle_map_mod is None:
        return [{"battle_map": None}]
    public, full = _battle_map_mod.views(state, active=_active_turn_name())
    out = [{"battle_map": public}]
    if full is not None:
        out.append({"battle_map": full, "dm_only": True})
    return out


def _broadcast_battle_map() -> None:
    for payload in _battle_map_payloads():
        _broadcast(payload)


def _party_names() -> "set[str]":
    with _stats_lock:
        return {str(p.get("name", "")).strip().lower()
                for p in _current_stats.get("players", []) if p.get("name")}


# ─── Turn lint ────────────────────────────────────────────────────────────────
# Built lazily: it reads the party and the turn order through callables, so it
# is wired here, after both exist, and stays correct as they change.

_LINT = None


def _lint():
    global _LINT
    if _LINT is None and _turn_lint is not None:
        _LINT = _turn_lint.Linter(
            campaign_dir_for=lambda camp: _find_campaign(camp),
            party_names=lambda: {str(p.get("name", "")) for p in _current_stats.get("players", [])
                                 if p.get("name")},
            turn_active=lambda: bool(_active_turn_name()),
            board_tokens=_board_token_names,
        )
    return _LINT


def _board_token_names() -> "list[str]":
    """Everyone standing on the board, hidden ones included — the DM's line
    about an ambusher still has to move the ambusher."""
    with _battle_map_lock:
        return [str(t.get("name", "")) for t in (_battle_map.get("tokens") or [])
                if t.get("pos") and t.get("name")]


def _lint_observe(entry: dict) -> None:
    """Hand a player-facing line to the linter. Never raises, never delays."""
    linter = _lint()
    if linter is None:
        return
    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
        linter.observe(entry, camp)
    except Exception:
        pass


@app.route("/lint")
def lint_tail():
    """The last N findings, for the DM to read between scenes or sessions."""
    if not _token_ok():
        return "Forbidden", 403
    linter = _lint()
    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
        n = int(request.args.get("n", "20"))
    except Exception:
        camp, n = "", 20
    return jsonify({"campaign": camp, "findings": linter.tail(camp, n) if linter and camp else []})


@app.route("/battle-map", methods=["POST"])
def battle_map_route():
    """Set, move, hide, reveal, remove, advance or clear.

    Body, any combination — applied in this order:
        {"spec": {...grid spec...}, "handle": "kavran", "round": 1}   open a board
        {"pos": {"Dilaver": "C4", "Goblin": "G7"}}                    place / move
        {"type": {"Goblin": "npc"}}                                   override kind
        {"hide": ["Goblin"]}  {"reveal": ["Goblin"]}                  DM-only tokens
        {"remove": ["Goblin"]}                                        off the board
        {"round": 3}                                                  new round
        {"clear": true}                                               back to theatre

    A token's kind defaults from the party list — a name the sidebar knows is a
    PC, anything else an NPC — so the DM never types "pc". Positions come in as
    tile labels and are stored as given; grid.py is what judges whether a move
    was legal, before the DM sends it here.
    """
    if not _token_ok():
        return "Forbidden", 403
    if _battle_map_mod is None:
        return jsonify({"error": "battle map module unavailable"}), 503
    data = request.get_json(silent=True) or {}

    if data.get("clear"):
        with _battle_map_lock:
            _battle_map.clear()
        _persist_battle_map()
        _broadcast_battle_map()
        return "", 204

    party = _party_names()
    with _battle_map_lock:
        if "spec" in data:
            spec = data["spec"]
            errors = _battle_map_mod.check_spec(spec)
            if errors:
                return jsonify({"error": "invalid grid spec", "details": errors}), 400
            _battle_map.clear()
            _battle_map.update({
                "spec": spec,
                "handle": str(data.get("handle") or spec.get("handle") or "")[:60],
                "round": int(data.get("round") or 1),
                "tokens": [],
            })
        if not _battle_map:
            return jsonify({"error": "no board open — send a spec first"}), 409

        tokens: list = _battle_map.setdefault("tokens", [])

        def _tok(name: str) -> dict:
            key = name.strip().lower()
            for t in tokens:
                if str(t.get("name", "")).strip().lower() == key:
                    return t
            t = {"name": name.strip()[:40], "type": "pc" if key in party else "npc"}
            tokens.append(t)
            return t

        cols, rows = int(_battle_map["spec"]["cols"]), int(_battle_map["spec"]["rows"])
        for name, pos in (data.get("pos") or {}).items():
            pos = str(pos or "").strip().upper()
            if pos == "-":
                pos = ""              # "off the board", the way the DM types it
            if pos:
                try:
                    c, r = _battle_map_mod.parse_tile(pos)
                except ValueError:
                    return jsonify({"error": f"bad tile {pos!r} for {name}"}), 400
                if not (0 <= c < cols and 0 <= r < rows):
                    return jsonify({"error": f"{pos} is outside the {cols}x{rows} grid"}), 400
            _tok(name)["pos"] = pos or None
        for name, kind in (data.get("type") or {}).items():
            if kind in ("pc", "npc"):
                _tok(name)["type"] = kind
        for name in data.get("hide") or []:
            _tok(name)["hidden"] = True
        for name in data.get("reveal") or []:
            _tok(name)["hidden"] = False
        gone = {str(n).strip().lower() for n in (data.get("remove") or [])}
        if gone:
            tokens[:] = [t for t in tokens
                         if str(t.get("name", "")).strip().lower() not in gone]
        if "round" in data and "spec" not in data:
            try:
                _battle_map["round"] = int(data["round"])
            except (TypeError, ValueError):
                pass

    _persist_battle_map()
    _broadcast_battle_map()
    linter = _lint()
    if linter is not None:
        linter.note_moved(list((data.get("pos") or {}).keys())
                          + list(data.get("remove") or []))
    return "", 204


@app.route("/minimap", methods=["POST"])
def minimap_route():
    """Set or clear the pinned map. Body: {"image": "/scenes/x.jpg", "label": "..."}
    An empty or missing image clears it."""
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    img = str(data.get("image", "") or "").strip()[:300]
    label = str(data.get("label", "") or "").strip()[:60]
    if img and not img.startswith("/scenes/"):
        return jsonify({"error": "image must be a /scenes/ path"}), 400
    with _minimap_lock:
        _minimap.clear()
        if img:
            _minimap.update({"image": img, "label": label})
        payload = dict(_minimap)
    _persist_minimap()
    _broadcast({"minimap": payload})
    return "", 204


# ─── Battle VFX ───────────────────────────────────────────────────────────────
# Screen-level effects (flash, shake, vignette, fog) the browser plays over the
# scene. Two sources: an explicit POST /vfx from send.py --vfx, and automatic
# detection on every --dice line so a hit, a miss and a crit read differently on
# screen without the DM doing anything. Effects are broadcast only, never
# logged: a replay must not re-fire tonight's fireballs.

_VFX_NAMES = {
    "hit", "miss", "crit", "fumble", "success", "fail",
    "fire", "thunder", "radiant", "psychic", "thorn", "cold", "lightning", "acid", "poison", "necrotic",
    "damage", "heal", "fog", "silver", "night", "dawn",
}

# A VFX may carry a sound; names come from audio.py's synth set.
_VFX_SFX = {
    "fire": "fire", "thunder": "low_hum", "radiant": "magic", "psychic": "magic",
    "thorn": "impact", "lightning": "low_hum", "cold": "breath", "acid": "magic",
    "poison": "breath", "necrotic": "magic",
    "hit": "impact", "crit": "sword", "damage": "thud", "silver": "sword",
    "fog": "breath", "night": None, "dawn": None, "heal": None,
    "miss": None, "fumble": "thud", "success": None, "fail": None,
}

_VFX_ELEMENT = [
    (re.compile(r"fire ?bolt|produce flame|\bfire\b|\bateş\b|alev|yakt|yand", re.I), "fire"),
    (re.compile(r"thunderwave|thunder|gök ?gürült", re.I), "thunder"),
    (re.compile(r"starry wisp|radiant|sacred flame|guiding bolt|ışı[kğ]", re.I), "radiant"),
    (re.compile(r"vicious mockery|psychic|dissonant", re.I), "psychic"),
    (re.compile(r"thorn whip|entangle|diken|sarmaşık", re.I), "thorn"),
    (re.compile(r"ray of frost|\bcold\b|frost|\bbuz\b|soğuk hasar", re.I), "cold"),
    (re.compile(r"lightning|shocking grasp|yıldırım|şimşek", re.I), "lightning"),
    (re.compile(r"acid|asit", re.I), "acid"),
    (re.compile(r"poison|zehir", re.I), "poison"),
    (re.compile(r"necrotic|nekrotik", re.I), "necrotic"),
    (re.compile(r"healing word|cure wounds|\bheal|iyileş|şifa", re.I), "heal"),
    (re.compile(r"gümüş|silver", re.I), "silver"),
]
_VFX_HIT   = re.compile(r"→\s*(vurdu|isabet|hit\b|başarılı|success)|\bhit!|\bvurdu\b", re.I)
_VFX_MISS  = re.compile(r"→\s*(ıskaladı|miss|başarısız|fail)|\bmiss(ed)?\b|ıskala", re.I)
_VFX_CRIT  = re.compile(r"doğal 20|natural 20|nat ?20|\bcrit|\[20\]", re.I)
_VFX_FUMBLE= re.compile(r"doğal 1\b|natural 1\b|nat ?1\b|\[1\]", re.I)
_VFX_ATTACK= re.compile(r"saldırı|attack|vs AC|AC \d", re.I)


def _detect_dice_vfx(text: str) -> "str | None":
    """Pick one effect for a dice line. Crit and fumble win, then the damage
    element on a hit, then plain hit/miss for attacks, then a soft success or
    fail glint for checks and saves. Text with no verdict gets nothing."""
    t = text or ""
    is_attack = bool(_VFX_ATTACK.search(t))
    hit  = bool(_VFX_HIT.search(t))
    miss = bool(_VFX_MISS.search(t))
    if _VFX_CRIT.search(t) and hit:
        return "crit"
    if _VFX_FUMBLE.search(t) and (miss or not hit):
        return "fumble"
    if hit:
        for rx, name in _VFX_ELEMENT:
            if rx.search(t):
                return name
        return "hit" if is_attack else "success"
    if miss:
        return "miss" if is_attack else "fail"
    return None


@app.route("/vfx", methods=["POST"])
def vfx_route():
    """Explicit effect from send.py --vfx <name>. Broadcast only."""
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip().lower()[:24]
    if name not in _VFX_NAMES:
        return jsonify({"error": "unknown vfx", "known": sorted(_VFX_NAMES)}), 400
    _broadcast({"vfx": name})
    sfx = _VFX_SFX.get(name)
    if sfx and _audio:
        _broadcast({"sfx": sfx})
    return "", 204


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Pass LAN token to template so the browser can authenticate /help-request
    return render_template(
        "index.html",
        lan_token=_lan_token or "",
        narrator_voice=_read_narrator_voice(),
        tts_available=(_tts is not None),
        # The voice catalog follows the active provider, so the dropdown is
        # rendered from the server rather than hardcoded in the template.
        tts_voices_male=(_tts.voices_male() if _tts else []),
        tts_voices_female=(_tts.voices_female() if _tts else []),
    )


@app.route("/icons/<path:filename>")
def serve_icon(filename):
    """Serve icons, favicon, and brand assets from display/icons/."""
    _icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(_icons_dir, filename)


@app.route("/scenes/<path:filename>")
def serve_scene(filename):
    """Serve pre-generated scene images from <campaign>/scenes/ (or DND_SCENES_DIR).

    Lets the DM show hand-made artwork via `send.py --image-file <name>` instead
    of a generated URL. Only the active campaign's folder is exposed.
    """
    scenes_dir = os.environ.get("DND_SCENES_DIR", "").strip()
    if not scenes_dir:
        name = _active_campaign_name()
        if not name:
            return "no active campaign", 404
        try:
            scenes_dir = str(_find_campaign(name) / "scenes")
        except ValueError:
            return "campaign not found", 404
    return send_from_directory(scenes_dir, filename)


@app.route("/favicon.ico")
def favicon():
    _icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(_icons_dir, "favicon.ico",
                               mimetype="image/vnd.microsoft.icon")


@app.route("/srd-lookup")
def srd_lookup():
    """Look up a spell, item, condition, feature, or monster by name.

    Query params:
        name      — the name to look up (required)
        category  — spell | item | equipment | magic_item | condition | monster | feature (optional)
        level     — character level (1–20); collapses scale progressions to the matching entry

    Returns JSON: {"found": bool, "name": str, "category": str, "text": str}
    """
    name     = request.args.get("name", "").strip()[:120]
    category = request.args.get("category", "").strip().lower() or None
    level_s  = request.args.get("level", "").strip()
    level    = int(level_s) if level_s.isdigit() and 1 <= int(level_s) <= 20 else None
    if not name:
        return jsonify({"found": False, "error": "name required"}), 400
    if not _SRD_AVAILABLE or _lookup is None:
        return jsonify({"found": False, "error": "SRD dataset not loaded"}), 503

    text = _lookup.lookup_with_level(name, category=category, level=level)
    if text:
        rec = _lookup.lookup_record(name, category=category)
        resolved_cat = (rec or {}).get("_cat", category or "")
        return jsonify({"found": True, "name": name, "category": resolved_cat, "text": text})
    # Not found — offer near-miss "did you mean?" suggestions (typo recovery)
    # plus the wikidot fallback URL so the frontend can still link out.
    # `ref` is {} when there is no VERIFIED destination for this category, and
    # the frontend renders no link at all in that case: a guessed URL reads as
    # an answer and dead-ends, which is worse than saying nothing.
    ref = _lookup.reference_url(name, category=category)
    suggestions = []
    try:
        for sg_name, sg_cat in _lookup.suggest(name, category=category, n=3):
            suggestions.append({"name": sg_name, "category": sg_cat})
    except Exception:
        pass  # suggestion is best-effort; never fail the lookup over it
    return jsonify({"found": False, "name": name,
                    "reference_url": ref.get("url", ""),
                    "reference_label": ref.get("label", ""),
                    # kept so an older cached frontend still gets a link
                    "wikidot_url": ref.get("url", ""),
                    "suggestions": suggestions})


@app.route("/snapshot")
def snapshot():
    """On-connect state for a polling client — the SSE stream's opening burst.

    A browser whose stream is being buffered by a proxy never receives those
    payloads, so it would render an empty sidebar, an input panel with no
    character tabs, and miss any dice request already in flight.
    """
    if not _token_ok():
        return "Forbidden", 403
    events: list[dict] = []
    _initial_payloads(events.append,
                      request.args.get("character") or request.args.get("char") or "")
    with _journal_lock:
        current = _broadcast_seq
    return jsonify({"seq": current, "events": events})


@app.route("/tail")
def tail_since():
    """Polling fallback for clients whose SSE stream is being buffered.

    Returns every broadcast payload newer than ?since=<seq>, plus the current
    sequence number to pass back next time. `since=0` (a fresh poller) returns
    only the current sequence so the client starts from now and does not replay
    the whole journal on top of the snapshot it already rendered.
    """
    if not _token_ok():
        return "Forbidden", 403
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    _note_polling_char(request.args.get("character") or request.args.get("char") or "")
    with _journal_lock:
        current = _broadcast_seq
        # The client's baseline always comes from /snapshot, so a plain
        # "everything after N" filter is right even at N=0 (a fresh server,
        # where dropping n=1 would silently lose the first event).
        events = [p for (n, p) in _broadcast_journal if n > since]
    _viewer = request.args.get("character") or request.args.get("char") or ""
    events = [p for p in events if _visible_to(p, _viewer)]
    return jsonify({"seq": current, "events": events})


@app.route("/tts-sample")
def tts_sample_index():
    """TEMP: side-by-side audition of the local/free TTS candidates."""
    rows = "".join(
        f'<section><h2>{title}</h2><p>{note}</p>'
        f'<audio controls preload="none" src="/tts-sample/{key}"></audio></section>'
        for key, title, note in [
            ("piper", "Piper · tr_TR-dfki-medium",
             "Tamamen çevrimdışı · 2.2 sn · kotasız, internetsiz"),
            ("ahmet", "Edge · tr-TR-AhmetNeural (erkek)",
             "Sinir ağı · 2.7 sn · anahtarsız, kotasız, internet gerekir"),
            ("emel", "Edge · tr-TR-EmelNeural (kadın)",
             "Sinir ağı · 4.1 sn · anahtarsız, kotasız, internet gerekir"),
        ])
    return Response(
        "<!doctype html><meta charset=utf-8><title>TTS karşılaştırma</title>"
        "<style>body{background:#14100c;color:#e8dcc8;font:16px/1.6 system-ui;"
        "max-width:680px;margin:0 auto;padding:32px 20px}h1{font-size:20px}"
        "section{border:1px solid #3a3128;border-radius:4px;padding:16px;margin:16px 0}"
        "h2{font-size:15px;margin:0 0 4px}p{margin:0 0 12px;color:#9a8d79;font-size:13px}"
        "audio{width:100%}blockquote{color:#9a8d79;font-size:14px;border-left:2px solid #3a3128;"
        "padding-left:12px;margin:0 0 24px}</style>"
        "<h1>Sesli anlatım — aynı metin, üç ses</h1>"
        "<blockquote>Ayıboğan çocuğa yaklaşmıyor. Yaklaşmak bir çocuğu kapatır. "
        "Bunun yerine havayı okuyor. Tepe ıslak, sırılsıklam…</blockquote>" + rows,
        mimetype="text/html; charset=utf-8")


@app.route("/tts-sample/<name>")
def tts_sample_file(name):
    files = {"piper": ("/tmp/piper_sample.wav", "audio/wav"),
             "ahmet": ("/tmp/edge_ahmet.mp3", "audio/mpeg"),
             "emel":  ("/tmp/edge_emel.mp3",  "audio/mpeg")}
    if name not in files:
        return "unknown sample", 404
    path, mime = files[name]
    if not os.path.isfile(path):
        return "no sample", 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype=mime, headers={"Cache-Control": "no-store"})


@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/health")
def health():
    """Server-side integrity probe used by send.py --verify and external monitors.

    Returns the live counts the send-side cares about:
      - alive: always True if the route runs
      - tail_buffer: number of entries currently in the rolling tail
      - tail_file_size: size in bytes of the on-disk session_tail.json
      - text_log: number of entries in the replay log
      - campaign: the active campaign name (empty if none set)
      - clients: connected SSE clients

    No auth required — this is a liveness/monitoring endpoint, no PII or game
    content is exposed.
    """
    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
    except Exception:
        camp = ""
    tail_path = _get_tail_file()
    try:
        tail_size = os.path.getsize(tail_path) if tail_path and os.path.exists(tail_path) else 0
    except OSError:
        tail_size = 0
    with _tail_lock:
        tail_count = len(_tail_buffer)
    with _text_log_lock:
        log_count = len(_text_log)
    with _clients_lock:
        client_count = len(_clients)
    return {
        "alive": True,
        "tail_buffer": tail_count,
        "tail_file_size": tail_size,
        "tail_path": tail_path or "",
        "text_log": log_count,
        "campaign": camp,
        "clients": client_count,
    }, 200


@app.route("/chunk", methods=["POST"])
def chunk():
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}

    # ── Scene image ───────────────────────────────────────────────────────────
    # Carries a URL only; the browser fetches the picture directly, so nothing
    # is downloaded or cached here. Handled before the text gate because an
    # image block legitimately has no body text.
    scene_image = str(data.get("scene_image") or "").strip()
    if scene_image:
        if not scene_image.startswith(("http://", "https://", "/scenes/")):
            return "bad scene_image url", 400
        img_payload: dict = {"scene_image": scene_image[:2000]}
        prompt = str(data.get("prompt") or "").strip()[:300]
        if prompt:
            img_payload["prompt"] = prompt
        img_log: dict = dict(img_payload)
        try:
            _camp_stamp = open(CAMP_FILE, encoding="utf-8").read().strip()
            if _camp_stamp:
                img_log["_camp"] = _camp_stamp
        except Exception:
            pass
        with _text_log_lock:
            _text_log.append(img_log)
        with _tail_lock:
            _tail_buffer.append(img_log)
        _persist_log()
        _persist_tail()
        _broadcast(img_payload)
        return "", 204

    raw = data.get("text", "")
    if not raw:
        return "", 204

    is_action      = bool(data.get("action"))
    is_player      = bool(data.get("player"))
    is_npc         = bool(data.get("npc"))
    is_dice        = bool(data.get("dice"))
    is_tutor       = bool(data.get("tutor"))
    is_inspiration = bool(data.get("inspiration_award"))
    is_milestone_award = bool(data.get("milestone_award"))
    is_milestone_spend = bool(data.get("milestone_spend"))
    is_xp_award    = bool(data.get("xp_award"))

    # ── Milestone award/spend (stack-based reward, distinct from binary Inspiration) ──
    if is_milestone_award or is_milestone_spend:
        name = str(data.get("milestone_award") or data.get("milestone_spend") or "").strip()[:80]
        label = str(data.get("label") or "Milestone").strip()[:40]
        if not name:
            return "", 204
        payload: dict = {
            "milestone_award" if is_milestone_award else "milestone_spend": name,
            "label": label,
            "text": name,
        }
        log_entry: dict = dict(payload)
        if is_milestone_award and data.get("reason"):
            payload["reason"] = str(data["reason"]).strip()[:240]
            log_entry["reason"] = payload["reason"]
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload)
        return "", 204

    # Inspiration and XP awards carry no text from stdin — build synthetic text.
    if is_inspiration:
        name = str(data.get("inspiration_award", "")).strip()[:80]
        if not name:
            return "", 204
        reason = str(data.get("reason", "")).strip()[:240]
        payload: dict = {"inspiration_award": name, "text": name}
        log_entry: dict = {"inspiration_award": name, "text": name}
        if reason:
            payload["reason"] = reason
            log_entry["reason"] = reason
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload)
        # Also update player inspiration state in stats.
        # NOTE: _persist_stats() acquires _stats_lock internally — capture the snapshot
        # inside the lock, then call persist/broadcast OUTSIDE to avoid deadlock.
        stats_snapshot = None
        with _stats_lock:
            players = _current_stats.setdefault("players", [])
            match = next((p for p in players if p.get("name", "").lower() == name.lower()), None)
            if match:
                match["inspiration"] = True
                stats_snapshot = dict(_current_stats)
        if stats_snapshot is not None:
            _persist_stats()
            _broadcast({"stats": stats_snapshot})
        return "", 204

    if is_xp_award:
        xp_data = data.get("xp_award", {})
        if not isinstance(xp_data, dict):
            return "", 204
        payload = {"xp_award": xp_data, "text": xp_data.get("summary", "")}
        log_entry = {"xp_award": xp_data, "text": xp_data.get("summary", "")}
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload)
        return "", 204

    # Player/npc/dice/tutor/action text comes from send.py (no ANSI/chrome) — light clean only.
    # DM narration may come from wrapper.py — full clean.
    cleaned = raw.strip() if (is_action or is_player or is_npc or is_dice or is_tutor) else _clean(raw)
    if not cleaned.strip():
        return "", 204

    payload: dict = {"text": cleaned}

    # Private narration: addressed to one bound character. Delivery is filtered
    # in _broadcast / _tail / the on-connect replay, so the rest of the table
    # never receives the bytes at all.
    private_to = str(data.get("to") or "").strip()[:48]
    if private_to:
        payload["to"] = private_to

    if is_action:
        payload["action"] = data["action"]
    elif is_player:
        payload["player"] = data["player"]
    elif is_npc:
        payload["npc"] = data["npc"]
    elif is_dice:
        payload["dice"] = True
        _vfx = _detect_dice_vfx(cleaned)
        if _vfx:
            payload["vfx"] = _vfx
    elif is_tutor:
        payload["tutor"] = True
    else:
        # Scene detection only on DM narration
        scene = _detect_scene(cleaned)
        if scene:
            payload["scene"] = scene
            if _audio:
                _audio.on_scene_change(scene["name"])
        # SFX scan on all non-player text
        if _audio:
            _audio.on_text(cleaned)

    # Not a block type: narration that names who speaks inside it, so the TTS
    # layer can hand the quoted lines to that character's voice.
    speaker = (data.get("speaker") or "").strip()
    if speaker:
        payload["speaker"] = speaker
    tone = (data.get("tone") or "").strip()
    if tone:
        payload["tone"] = tone

    # Store full typed payload so replay preserves action/player/npc/dice/tutor context
    log_entry: dict = {"text": cleaned}
    if private_to:
        log_entry["to"] = private_to
    if speaker:
        log_entry["speaker"] = speaker
    if tone:
        log_entry["tone"] = tone
    if is_action:
        log_entry["action"] = data["action"]
    elif is_player:
        log_entry["player"] = data["player"]
    elif is_npc:
        log_entry["npc"] = data["npc"]
    elif is_dice:
        log_entry["dice"] = True
    elif is_tutor:
        log_entry["tutor"] = True

    # Stamp campaign on tail entries to prevent bleed when switching campaigns
    try:
        _camp_stamp = open(CAMP_FILE, encoding="utf-8").read().strip()
        if _camp_stamp:
            log_entry["_camp"] = _camp_stamp
    except Exception:
        pass

    with _text_log_lock:
        _text_log.append(log_entry)
    with _tail_lock:
        _tail_buffer.append(log_entry)

    _persist_log()
    _persist_tail()
    _broadcast(payload)
    _lint_observe(log_entry)
    return "", 204


@app.route("/party")
def party():
    """Names the display currently knows, so a sender can check before sending.

    A dice request addressed to a name nobody is bound to does not fail, it
    hangs: the request sits on a screen that never shows it. Checking first is
    the only way that turns into an error the DM can see.
    """
    if not _token_ok():
        return "Forbidden", 403
    with _stats_lock:
        names = [p.get("name", "") for p in _current_stats.get("players", [])]
    return jsonify({"players": [n for n in names if n]}), 200


@app.route("/stats", methods=["POST"])
def stats():
    """Receive character/combat stat updates. Merges players by name, replaces turn_order.

    Pass replace_players=true to replace the entire player list (use on /dnd load to
    prevent stale characters from a previous campaign persisting in the sidebar).
    """
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    if not data:
        return "", 204

    _effect_expire_events: list[dict] = []
    with _stats_lock:
        if "players" in data:
            # replace_players=true wipes the list first — used on campaign load
            if data.get("replace_players"):
                _current_stats["players"] = []
            existing_players: list = _current_stats.setdefault("players", [])
            for incoming in data["players"]:
                name = incoming.get("name")
                if not name:
                    continue
                match = next((p for p in existing_players if p.get("name") == name), None)
                # Keys prefixed with _ are mutation ops, not stored fields
                _MUTATION_KEYS = {
                    "_inventory_add", "_inventory_remove",
                    "_conditions_add", "_conditions_remove",
                    "_slot_use", "_slot_restore",
                    "_use_spend", "_use_restore",
                    "_hd_use", "_hd_restore",
                    "_effect_start", "_effect_end",
                    "_sheet_spells",
                    "_milestone_inc", "_milestone_dec",
                }
                if match:
                    for key, val in incoming.items():
                        if key == "_inventory_add":
                            inv = match.setdefault("sheet", {}).setdefault("inventory", [])
                            if val not in inv:
                                inv.append(val)
                        elif key == "_inventory_remove":
                            sheet = match.get("sheet", {})
                            sheet["inventory"] = [
                                i for i in sheet.get("inventory", [])
                                if i.lower() != str(val).lower()
                            ]
                        elif key == "_conditions_add":
                            conds = match.setdefault("conditions", [])
                            if val not in conds:
                                conds.append(val)
                        elif key == "_conditions_remove":
                            match["conditions"] = [
                                c for c in match.get("conditions", [])
                                if c.lower() != str(val).lower()
                            ]
                        elif key == "_slot_use":
                            slots = match.setdefault("spell_slots", {})
                            lvl = str(val)
                            slot = slots.setdefault(lvl, {"used": 0, "max": 0})
                            _normalize_slot(slot)
                            slot["used"] = min(slot["used"] + 1, slot.get("max", 99))
                        elif key == "_slot_restore":
                            slots = match.setdefault("spell_slots", {})
                            lvl = str(val)
                            slot = slots.setdefault(lvl, {"used": 0, "max": 0})
                            _normalize_slot(slot)
                            slot["used"] = max(slot["used"] - 1, 0)
                        elif key in ("_use_spend", "_use_restore"):
                            # Only moves a counter the character already has.
                            # Declaring one is a plain `uses` field — spending
                            # a feature nobody declared would invent a counter
                            # at zero and then refuse the feature forever.
                            counter = (match.get("uses") or {}).get(str(val))
                            if isinstance(counter, dict):
                                left = int(counter.get("left", 0))
                                counter["left"] = (
                                    max(0, left - 1) if key == "_use_spend"
                                    else min(left + 1, int(counter.get("max", 99))))
                        elif key == "_hd_use":
                            hd = match.setdefault("hit_dice", {"remaining": 0, "max": 0})
                            hd["remaining"] = max(hd.get("remaining", 0) - 1, 0)
                        elif key == "_hd_restore":
                            hd = match.setdefault("hit_dice", {"remaining": 0, "max": 0})
                            hd["remaining"] = min(
                                hd.get("remaining", 0) + int(val),
                                hd.get("max", 99)
                            )
                        elif key == "_effect_start":
                            # val is an effect dict: {name, duration_type, ...}
                            spell_name = val.get("name", "")
                            effects = match.setdefault("effects", [])
                            # Replace any existing effect with the same name
                            match["effects"] = [
                                e for e in effects
                                if e.get("name", "").lower() != spell_name.lower()
                            ]
                            match["effects"].append(val)
                            # Sync concentration field if this is a conc effect
                            if val.get("concentration") and spell_name:
                                match["concentration"] = spell_name
                        elif key == "_effect_end":
                            # val is the spell name string
                            spell_lower = str(val).lower()
                            removed = [
                                e for e in match.get("effects", [])
                                if e.get("name", "").lower() == spell_lower
                            ]
                            match["effects"] = [
                                e for e in match.get("effects", [])
                                if e.get("name", "").lower() != spell_lower
                            ]
                            # If the ended effect was concentration, also clear it
                            if removed and any(e.get("concentration") for e in removed):
                                if match.get("concentration", "").lower() == spell_lower:
                                    match["concentration"] = None
                        elif key == "_sheet_spells":
                            # Patch only the spells sub-key inside sheet
                            sheet = match.setdefault("sheet", {})
                            sheet["spells"] = val
                        elif key == "inspiration" and val is False:
                            match["inspiration"] = False
                        elif key == "_milestone_inc":
                            # Stack-based reward counter — Inspiration variants,
                            # homebrew Hero Coins, Bardic Inspiration tokens, etc.
                            # The label string is the value; per-label cap optional.
                            label = str(val) or "Milestone"
                            ms = match.setdefault("milestones", {})
                            cap = match.get("milestone_caps", {}).get(label, 99)
                            ms[label] = min(ms.get(label, 0) + 1, cap)
                        elif key == "_milestone_dec":
                            label = str(val) or "Milestone"
                            ms = match.setdefault("milestones", {})
                            ms[label] = max(ms.get(label, 0) - 1, 0)
                            if ms.get(label, 0) == 0:
                                ms.pop(label, None)
                        elif isinstance(val, dict) and isinstance(match.get(key), dict):
                            match[key].update(val)
                        else:
                            match[key] = val
                else:
                    # Strip mutation ops — they're meaningless for new players
                    existing_players.append(
                        {k: v for k, v in incoming.items() if k not in _MUTATION_KEYS}
                    )

        # turn_order replaces entirely (None = clear); also ticks round-based effects
        _effect_expire_events: list[dict] = []
        if "turn_order" in data:
            new_to = data["turn_order"]
            _current_stats["turn_order"] = new_to
            # Decrement round-based effects for the actor whose turn just started
            if new_to and isinstance(new_to, dict) and new_to.get("current"):
                actor = new_to["current"].lower()
                for p in _current_stats.get("players", []):
                    if p.get("name", "").lower() != actor:
                        continue
                    kept, expired = [], []
                    for eff in p.get("effects", []):
                        if eff.get("duration_type") == "rounds":
                            eff = dict(eff)  # don't mutate in-place
                            eff["duration_remaining"] = max(0, eff.get("duration_remaining", 1) - 1)
                            if eff["duration_remaining"] <= 0:
                                expired.append(eff)
                            else:
                                kept.append(eff)
                        else:
                            kept.append(eff)
                    p["effects"] = kept
                    for eff in expired:
                        was_conc = eff.get("concentration", False)
                        if was_conc and p.get("concentration", "").lower() == eff["name"].lower():
                            p["concentration"] = None
                        _effect_expire_events.append({
                            "owner": p["name"],
                            "name": eff["name"],
                            "was_concentration": was_conc,
                        })

        # world_time replaces entirely
        if "world_time" in data:
            _current_stats["world_time"] = data["world_time"]

        # factions replaces entirely ([] clears)
        # Validate: default missing standing to "Neutral" and warn so the root
        # cause (DM omitting the field when building JSON from state.md prose)
        # is surfaced in logs without silently showing "—" in the sidebar.
        if "factions" in data:
            validated_factions = []
            for f in (data["factions"] or []):
                if not isinstance(f, dict):
                    continue
                if f.get("name") and not f.get("standing"):
                    print(
                        f"[WARN] faction '{f['name']}' missing standing field — "
                        "defaulting to Neutral. Push with standing: Allied/Friendly/"
                        "Neutral/Suspicious/Hostile to show correct colour.",
                        file=sys.stderr,
                    )
                    f = dict(f)
                    f["standing"] = "Neutral"
                validated_factions.append(f)
            _current_stats["factions"] = validated_factions

        # quests replaces entirely ([] clears)
        if "quests" in data:
            _current_stats["quests"] = data["quests"]

        current = dict(_current_stats)

    # autorun_waiting / autorun_cycle — display-only signals, not stored in stats
    if "autorun_waiting" in data:
        if not data["autorun_waiting"]:
            # Turn resolved — clear stored cycle so reconnecting clients don't see stale pie
            global _autorun_cycle
            with _autorun_cycle_lock:
                _autorun_cycle = None
        _broadcast({"autorun_waiting": bool(data["autorun_waiting"])})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "quests", "replace_players", "sheet", "autorun_cycle")):
            return "", 204

    if "autorun_cycle" in data:
        with _autorun_cycle_lock:
            _autorun_cycle = data["autorun_cycle"]
        _broadcast({"autorun_cycle": data["autorun_cycle"]})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "replace_players", "sheet", "autorun_threshold")):
            return "", 204

    if "autorun_threshold" in data:
        global _autorun_threshold
        val = data["autorun_threshold"]
        _autorun_threshold = int(val) if val is not None else None
        _broadcast({"autorun_threshold": _autorun_threshold})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "replace_players", "sheet")):
            return "", 204

    # Write active campaign name so dm_help.py always reads the current campaign.
    # Also reload the tail buffer from the new campaign's session_tail.json so
    # display replay at /dnd load shows the correct campaign's last session.
    if "campaign" in data:
        try:
            with open(CAMP_FILE, "w", encoding="utf-8") as f:
                f.write(str(data["campaign"]).strip())
            _load_tail()
        except Exception:
            pass
        # Resolve and stash the ruleset for this campaign so the sidebar badge
        # can render. Defaults to '2014' for legacy campaigns predating the
        # ruleset field. Wrapped in try/except so a missing paths import or
        # malformed state.md never breaks the stats endpoint.
        try:
            from paths import campaign_ruleset as _campaign_ruleset
            _rs = _campaign_ruleset(str(data["campaign"]).strip())
            with _stats_lock:
                _current_stats["ruleset"] = _rs
            current = dict(_current_stats)
        except Exception:
            pass

    # Explicit ruleset override (e.g. push_stats.py --ruleset 2024)
    if "ruleset" in data:
        rs_in = str(data.get("ruleset") or "").strip()
        if rs_in in ("2014", "2024"):
            with _stats_lock:
                _current_stats["ruleset"] = rs_in
            current = dict(_current_stats)

    _persist_stats()
    _broadcast({"stats": current})
    # Whose turn it is shows on the board as a ring; a turn change redraws it.
    if "turn_order" in data and _battle_map:
        _broadcast_battle_map()
    # Broadcast any round-based effect expiries after the stats update
    for evt in _effect_expire_events:
        _broadcast({"effect_expired": evt})

    # Update expected player count for staged-input auto-trigger
    global _expected_count
    with _stats_lock:
        players = _current_stats.get("players", [])
    _expected_count = max(1, len(players))

    return "", 204


@app.route("/effects/expire", methods=["POST"])
def effects_expire():
    """Called by browser when a time-based effect countdown reaches zero.
    Removes the effect from stats, clears concentration if applicable,
    and broadcasts effect_expired to all connected clients.
    """
    if not _token_ok():
        return "Forbidden", 403
    data  = request.get_json(silent=True) or {}
    owner = data.get("owner", "").strip()
    name  = data.get("name", "").strip()
    if not owner or not name:
        return "", 400

    expire_evt = None
    with _stats_lock:
        for p in _current_stats.get("players", []):
            if p.get("name", "").lower() != owner.lower():
                continue
            was_conc   = False
            new_effects = []
            for e in p.get("effects", []):
                if e.get("name", "").lower() == name.lower():
                    was_conc = e.get("concentration", False)
                    if was_conc and p.get("concentration", "").lower() == name.lower():
                        p["concentration"] = None
                else:
                    new_effects.append(e)
            p["effects"] = new_effects
            expire_evt = {"owner": p["name"], "name": name, "was_concentration": was_conc}
            break
        current = dict(_current_stats)

    if expire_evt:
        _broadcast({"effect_expired": expire_evt})
    _broadcast({"stats": current})
    _persist_stats()
    return "", 204


@app.route("/audio-toggle", methods=["POST"])
def audio_toggle():
    """Enable/disable ambient or SFX from the browser toggle switches.

    Body: {"ambient": true|false, "sfx": true|false}  (either or both keys)
    Response: {"ambient": bool, "sfx": bool, "available": bool}
    Broadcasts audio_state to all connected browsers so every device syncs.
    """
    data = request.get_json(silent=True) or {}
    if _audio:
        if "sfx" in data:
            _audio.set_sfx(bool(data["sfx"]))
        state = _audio.get_state()
    else:
        state = {"sfx": False, "available": False}
    return state, 200


@app.route("/narration-pref", methods=["POST"])
def narration_pref():
    """Set the narration-length target the DM aims for each turn.

    Body: {"target_words": int}.  0 clears the preference. Persisted to the
    runtime dir as a plain integer; check_input.py reads it and prepends a
    directive to queued player input so the DM honors it that turn — no
    separate file read required on the DM side.
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(_client_ip()):
        return "Rate limited", 429
    data = request.get_json(silent=True) or {}
    try:
        n = int(data.get("target_words", 0))
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(5000, n))
    pref = rt("narration_target")
    try:
        if n:
            with open(pref, "w", encoding="utf-8") as f:
                f.write(str(n))
        elif os.path.exists(pref):
            os.remove(pref)
    except OSError:
        pass
    return {"target_words": n}, 200


@app.route("/roll-pref", methods=["POST"])
def roll_pref():
    """Per-character roll preference. Body: {"character": str, "mode": "auto"|"players"}.

    Persisted to runtime roll_prefs.json; check_input.py surfaces each override as a
    [[<Char> roll mode: …]] directive so the DM honors it for that character,
    overriding the campaign-wide roll_mode in state.md.

    The character name is validated against the active party via _char_ok before
    persistence — otherwise a crafted value could smuggle prompt text into the DM
    through the [[<Char> roll mode: …]] template that check_input.py emits.
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(_client_ip()):
        return "Rate limited", 429
    data = request.get_json(silent=True) or {}
    char = (data.get("character") or "").strip()
    mode = (data.get("mode") or "").strip().lower()
    if not char or mode not in ("auto", "players"):
        return {"ok": False}, 400
    with _stats_lock:
        known = {p["name"] for p in _current_stats.get("players", [])}
    if not _char_ok(char, known):
        return "Forbidden", 403
    pref = rt("roll_prefs.json")
    try:
        prefs = {}
        if os.path.exists(pref):
            with open(pref, encoding="utf-8") as f:
                prefs = json.load(f)
        prefs[char] = mode
        with open(pref, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except (OSError, ValueError):
        pass
    return {"ok": True, "character": char, "mode": mode}, 200


# ─── Narrator voice ───────────────────────────────────────────────────────────
# Voice selection persists per-campaign in state.md → ## Session Flags →
# `tts_voice: <name>`. Read at /index render, written by POST /voice.
#
# Azure voice names are not bare words — `tr-TR-Aydın:MAI-Voice-2` carries
# hyphens, a colon, digits and a dotted-i, so the pattern has to be wider than
# the Gemini-era [A-Za-z]+. \w is Unicode-aware in Python 3, which covers the
# Turkish letters; the explicit class adds the punctuation Azure uses.
_VOICE_PAT = re.compile(r"^\s*tts_voice:\s*([\w:.\-]+)\s*$", re.MULTILINE)


def _active_campaign_name() -> Optional[str]:
    try:
        return open(CAMP_FILE, encoding="utf-8").read().strip() or None
    except OSError:
        return None


def _read_narrator_voice() -> str:
    """Return the active campaign's tts_voice, or the module default."""
    if _tts is None:
        return ""
    name = _active_campaign_name()
    if not name:
        return _tts.DEFAULT_VOICE
    try:
        state = _find_campaign(name) / "state.md"
        if not state.exists():
            return _tts.DEFAULT_VOICE
        text = state.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return _tts.DEFAULT_VOICE
    m = _VOICE_PAT.search(text)
    if not m:
        return _tts.DEFAULT_VOICE
    v = m.group(1).strip()
    return v if v in _tts.VALID_VOICES else _tts.DEFAULT_VOICE


def _write_narrator_voice(voice: str) -> bool:
    """Persist tts_voice to the active campaign's state.md → ## Session Flags."""
    if _tts is None or voice not in _tts.VALID_VOICES:
        return False
    name = _active_campaign_name()
    if not name:
        return False
    try:
        state = _find_campaign(name) / "state.md"
        # utf8io transcodes legacy-GBK state.md losslessly (one-time migration);
        # raises ValueError on anything undecodable, so we never write U+FFFD
        # back over the file from the display UI.
        text = _read_text(state) if state.exists() else ""
    except (OSError, ValueError):
        return False

    new_line = f"tts_voice: {voice}"
    if _VOICE_PAT.search(text):
        text = _VOICE_PAT.sub(new_line, text, count=1)
    else:
        # Append under ## Session Flags. If the section is missing, append at EOF.
        if "## Session Flags" in text:
            # Insert right after the header line. Keep the existing template
            # comment if present, but place the flag immediately under it.
            text = re.sub(
                r"(## Session Flags\n(?:\*\(.*?\)\*\n)?)",
                r"\1" + new_line + "\n",
                text,
                count=1,
            )
        else:
            sep = "" if text.endswith("\n") else "\n"
            text = f"{text}{sep}\n## Session Flags\n{new_line}\n"

    try:
        state.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


# ─── Per-character voices ─────────────────────────────────────────────────────
# The campaign's ses-haritasi.json casts each NPC: a prebuilt voice plus one
# line of acting direction. The narrator sits under "_narrator". Keys starting
# with an underscore are metadata, never speakers.
#
# Only the gemini backend uses this. Azure's tr-TR catalog has six voices and
# no style input, so a cast built for thirty would resolve to nothing.

_VOICE_MAP_CACHE = {"path": None, "mtime": 0.0, "data": {}}

# A quarter second between spans: long enough to hear the voice change, short
# enough that prose and the line it introduces stay one breath.
_SPAN_GAP = b"\x00\x00" * int(24000 * 0.25)


def _read_voice_map() -> dict:
    """Load the active campaign's voice map, re-reading only when it changes."""
    name = _active_campaign_name()
    if not name:
        return {}
    try:
        path = _find_campaign(name) / "ses-haritasi.json"
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return {}
    cache = _VOICE_MAP_CACHE
    if cache["path"] == path and cache["mtime"] == mtime:
        return cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cache.update(path=path, mtime=mtime, data=data)
    return data


_TONE_PAT = re.compile(r"^[a-z_]{1,24}$")


def _narrator_key(tone: str) -> str:
    """Pick the narrator entry for this block's mode.

    The narrator keeps one voice all session; what changes is the pace it is
    told to read at, so a rules-and-XP block does not get the same unhurried
    delivery as a room being described. An unknown mode falls back rather than
    losing the narration.
    """
    tone = (tone or "").strip().lower()
    if not tone or not _TONE_PAT.match(tone):
        return "_narrator"
    key = f"_narrator_{tone}"
    return key if isinstance(_read_voice_map().get(key), dict) else "_narrator"


def _cast_entry(npc: str) -> "tuple[str, str]":
    """Return (voice, style) for a speaker name, or ("", "") if uncast.

    The name has to match what send.py --npc wrote, Turkish letters included.
    A miss is silent on purpose: an unnamed walk-on should still be narrated
    rather than failing the request.
    """
    entry = _read_voice_map().get(npc) if npc else None
    if not isinstance(entry, dict):
        return "", ""
    return (str(entry.get("gemini") or "").strip(),
            str(entry.get("yon") or "").strip())


@app.route("/tts", methods=["POST"])
def tts_synthesize():
    """Synthesize a narrator/NPC block to L16 PCM.

    Body: {"text": str, "voice": str (optional)}
    Response: raw L16 PCM bytes, Content-Type: audio/L16;codec=pcm;rate=24000
    Failures: 503 (no key / module unavailable), 400 (bad input), 502 (upstream)
    """
    if _tts is None:
        return "TTS module unavailable", 503
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(_client_ip()):
        return "Rate limited", 429
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = (data.get("voice") or _tts.DEFAULT_VOICE).strip()
    npc = (data.get("npc") or "").strip()
    speaker = (data.get("speaker") or "").strip()
    tone = (data.get("tone") or "").strip()
    style = ""
    if not text:
        return "empty text", 400
    if len(text) > _tts.MAX_TEXT_CHARS:
        text = text[: _tts.MAX_TEXT_CHARS]
    # An NPC the campaign has cast overrides whatever voice the browser picked;
    # the dropdown is the narrator's, and a speaker's voice is not the viewer's
    # to choose. A DM block keeps the browser's selection and only falls back to
    # the cast's narrator entry when that selection is unusable.
    if _tts.provider() == "gemini":
        cast_voice, style = _cast_entry(npc or _narrator_key(tone))
        if cast_voice and (npc or voice not in _tts.VALID_VOICES):
            voice = cast_voice
    if voice not in _tts.VALID_VOICES:
        voice = _tts.DEFAULT_VOICE
    if _tts.key_source() == "unset":
        return "TTS not configured (see docs/SKILL-tts.md)", 503

    # A narration block that declares a speaker is read by two voices: the prose
    # stays with the narrator, the quoted lines go to that character. Azure has
    # no cast to draw on, so it reads the block whole as before.
    spans = [(None, text)]
    if speaker and not npc and _tts.provider() == "gemini":
        spans = _dialogue.split(text, speaker)

    try:
        chunks = []
        for who, span in spans:
            span_voice, span_style = voice, style
            if who:
                cast_voice, cast_style = _cast_entry(who)
                # An uncast speaker keeps the narrator's voice rather than
                # dropping the line.
                if cast_voice:
                    span_voice, span_style = cast_voice, cast_style
            chunks.append(_tts.synthesize_strict(span, span_voice, style=span_style))
        pcm = _SPAN_GAP.join(chunks)
    except _tts.TtsError as e:
        return f"TTS upstream: {e}", 502
    report = _tts.usage_report()
    return Response(
        pcm,
        mimetype="audio/L16;codec=pcm;rate=24000",
        headers={
            "X-Audio-Chars": str(len(text)),
            # HTTP headers are latin-1 only, and `tr-TR-Aydın:MAI-Voice-2`
            # carries a dotless i. Sending it raw raises UnicodeEncodeError
            # inside the WSGI server *after* the body is queued, which hangs
            # the request instead of failing it. Percent-encode; decode with
            # decodeURIComponent() on the client.
            "X-Audio-Voice": urllib.parse.quote(voice, safe=""),
            # Running position against the free quota, so a client can surface
            # the month's burn without a second round trip.
            "X-Quota-Used-Pct": str(report["free_tier_used_pct"]),
            "Cache-Control": "no-store",
        },
    )


@app.route("/tts-usage")
def tts_usage():
    """This month's synthesis totals and where they sit against the free quota.

    Azure exposes no character-count metric for Speech resources, so this local
    tally is the only running answer to "how much of the 500k is left".
    """
    if _tts is None:
        return jsonify({"error": "TTS module unavailable"}), 503
    if not _token_ok():
        return "Forbidden", 403
    return jsonify(_tts.usage_report()), 200


@app.route("/voice", methods=["POST"])
def tts_voice():
    """Persist narrator voice selection for the active campaign.

    Body: {"voice": str}
    Response: {"voice": str, "persisted": bool}
    """
    if _tts is None:
        return jsonify({"voice": "", "persisted": False}), 503
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    voice = (data.get("voice") or "").strip()
    if voice not in _tts.VALID_VOICES:
        return jsonify({"error": "invalid voice"}), 400
    ok = _write_narrator_voice(voice)
    return jsonify({"voice": voice, "persisted": ok}), 200


@app.route("/audio/sfx/<name>")
def audio_sfx(name):
    """Serve a synthesized SFX WAV for the given effect name."""
    if not _audio:
        return "Audio not available", 503
    wav = _audio.get_sfx_wav(name)
    if wav is None:
        return "Not found", 404
    return Response(wav, mimetype="audio/wav",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/clear", methods=["POST"])
def clear():
    """Wipe text log AND stats, broadcast clear to all connected browsers.

    Called on /dnd new (fresh campaign). Ensures sidebar shows no stale characters.
    """
    if not _token_ok():
        return "Forbidden", 403
    global _scene_buffer, _current_stats
    with _text_log_lock:
        _text_log.clear()
    with _stats_lock:
        _current_stats = {}
    _scene_buffer = []
    for path in (LOG_FILE, STATS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _broadcast({"clear": True})
    return "", 204


@app.route("/help-request", methods=["POST"])
def help_request():
    """Spawn dm_help.py to generate and send an on-demand DM hint.

    Protected by an O_EXCL lock file — concurrent requests return 409
    so multiple players clicking the button never duplicates execution.
    Lock is released by dm_help.py in its finally block.
    """
    if not _token_ok():
        return "Forbidden", 403

    # Atomic lock: O_EXCL fails if file already exists — no race condition
    try:
        fd = os.open(HELP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return "Already running", 409

    # Read active campaign name
    try:
        campaign = open(CAMP_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        os.unlink(HELP_LOCK)
        return "No active campaign", 400

    if not campaign:
        os.unlink(HELP_LOCK)
        return "No active campaign", 400

    dm_help_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_help.py")
    subprocess.Popen(
        [sys.executable, dm_help_py, "--campaign", campaign],
        close_fds=True,
        start_new_session=True,
    )
    return "", 202


@app.route("/player-input", methods=["POST"])
def player_input():
    """Queue a player action submitted from the display companion.

    Body: {"character": "Mira", "text": "I draw my rapier", "hold": false}
    Broadcasts pending_input event to all connected browsers.
    """
    if not _token_ok():
        return "Forbidden", 403

    import time
    data = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", "Party"))[:50]
    text = str(data.get("text", ""))[:500]
    hold = bool(data.get("hold", False))

    # Strip shell metacharacters — input is player dialogue/action, not commands
    text = re.sub(r"[`\\$]", "", text).strip()
    if not text:
        return "empty", 400

    entry = {
        "character": character,
        "text": text,
        "hold": hold,
        "timestamp": time.time(),
    }

    with _input_lock:
        _input_queue.append(entry)
        current = list(_input_queue)

    _persist_input_queue()
    _broadcast({"pending_input": current})
    return "", 204


@app.route("/player-input/dice", methods=["POST"])
def player_dice():
    """Server-side dice roll submitted from a player's phone.

    Body: {"character": "Piper", "spec": "1d20", "modifier": 5,
           "advantage": "normal" | "advantage" | "disadvantage",
           "label": "Stealth check"  (optional)}

    Rolls server-side (secrets.randbelow → uniform, non-spoofable), broadcasts
    a dice-typed entry on the feed, and returns the result so the phone can
    finish its slot-machine animation on the real value.
    """
    if not _token_ok():
        return "Forbidden", 403

    data = request.get_json(force=True, silent=True) or {}
    character = re.sub(r"[`\\$]", "", str(data.get("character", "Player"))[:50]).strip() or "Player"
    spec      = str(data.get("spec", "1d20")).strip().lower()
    modifier  = int(data.get("modifier", 0) or 0)
    adv       = str(data.get("advantage", "normal")).strip().lower()
    label     = re.sub(r"[`\\$]", "", str(data.get("label", ""))[:60]).strip()
    req_id    = str(data.get("request_id", "")).strip()[:24]

    # A prescribed roll's die is the server's, not the pad's. The pad locks
    # itself to the last die it was told to roll and a stale page can submit
    # that one under the new label — which is how a Tactical Mind d10 went out
    # as a d20, correct-looking in the log and wrong at the table. The request
    # already says which die was asked for, so no client is trusted to agree.
    corrected_from = ""
    if req_id:
        with _dice_pending_lock:
            _entry = _dice_pending.get(req_id)
            _want = (_entry or {}).get("meta", {}).get("spec", "")
        if _want and _want != spec:
            corrected_from, spec = spec, _want

    m = re.fullmatch(r"(\d{1,2})d(\d{1,3})", spec)
    if not m:
        return jsonify({"error": "bad spec"}), 400
    n_dice, n_sides = int(m.group(1)), int(m.group(2))
    if not (1 <= n_dice <= 20 and 2 <= n_sides <= 100):
        return jsonify({"error": "out of range"}), 400
    modifier = max(-100, min(100, modifier))

    def _roll_once() -> list[int]:
        return [secrets.randbelow(n_sides) + 1 for _ in range(n_dice)]

    if adv in ("advantage", "disadvantage") and spec == "1d20":
        r1, r2 = _roll_once(), _roll_once()
        chosen = max(r1[0], r2[0]) if adv == "advantage" else min(r1[0], r2[0])
        rolls  = [chosen]
        kept   = [chosen]
        both   = [r1[0], r2[0]]
    else:
        rolls = _roll_once()
        kept  = rolls
        both  = None

    subtotal = sum(kept)
    total    = subtotal + modifier
    mod_str  = (f"+{modifier}" if modifier > 0 else (str(modifier) if modifier < 0 else ""))
    breakdown = f"[{', '.join(str(r) for r in (both or rolls))}]"
    if both is not None:
        breakdown += f" → keep {kept[0]} ({adv})"
    if modifier:
        breakdown += f" {mod_str}"
    suffix = f" — {label}" if label else ""
    if corrected_from:
        suffix += f" (istenen zar {spec}; ekran {corrected_from} göndermişti)"
    text   = f"{character} rolls {spec}{mod_str}: {breakdown} = {total}{suffix}"

    payload   = {"text": text, "dice": True}
    log_entry = {"text": text, "dice": True}
    try:
        _camp_stamp = open(CAMP_FILE, encoding="utf-8").read().strip()
        if _camp_stamp:
            log_entry["_camp"] = _camp_stamp
    except Exception:
        pass

    with _text_log_lock:
        _text_log.append(log_entry)
    with _tail_lock:
        _tail_buffer.append(log_entry)
    _persist_log()
    _persist_tail()
    _broadcast(payload)

    # Correlate against any pending DM request. Case-insensitive match on the
    # character name — drop them from the request's expected-rollers set.
    pending_changed = False
    window_meta = None
    if req_id:
        with _dice_pending_lock:
            entry = _dice_pending.get(req_id)
            if entry is not None:
                ci = character.lower()
                matched = next((c for c in entry["chars"] if c.lower() == ci), None)
                if matched is not None:
                    entry["chars"].discard(matched)
                    pending_changed = True
                    window_meta = dict(entry["meta"])
                    if not entry["chars"]:
                        _dice_pending.pop(req_id, None)
                        if _lint() is not None:
                            _lint().note_resolved(req_id)
    if pending_changed:
        _broadcast({"dice_pending": _dice_pending_snapshot()})

    # A bonus die is not a result, it is an addend, and a reroll replaces the
    # number the DM already saw. Either way the table needs one authoritative
    # line — and so does the DM, who under this flow narrated only the attempt.
    outcome_for = (window_meta or {}).get("outcome_for")
    if outcome_for:
        base = int(outcome_for.get("total", 0))
        dc_b = outcome_for.get("dc")
        who_o = outcome_for.get("roller", character)
        feat = outcome_for.get("feature", "ek zar")
        if outcome_for.get("mode") == "yeniden":
            final, sum_text = total, f"{base} yerine {total}"
        else:
            final, sum_text = base + total, f"{base} + {total} = {base + total}"
        verdict = ""
        if isinstance(dc_b, int):
            verdict = f" vs DC {dc_b} — {'geçti' if final >= dc_b else 'yine kaldı'}"
        _resolve_outcome(f"{who_o} — {feat}: {sum_text}{verdict}")

    # The roll is on the feed and the phone has its number; now give the player
    # the seconds the rules already give them. Off the request thread, so the
    # slot-machine animation is never waiting on a judgment.
    if window_meta and not window_meta.get("no_window"):
        threading.Thread(
            target=_open_response_window,
            args=(character, window_meta, total, req_id), daemon=True).start()

    return jsonify({
        "character": character,
        "spec": spec,
        "modifier": modifier,
        "advantage": adv,
        "rolls": rolls,
        "kept": kept,
        "both": both,
        "subtotal": subtotal,
        "total": total,
        "text": text,
        "request_id": req_id or None,
    }), 200


def _issue_dice_request(chars: list, spec: str, modifier: int, adv: str, label: str,
                        dc_val: "int | None", no_window: bool = False,
                        bonus_for: "dict | None" = None) -> "tuple[str, list]":
    """Register a dice request and put it on the wire.

    Split out of the endpoint because the response window issues rerolls and
    bonus dice itself, and a window's follow-up has to reach the phones by the
    same path the DM's own request does — same pending entry, same payload,
    same replay on reconnect.
    """
    request_id = secrets.token_hex(6)

    # Only register pending entries for explicit named targets. "any" is fire-and-forget.
    trackable = [c for c in chars if c.lower() != "any"]
    if trackable:
        with _dice_pending_lock:
            _dice_pending[request_id] = {
                "chars": set(trackable),
                "meta": {"spec": spec, "modifier": modifier, "advantage": adv, "label": label,
                         "dc": dc_val,
                         # A roll issued *by* a response window does not get a
                         # window of its own: a reroll the player already paid
                         # for is the answer, not a new question.
                         "no_window": bool(no_window),
                         # Set when this roll finishes an earlier one: a bonus
                         # die to add, or a reroll that replaces it. Carries
                         # what it resolves, because a d10 on its own line
                         # answers nothing.
                         "outcome_for": bonus_for},
                "started_at": _time.time(),
            }
        _broadcast({"dice_pending": _dice_pending_snapshot()})
        linter = _lint()
        if linter is not None:
            linter.note_request(request_id, trackable, dc_val, label)

    # Targets with no live phone bound → the main display should roll on-screen.
    onscreen_targets = [c for c in chars if c.lower() != "any" and not _phone_present(c)]
    _broadcast({
        "dice_request": {
            "request_id": request_id,
            "characters": chars,
            "character": chars[0] if len(chars) == 1 else "any",   # legacy single-target field
            "onscreen_targets": onscreen_targets,
            "spec": spec,
            "modifier": modifier,
            "advantage": adv,
            "label": label,
            "dc": dc_val,
        }
    })
    return request_id, trackable


@app.route("/dice-request", methods=["POST"])
def dice_request():
    """DM-initiated dice request — broadcast to player phones (no persistence).

    Body: {"character": "Piper" | "any",
           "spec": "1d20", "modifier": 5,
           "advantage": "normal" | "advantage" | "disadvantage",
           "label": "Stealth check"  (optional),
           "dc": 15  (optional, informational)}

    Phones bound to ?character=<name> match case-insensitively. "any" / ""
    targets every phone. No state stored — late-joining phones will not see
    requests issued before they connected.
    """
    if not _token_ok():
        return "Forbidden", 403

    import time
    data = request.get_json(force=True, silent=True) or {}
    raw_char  = data.get("characters") if "characters" in data else data.get("character", "any")
    if isinstance(raw_char, list):
        chars = [str(c).strip() for c in raw_char if str(c).strip()]
    else:
        chars = [c.strip() for c in re.sub(r"[`\\$]", "", str(raw_char))[:200].split(",") if c.strip()]
    if not chars:
        chars = ["any"]

    spec      = str(data.get("spec", "1d20")).strip().lower()
    modifier  = int(data.get("modifier", 0) or 0)
    adv       = str(data.get("advantage", "normal")).strip().lower()
    label     = re.sub(r"[`\\$]", "", str(data.get("label", ""))[:60]).strip()
    dc        = data.get("dc")

    if not re.fullmatch(r"\d{1,2}d\d{1,3}", spec):
        return jsonify({"error": "bad spec"}), 400
    if adv not in ("normal", "advantage", "disadvantage"):
        adv = "normal"
    modifier = max(-100, min(100, modifier))
    dc_val   = int(dc) if isinstance(dc, (int, float)) else None

    request_id, trackable = _issue_dice_request(
        chars, spec, modifier, adv, label, dc_val, bool(data.get("no_window")))
    return jsonify({
        "request_id": request_id,
        "pending": sorted(trackable),
        "complete": not trackable,
    }), 200


@app.route("/dice-request/<request_id>", methods=["GET"])
def dice_request_status(request_id):
    """Poll a dice request's completion state.

    Returns 200 with {complete, pending, label, started_at}. A request that
    never existed (or has already fully drained) reports complete=True with
    an empty pending list — send.py --wait treats both identically.
    """
    if not _token_ok():
        return "Forbidden", 403
    with _dice_pending_lock:
        entry = _dice_pending.get(request_id)
        if entry is None or not entry["chars"]:
            return jsonify({"complete": True, "pending": []}), 200
        return jsonify({
            "complete": False,
            "pending": sorted(entry["chars"]),
            "label": entry["meta"].get("label", ""),
            "started_at": entry["started_at"],
        }), 200


@app.route("/dice-request/<request_id>", methods=["DELETE"])
def dice_request_cancel(request_id):
    """Cancel a pending dice request (DM gave up waiting / moved on)."""
    if not _token_ok():
        return "Forbidden", 403
    with _dice_pending_lock:
        _dice_pending.pop(request_id, None)
    if _lint() is not None:
        _lint().note_resolved(request_id)
    _broadcast({"dice_pending": _dice_pending_snapshot(), "dice_request_cancelled": request_id})
    return "", 204


def _spend_inspiration(name: str) -> None:
    """Clear the Heroic Inspiration flag the display keeps for a character.

    The window offered it because this counter said they were holding it, so
    the same counter is what has to come down when they spend it — otherwise
    the next failed roll offers a reroll they no longer have.
    """
    snapshot = None
    with _stats_lock:
        match = next((p for p in _current_stats.get("players", [])
                      if p.get("name", "").lower() == name.lower()), None)
        if match and match.get("inspiration"):
            match["inspiration"] = False
            snapshot = dict(_current_stats)
    if snapshot is not None:
        _persist_stats()
        _broadcast({"stats": snapshot})


@app.route("/response-window/<window_id>/spend", methods=["POST"])
def response_window_spend(window_id):
    """A player spends something on the roll that just landed.

    Body: {"offer_id": "...", "character": "Dilaver"}

    Closes the window, announces the spend on the feed so the DM narrates
    against it, drops the resource the display owns, and issues whatever roll
    the feature calls for.
    """
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(force=True, silent=True) or {}
    offer_id = str(data.get("offer_id", "")).strip()[:120]
    who      = str(data.get("character", "")).strip()[:60]

    with _resp_lock:
        window = _resp_windows.get(window_id)
        offer = next((o for o in window["offers"] if o["id"] == offer_id), None) if window else None
    if window is None:
        return jsonify({"error": "window closed"}), 409
    if offer is None:
        return jsonify({"error": "unknown offer"}), 404
    # The offer belongs to one character's sheet; another phone cannot spend it.
    if who and who.lower() != offer["character"].lower():
        return jsonify({"error": "not your offer"}), 403
    if _time.time() > window["expires_at"]:
        _close_response_window(window_id, "timeout")
        return jsonify({"error": "too late"}), 409

    if _close_response_window(window_id, "spent", note=f"{offer['character']}: {offer['feature']}") is None:
        return jsonify({"error": "window closed"}), 409

    _feed_line(f"{offer['character']} — {offer['feature']} kullanıyor "
               f"({window['roller']}, {window['total']} vs DC {window['dc']}).")
    if offer.get("kaynak") == "heroic_inspiration":
        _spend_inspiration(offer["character"])
    elif offer.get("kaynak") == "sinirli_kullanim":
        _spend_limited_use(offer["feature"], offer["character"])
    if offer.get("konsantrasyon"):
        _switch_concentration(offer["character"], offer["feature"])

    follow = _jev_window.follow_up(offer.get("etki", ""), window["spec"],
                                   window["modifier"], window["advantage"]) if _jev_window else None
    issued = None
    if follow:
        # A reroll replaces the roller's own d20; an extra die is rolled by
        # whoever owns the feature, because it is their die.
        target = window["roller"] if follow["kind"] == "yeniden" else offer["character"]
        label = (f"{window['label']} (yeniden)" if follow["kind"] == "yeniden"
                 else f"{offer['feature']} — ek zar")
        issued, _ = _issue_dice_request(
            [target], follow["spec"], follow["modifier"], follow["advantage"],
            label[:60], window["dc"] if follow["kind"] == "yeniden" else None,
            no_window=True,
            # Both kinds resolve the original roll — one replaces its number,
            # the other adds to it — so both carry what they are finishing.
            bonus_for={"mode": follow["kind"], "total": window["total"],
                       "dc": window["dc"], "roller": window["roller"],
                       "feature": offer["feature"]})
    return jsonify({"ok": True, "follow_up_request": issued}), 200


@app.route("/response-window/<window_id>/pass", methods=["POST"])
def response_window_pass(window_id):
    """Nobody is spending — close the window now instead of waiting it out.

    Only the player who rolled can give the seconds back (the DM screen, which
    binds no character, can too). An ally holding an offer must not be able to
    end someone else's decision early.
    """
    if not _token_ok():
        return "Forbidden", 403
    who = str((request.get_json(force=True, silent=True) or {}).get("character", "")).strip()
    with _resp_lock:
        window = _resp_windows.get(window_id)
    if window is None:
        return jsonify({"ok": False}), 409
    if who and who.lower() != window["roller"].lower():
        return jsonify({"error": "not your window"}), 403
    closed = _close_response_window(window_id, "passed")
    return jsonify({"ok": closed is not None}), 200


def _fold_name(name: str) -> str:
    """Fold a character name to a comparable ASCII key.

    Sheet files are saved slugged and lowercase (`ayibogan.md`) while the display
    addresses characters by their real name (`Ayıboğan`). A plain ASCII allowlist
    turns "Ayıboğan" into "Ayboan" and matches nothing, so map the letters that
    carry diacritics to their base form first — Turkish included, where ı and İ
    do not fold the way str.lower() assumes.
    """
    table = str.maketrans({
        "ı": "i", "İ": "i", "ğ": "g", "Ğ": "g", "ş": "s", "Ş": "s",
        "ö": "o", "Ö": "o", "ü": "u", "Ü": "u", "ç": "c", "Ç": "c",
    })
    folded = unicodedata.normalize("NFKD", name.translate(table))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _resolve_sheet_path(dirpath: str, character: str) -> "str | None":
    """Find the sheet file for `character` in `dirpath`, ignoring case and accents.

    Matching against an actual directory listing (rather than building a path
    from user input) also keeps the traversal guarantee the caller relies on.
    """
    want = _fold_name(character)
    if not want or not os.path.isdir(dirpath):
        return None
    for entry in sorted(os.listdir(dirpath)):
        if not entry.endswith(".md"):
            continue
        if _fold_name(entry[:-3]) == want:
            return os.path.join(dirpath, entry)
    return None


@app.route("/character/<character>", methods=["GET"])
def get_character_sheet(character):
    """Return the markdown content of a PC sheet for the active campaign.

    Used by the phone's Character tab. Resolves the active campaign from
    CAMP_FILE, then reads:
        <DND_CAMPAIGN_ROOT>/campaigns/<campaign>/characters/<character>.md

    Falls back to the global roster at ~/.claude/dnd/characters/<character>.md
    if the campaign-side file is missing — useful when the character was just
    imported but not yet replicated.

    Returns text/markdown so the phone can render in JS without server-side
    dependencies (no `markdown` lib required).
    """
    if not _token_ok():
        return "Forbidden", 403

    safe = character.strip()[:60]
    if not _fold_name(safe):
        return "Bad character name", 400

    try:
        camp = open(CAMP_FILE, encoding="utf-8").read().strip()
    except Exception:
        camp = ""
    # Sanitise the campaign name with the same allowlist + length cap as the
    # character argument. CAMP_FILE is writable by anyone inside the LAN+token
    # trust boundary (via push_stats.py --set-campaign), so a malicious value
    # here could pivot to arbitrary `<name>.md` reads via os.path.join.
    camp = re.sub(r"[^A-Za-z0-9_-]", "", camp)[:50]

    root = os.environ.get("DND_CAMPAIGN_ROOT", os.path.expanduser("~/.claude/dnd"))
    search_dirs = []
    if camp:
        search_dirs.append(os.path.join(root, "campaigns", camp, "characters"))
    search_dirs.append(os.path.expanduser("~/.claude/dnd/characters"))

    candidates = [p for p in (_resolve_sheet_path(d, safe) for d in search_dirs) if p]

    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    body = f.read()
            except Exception as e:
                return f"Read error: {e}", 500
            return Response(body, mimetype="text/markdown; charset=utf-8")

    return f"No sheet found for '{safe}' in campaign '{camp}'", 404


@app.route("/device/approve", methods=["POST"])
def device_approve():
    """DM approves a pending device. Body: {"id": "<device_id>"}"""
    if not _token_ok():
        return "Forbidden", 403
    device_id = str((request.get_json(force=True, silent=True) or {}).get("id", ""))
    with _devices_lock:
        _pending_devices.pop(device_id, None)
        _approved_devices.add(device_id)
    _persist_approved_devices()
    _persist_pending_devices()
    _broadcast({"device_approved": device_id})
    return "", 204


@app.route("/device/deny", methods=["POST"])
def device_deny():
    """DM denies a pending device. Body: {"id": "<device_id>"}"""
    if not _token_ok():
        return "Forbidden", 403
    device_id = str((request.get_json(force=True, silent=True) or {}).get("id", ""))
    with _devices_lock:
        _pending_devices.pop(device_id, None)
        _denied_devices.add(device_id)
    _persist_pending_devices()
    _broadcast({"device_denied": device_id})
    return "", 204


@app.route("/player-input/stage", methods=["POST"])
def stage_input():
    """Stage a player action for review. Broadcasts staged_inputs to all displays.

    Body: {"character": "Mira", "text": "draws her rapier"}
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(_client_ip()):
        return "Too Many Requests", 429

    device_id = request.headers.get("X-DND-Device", "")
    status    = _device_ok(device_id, _client_ip())
    if status == "denied":
        return "Forbidden", 403
    if status == "pending":
        return jsonify({"status": "pending"}), 202

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    text      = _sanitize_input(str(data.get("text", "")))

    if not character or not text:
        return "Bad Request", 400

    with _stats_lock:
        known = {p["name"] for p in _current_stats.get("players", [])}
    if not _char_ok(character, known):
        return "Forbidden", 403

    # In solo mode (1 expected player), skip the manual Ready step and auto-trigger.
    solo = (_expected_count == 1)

    with _staged_lock:
        _staged[character] = {
            "text":      text,
            "ready":     solo,
            "timestamp": _time.time(),
        }
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})

    if solo:
        _check_auto_trigger()

    return "", 204


@app.route("/player-input/ready", methods=["POST"])
def ready_input():
    """Toggle the ready flag for a staged character.

    Body: {"character": "Mira", "ready": true}
    Triggers auto-fire when all expected players are ready.
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(_client_ip()):
        return "Too Many Requests", 429

    device_id = request.headers.get("X-DND-Device", "")
    status    = _device_ok(device_id, _client_ip())
    if status == "denied":
        return "Forbidden", 403
    if status == "pending":
        return jsonify({"status": "pending"}), 202

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    ready     = bool(data.get("ready", True))

    with _staged_lock:
        if character not in _staged:
            return "Not Found", 404
        _staged[character]["ready"] = ready
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})

    if ready:
        _check_auto_trigger()

    return "", 204


@app.route("/player-input/unstage", methods=["POST"])
def unstage_input():
    """Remove a character's staged action (e.g. player wants to edit it).

    Body: {"character": "Mira"}
    """
    if not _token_ok():
        return "Forbidden", 403

    device_id = request.headers.get("X-DND-Device", "")
    if _device_ok(device_id, _client_ip()) != "approved":
        return "Forbidden", 403

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()

    with _staged_lock:
        _staged.pop(character, None)
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})
    return "", 204


@app.route("/player-input/skip", methods=["POST"])
def skip_input():
    """Skip a character's turn — stages a 'skips their turn' entry marked ready.

    Counts toward the auto-trigger threshold and fires auto-trigger if threshold met.
    Body: {"character": "Mira"}
    """
    if not _token_ok():
        return "Forbidden", 403

    device_id = request.headers.get("X-DND-Device", "")
    if _device_ok(device_id, _client_ip()) != "approved":
        return "Forbidden", 403

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    if not character:
        return "Bad Request", 400

    with _stats_lock:
        known = {p["name"] for p in _current_stats.get("players", [])}
    if not _char_ok(character, known):
        return "Forbidden", 403

    with _staged_lock:
        _staged[character] = {
            "text":      "skips their turn",
            "ready":     True,
            "timestamp": _time.time(),
        }
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})
    _check_auto_trigger()
    return "", 204


@app.route("/queue/consumed", methods=["POST"])
def queue_consumed():
    """Called by wrapper.py after it injects .input_queue into the PTY.

    Clears the server-side queue_status and broadcasts to all clients so
    the 'Queued — fires on DM Enter' indicator disappears on every display.
    Token required (called from localhost by the wrapper, but checked for
    consistency).
    """
    if not _token_ok():
        return "Forbidden", 403
    with _queue_status_lock:
        _queue_status.clear()
    _broadcast({"queue_status": [], "dm_processing": True})
    return "", 204


@app.route("/player-input/submit-now", methods=["POST"])
def submit_now():
    """Promote .input_queue → .input_trigger for immediate injection.

    Called by the DM or Claude when they want to process queued player actions
    right now rather than waiting for the DM's next CLI Enter press.
    Token required (DM-only action).
    """
    if not _token_ok():
        return "Forbidden", 403
    try:
        content = open(QUEUE_FILE, encoding="utf-8").read()
        os.unlink(QUEUE_FILE)
    except FileNotFoundError:
        return "No queue", 204
    except Exception:
        return "Error", 500
    try:
        with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        return "Error", 500
    return "", 204


@app.route("/player-input/drain", methods=["POST"])
def drain_player_input():
    """Read and clear the player input queue. Called by check_input.py at turn start.

    Returns the drained entries as JSON, then broadcasts pending_input: [] to
    clear the indicator on all connected displays.
    """
    if not _token_ok():
        return "Forbidden", 403

    with _input_lock:
        drained = list(_input_queue)
        _input_queue.clear()

    _persist_input_queue()
    _broadcast({"pending_input": []})
    return jsonify(drained), 200


def _initial_payloads(emit, char: str = "") -> None:
    """Push the on-connect snapshot (scene, replay, stats, pending rolls, …).

    Shared by the SSE stream and the /snapshot polling fallback: a client that
    cannot receive the stream would otherwise start with an empty sidebar, no
    character list on the input panel, and no pending dice request.
    """
    _emit = emit
    # Send the current scene immediately on connect so the browser
    # starts with the right background even mid-session.
    initial_scene = SCENES[_current_scene_name] | {"name": _current_scene_name}
    _emit({"scene": initial_scene})

    # Replay recent entries so late-connecting / reconnecting browsers catch up.
    # _text_log is the durable session record (maxlen=2000); replay only the
    # last 200 chunks — the browser renders this batch on join, not the whole
    # log, so a late joiner isn't shown sessions 1..N rendered at them.
    with _text_log_lock:
        recent = list(_text_log)[-200:]
    recent = [e for e in recent if _visible_to(e, char)]
    if recent:
        _emit({"replay_batch": recent})

    # Send the pinned map so a late joiner or a refresh keeps it on screen.
    with _minimap_lock:
        if _minimap:
            _emit({"minimap": dict(_minimap)})

    # And the board, if a fight is on. The DM-only view goes only to a viewer
    # that binds no character — the same rule _visible_to applies live.
    if _battle_map:
        for payload in _battle_map_payloads():
            if _visible_to(payload, char):
                _emit(payload)

    # Send current stats so the sidebar is populated immediately on (re)connect.
    with _stats_lock:
        if _current_stats:
            _emit({"stats": dict(_current_stats)})

    # Send current input queue so the pending indicator is accurate on reconnect.
    with _input_lock:
        if _input_queue:
            _emit({"pending_input": list(_input_queue)})

    # Send current staged inputs so the panel reflects live state on reconnect.
    with _staged_lock:
        if _staged:
            _emit({"staged_inputs": _staged_snapshot()})

    # Send current queue status so the 'Queued' indicator survives page reload.
    with _queue_status_lock:
        if _queue_status:
            _emit({"queue_status": list(_queue_status)})

    # Send current pending dice requests so the "Waiting on…" badge survives reload.
    snap = _dice_pending_snapshot()
    if snap:
        _emit({"dice_pending": snap})

    # Replay every active dice_request so phones that connected *after* a DM
    # broadcast still pre-fill their pad and store the request_id. Without this,
    # a late-joining or reloaded phone rolls without a request_id, the roll logs
    # but the pending set never drains, and the "Waiting on…" banner gets stuck.
    with _dice_pending_lock:
        active = [(rid, dict(e["meta"]), sorted(e["chars"])) for rid, e in _dice_pending.items() if e["chars"]]
    for rid, meta, chars in active:
        _emit({"dice_request": {
            "request_id": rid,
            "characters": chars,
            "character": chars[0] if len(chars) == 1 else "any",
            "onscreen_targets": [c for c in chars if c.lower() != "any" and not _phone_present(c)],
            "spec": meta.get("spec", "1d20"),
            "modifier": meta.get("modifier", 0),
            "advantage": meta.get("advantage", "normal"),
            "label": meta.get("label", ""),
            "dc": meta.get("dc"),
        }})

    # Replay any open response window. A phone that reloaded during the
    # countdown has to get its buttons back — the window is seconds long and
    # there is no second chance at it.
    for w in _resp_snapshot():
        _emit({"response_window": w})

    # Replay autorun cycle so reconnecting clients resume the countdown from correct elapsed position.
    with _autorun_cycle_lock:
        if _autorun_cycle:
            _emit({"autorun_cycle": dict(_autorun_cycle)})

    # Replay threshold so the ready counter reflects the correct target on reconnect.
    if _autorun_threshold is not None:
        _emit({"autorun_threshold": _autorun_threshold})

    # Send any pending device approval requests so the DM sees them on reconnect.
    with _devices_lock:
        for dev in list(_pending_devices.values()):
            _emit({"device_request": {"id": dev["id"], "ip": dev["ip"]}})



@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=256)
    with _clients_lock:
        _clients.append(q)
        # Register this client's bound character (phones pass ?character=/?char=);
        # the main display passes neither. Drives dice-request phone-vs-screen routing.
        _ch = (request.args.get("character") or request.args.get("char") or "").strip().lower()[:48]
        if _ch:
            _client_chars[q] = _ch

    _initial_payloads(q.put_nowait, _ch)

    def generate():
        try:
            # Prime the stream. A CDN (Cloudflare Tunnel) will hold a response
            # in its compression buffer until enough bytes arrive, which stalls
            # an idle SSE connection forever; 2 KB of comment padding pushes the
            # headers and the first flush through immediately.
            yield ":" + (" " * 2048) + "\n\n"
            # A real event (not a comment) so the browser can tell a live
            # stream from one a proxy is holding open but buffering.
            yield "data: " + json.dumps({"sse_alive": True}) + "\n\n"
            while True:
                try:
                    payload = q.get(timeout=5)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"   # prevent proxy timeout
        except GeneratorExit:
            with _clients_lock:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass
                _client_chars.pop(q, None)

    resp = Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            # no-transform stops Cloudflare compressing (and therefore buffering)
            # the stream; X-Accel-Buffering covers nginx, which Cloudflare strips.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
    # Transfer-Encoding and Connection are hop-by-hop: the WSGI server owns them.
    # Setting them by hand made Cloudflare Tunnel reject /stream with a 502, so
    # they are only forced when the stream is served straight onto the LAN —
    # which is where the original problem lived (eero mesh buffering the stream
    # because Werkzeug emitted both keep-alive and close).
    if _LAN_MODE:
        resp.headers["Connection"] = "keep-alive"
    return resp


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Wire audio SFX broadcast now that _broadcast is defined
    if _audio:
        _audio.set_broadcast(_broadcast)

    host = "0.0.0.0" if _LAN_MODE else "localhost"
    # TLS — only enabled when --tls is explicitly passed; HTTP is the default.
    # Certs are runtime state (persist across plugin updates) → rt(); .scheme is a
    # launch-time marker read by simple shell commands → stays in the code dir.
    _display_dir = os.path.dirname(os.path.abspath(__file__))
    _cert = rt("cert.pem")
    _key  = rt("key.pem")
    ssl_ctx = (_cert, _key) if (_TLS_MODE and os.path.exists(_cert) and os.path.exists(_key)) else None
    scheme  = "https" if ssl_ctx else "http"

    # Write .scheme so push_stats.py / send.py / autorun_wait.py know which to use
    try:
        with open(os.path.join(_display_dir, ".scheme"), "w", encoding="utf-8") as _sf:
            _sf.write(scheme)
    except OSError:
        pass

    if _LAN_MODE:
        print(f"DnD DM Display — LAN mode (0.0.0.0:5001) [{scheme.upper()}]")
        print(f"  Local:  {scheme}://localhost:5001")
        print("  Token stored at:", TOKEN_FILE)
        print("  POST endpoints require X-DND-Token header (send.py/push_stats.py handle this automatically)")
        print()
    else:
        print(f"DnD DM Display — Flask server starting on {scheme}://localhost:5001")
        print(f"Open {scheme}://localhost:5001 in your browser, then Chromecast the tab.")
        print()
    app.run(host=host, port=5001, threaded=True, debug=False, ssl_context=ssl_ctx)
