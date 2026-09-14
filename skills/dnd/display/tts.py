"""Narrator TTS for the display companion — Azure Speech, with Gemini fallback.

Server-side wrapper called by /tts in dnd-display-app.py. Two backends:

  azure   Azure Speech REST. Default. tr-TR MAI-Voice-2 HD voices, and the
          F0 tier bills nothing up to 500k characters a month.
  gemini  Google AI Studio. Kept because the key is already configured and it
          is the only backend if the Azure resource ever goes away.

Azure is chosen when an Azure key resolves, Gemini otherwise; DND_TTS_PROVIDER
overrides. Both return raw L16 PCM (24 kHz mono) so the browser's Int16 ->
Float32 path is identical either way.

Every successful synthesis is recorded to ~/.config/claude-dnd/tts-usage.json,
keyed by calendar month, so `python3 tts.py --usage` answers "where am I against
the free quota" across app restarts.

stdlib only — no requests, no SDKs. Setup walkthrough: docs/SKILL-tts.md.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils as _xml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ───────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "claude-dnd"

# Azure ----------------------------------------------------------------------
AZURE_KEY_FILE = CONFIG_DIR / "azure-tts.key"
AZURE_REGION_FILE = CONFIG_DIR / "azure-tts.region"
AZURE_DEFAULT_REGION = "northeurope"
# Headerless PCM, matching what the Gemini path returns. The riff-* formats
# would prepend a 44-byte WAV header that the browser decoder is not expecting.
AZURE_OUTPUT_FORMAT = "raw-24khz-16bit-mono-pcm"

# The tr-TR catalog, best first. MAI-Voice-2 is a generation ahead of the
# Ahmet/Emel pair, which reads as flatly robotic on long narration.
AZURE_VOICES_MALE = ["tr-TR-Aydın:MAI-Voice-2", "tr-TR-Aydın:MAI-Voice-2-Flash",
                     "tr-TR-AhmetNeural"]
AZURE_VOICES_FEMALE = ["tr-TR-Elif:MAI-Voice-2", "tr-TR-Elif:MAI-Voice-2-Flash",
                       "tr-TR-EmelNeural"]
AZURE_DEFAULT_VOICE = "tr-TR-Aydın:MAI-Voice-2"

# F0 allows 20 transactions per 60 seconds and throttles with 429 past that;
# a table clicking several blocks in a row hits it, so back off rather than
# surfacing the failure.
AZURE_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
AZURE_RETRIES = 3
AZURE_BACKOFF = 4.0

# Gemini ---------------------------------------------------------------------
GEMINI_KEY_FILE = CONFIG_DIR / "tts.key"
GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_TTS_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_TTS_MODEL}:generateContent"
)
GEMINI_VOICES_MALE = ["Charon", "Enceladus", "Fenrir", "Umbriel"]
GEMINI_VOICES_FEMALE = ["Aoede", "Gacrux", "Kore", "Vindemiatrix", "Zephyr"]
GEMINI_DEFAULT_VOICE = "Enceladus"

# Shared ---------------------------------------------------------------------
# Azure F0 caps a single request at 3000 characters of plain text; 2000 also
# keeps Gemini from degrading, so one limit serves both.
MAX_TEXT_CHARS = 2000

# Azure Speech F0: 500k characters per month, resets on Azure's clock, never
# expires. Only meaningful for the azure provider; the report says so.
FREE_TIER_CHARS = 500_000

USAGE_FILE = CONFIG_DIR / "tts-usage.json"

# Azure typically answers in under 15s; Gemini's preview model has been
# measured at 46-251s for a 77s block, so the timeout has to cover the worst.
DEFAULT_TIMEOUT = 120.0

# Legacy alias — dnd-display-app.py and docs still refer to KEY_FILE.
KEY_FILE = GEMINI_KEY_FILE


class TtsError(Exception):
    """Raised by synthesize_strict; caught by synthesize to fail silently."""


# ── Provider selection ──────────────────────────────────────────────────────

def _azure_key() -> Optional[str]:
    for env in ("DND_AZURE_TTS_KEY", "AZURE_SPEECH_KEY"):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    try:
        return AZURE_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _azure_region() -> str:
    for env in ("DND_AZURE_TTS_REGION", "AZURE_SPEECH_REGION"):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    try:
        return (AZURE_REGION_FILE.read_text(encoding="utf-8").strip()
                or AZURE_DEFAULT_REGION)
    except OSError:
        return AZURE_DEFAULT_REGION


def _gemini_key() -> Optional[str]:
    for env in ("DND_TTS_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    try:
        return GEMINI_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def provider() -> str:
    """Return the active backend: "azure", "gemini", or "none"."""
    forced = (os.environ.get("DND_TTS_PROVIDER") or "").strip().lower()
    if forced in ("azure", "gemini"):
        return forced
    if _azure_key():
        return "azure"
    if _gemini_key():
        return "gemini"
    return "none"


def _voice_table(prov: str) -> "tuple[list, list, str]":
    if prov == "azure":
        return AZURE_VOICES_MALE, AZURE_VOICES_FEMALE, AZURE_DEFAULT_VOICE
    return GEMINI_VOICES_MALE, GEMINI_VOICES_FEMALE, GEMINI_DEFAULT_VOICE


def voices_male() -> "list[str]":
    return _voice_table(provider())[0]


def voices_female() -> "list[str]":
    return _voice_table(provider())[1]


def default_voice() -> str:
    return _voice_table(provider())[2]


def valid_voices() -> frozenset:
    m, f, _ = _voice_table(provider())
    return frozenset(m + f)


# Snapshots of the provider active at import. The provider is decided by env
# vars and key files, neither of which changes inside a running process, so a
# plain value is honest here — and it stays a plain str/frozenset, which the
# JSON and template layers can serialize without surprises. Call default_voice()
# / valid_voices() instead if you need the live answer.
DEFAULT_VOICE = default_voice()
VALID_VOICES = valid_voices()


def key_source() -> str:
    """Describe where the active provider's key came from. Never returns it."""
    prov = provider()
    if prov == "azure":
        for env in ("DND_AZURE_TTS_KEY", "AZURE_SPEECH_KEY"):
            if (os.environ.get(env) or "").strip():
                return f"env:{env}"
        if _azure_key():
            return f"file:{AZURE_KEY_FILE}"
        return "unset"
    if prov == "gemini":
        for env in ("DND_TTS_KEY", "GEMINI_API_KEY"):
            if (os.environ.get(env) or "").strip():
                return f"env:{env}"
        if _gemini_key():
            return f"file:{GEMINI_KEY_FILE}"
        return "unset"
    return "unset"


# ── Usage accounting ────────────────────────────────────────────────────────

def _month_key() -> str:
    """Current month in UTC. Azure's quota resets on its own clock, and UTC is
    the closest stable approximation available locally."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load_usage() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_usage(chars: int, voice: str, prov: str) -> None:
    """Add one synthesis to this month's tally. Never raises.

    Counts the plain text actually handed to the backend — after truncation and
    excluding the SSML envelope — because that is what Azure meters.
    """
    if chars <= 0:
        return
    data = _load_usage()
    month = data.setdefault(_month_key(), {})
    bucket = month.setdefault(prov, {"chars": 0, "calls": 0, "voices": {}})
    bucket["chars"] = int(bucket.get("chars", 0)) + chars
    bucket["calls"] = int(bucket.get("calls", 0)) + 1
    voices = bucket.setdefault("voices", {})
    voices[voice] = int(voices.get(voice, 0)) + chars
    tmp = USAGE_FILE.with_suffix(".json.tmp")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, USAGE_FILE)   # atomic; a crash mid-write keeps the old file
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def usage_report(month: "str | None" = None) -> dict:
    """Return this month's totals plus the free-quota position."""
    month = month or _month_key()
    buckets = _load_usage().get(month, {})
    chars = sum(int(b.get("chars", 0)) for b in buckets.values()
                if isinstance(b, dict))
    calls = sum(int(b.get("calls", 0)) for b in buckets.values()
                if isinstance(b, dict))
    azure_chars = int((buckets.get("azure") or {}).get("chars", 0))
    return {
        "month": month,
        "chars": chars,
        "calls": calls,
        "by_provider": buckets,
        "free_tier_chars": FREE_TIER_CHARS,
        "free_tier_used_pct": round(100.0 * azure_chars / FREE_TIER_CHARS, 1),
        "free_tier_remaining": max(0, FREE_TIER_CHARS - azure_chars),
        # 1089 characters measured at 79s of speech on the tr-TR voices.
        "audio_minutes_est": round(chars / 13.785 / 60.0, 1),
    }


# ── Synthesis ───────────────────────────────────────────────────────────────

def _post(req: urllib.request.Request, timeout: float, retry_on: frozenset,
          retries: int, backoff: float) -> bytes:
    """POST with backoff on the caller's retryable statuses."""
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise TtsError(f"http {resp.status}")
                return resp.read()
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                snippet = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            last = f"http {e.code}: {snippet}"
            if e.code not in retry_on or attempt == retries:
                raise TtsError(last) from e
        except urllib.error.URLError as e:
            last = f"network: {e.reason}"
            if attempt == retries:
                raise TtsError(last) from e
        except TtsError:
            raise
        except Exception as e:
            raise TtsError(f"unexpected: {e}") from e
        time.sleep(backoff * (attempt + 1))
    raise TtsError(last or "exhausted retries")


def _synthesize_azure(text: str, voice: str, timeout: float) -> bytes:
    key = _azure_key()
    if not key:
        raise TtsError("no azure key configured")
    region = _azure_region()
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="tr-TR">'
        f'<voice name="{_xml.escape(voice, {chr(34): "&quot;"})}">'
        f'{_xml.escape(text)}</voice></speak>'
    )
    req = urllib.request.Request(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml.encode("utf-8"),
        method="POST",
    )
    req.add_header("Ocp-Apim-Subscription-Key", key)
    req.add_header("Content-Type", "application/ssml+xml")
    req.add_header("X-Microsoft-OutputFormat", AZURE_OUTPUT_FORMAT)
    req.add_header("User-Agent", "claude-dnd-display")
    return _post(req, timeout, AZURE_RETRY_STATUS, AZURE_RETRIES, AZURE_BACKOFF)


def _synthesize_gemini(text: str, voice: str, timeout: float) -> bytes:
    key = _gemini_key()
    if not key:
        raise TtsError("no gemini key configured")
    body = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
    }
    req = urllib.request.Request(
        f"{GEMINI_TTS_URL}?key={urllib.parse.quote(key)}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    raw = _post(req, timeout, frozenset({429, 500, 502, 503, 504}), 3, 8.0)
    try:
        data = json.loads(raw)
        b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise TtsError(f"bad response shape: {e}") from e
    try:
        return base64.b64decode(b64)
    except Exception as e:
        raise TtsError(f"bad base64: {e}") from e


def synthesize_strict(
    text: str,
    voice: "str | None" = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """Synthesize to raw L16 PCM (24 kHz mono). Raises TtsError on any failure.

    Use synthesize() for the silent-fail path.
    """
    prov = provider()
    if prov == "none":
        raise TtsError("no api key configured")

    text = (text or "").strip()
    if not text:
        raise TtsError("empty text")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    voice = (voice or "").strip() or default_voice()
    if voice not in valid_voices():
        voice = default_voice()

    pcm = (_synthesize_azure(text, voice, timeout) if prov == "azure"
           else _synthesize_gemini(text, voice, timeout))
    if not pcm:
        raise TtsError("empty pcm payload")

    record_usage(len(text), voice, prov)
    return pcm


def synthesize(text: str, voice: "str | None" = None) -> Optional[bytes]:
    """Silent-fail wrapper. Returns L16 PCM bytes or None on any failure."""
    try:
        return synthesize_strict(text, voice)
    except TtsError:
        return None


# ── CLI for verification ────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    import struct
    import subprocess
    import tempfile

    p = argparse.ArgumentParser(
        description="Verify narrator TTS setup for the DnD display companion.",
    )
    p.add_argument("--test", action="store_true",
                   help="Check key source + run a synthesis call. Writes nothing.")
    p.add_argument("--usage", action="store_true",
                   help="Report this month's character usage against the free quota.")
    p.add_argument("--voices", action="store_true",
                   help="List the active provider's voices.")
    p.add_argument("--speak", action="store_true",
                   help="Also play the synthesized audio.")
    p.add_argument("--text",
                   default="Höyüğün ağzı önünüzde açılıyor. Meşale ışığı oyma taş kapağın üzerinde kırılıyor.",
                   help="Override the default test phrase.")
    p.add_argument("--voice", default=None,
                   help="Voice name (default: the provider's default).")
    args = p.parse_args()

    prov = provider()

    if args.usage:
        r = usage_report()
        print(f"Month:    {r['month']}")
        print(f"Calls:    {r['calls']}")
        print(f"Chars:    {r['chars']:,}  (~{r['audio_minutes_est']} min of audio)")
        for name, b in sorted(r["by_provider"].items()):
            if isinstance(b, dict):
                print(f"  {name:<8} {int(b.get('chars', 0)):>8,} chars  "
                      f"{int(b.get('calls', 0)):>4} calls")
        print(f"Azure F0: {r['free_tier_used_pct']}% of {r['free_tier_chars']:,} "
              f"({r['free_tier_remaining']:,} left)")
        return 0

    if args.voices:
        print(f"Provider: {prov}")
        print(f"Default:  {default_voice()}")
        for label, names in (("Male", voices_male()), ("Female", voices_female())):
            print(f"  {label}:")
            for n in names:
                print(f"    {n}")
        return 0

    if not args.test:
        p.print_help()
        return 0

    src = key_source()
    print(f"Provider:   {prov}")
    print(f"Key source: {src}")
    if src == "unset":
        print("  → no key found. See docs/SKILL-tts.md to configure one.")
        return 2
    if prov == "azure":
        print(f"Region:     {_azure_region()}")

    voice = args.voice or default_voice()
    print(f"Voice:      {voice}")
    print(f"Text:       {args.text!r}")
    print("Synthesizing…")
    t0 = time.time()
    try:
        pcm = synthesize_strict(args.text, voice)
    except TtsError as e:
        print(f"  FAIL: {e}")
        return 1
    elapsed = time.time() - t0
    secs = len(pcm) / 48000.0
    print(f"  OK — {len(pcm):,} bytes L16 PCM (24 kHz mono), {secs:.1f}s of audio "
          f"in {elapsed:.1f}s")

    if args.speak:
        sample_rate = 24000
        header = (
            b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm))
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(header + pcm)
            wav_path = f.name
        for player in ("paplay", "afplay", "aplay"):
            try:
                subprocess.run([player, wav_path], check=False)
                break
            except FileNotFoundError:
                continue
        else:
            print(f"  (no audio player found — WAV kept at {wav_path})")
            return 0
        os.unlink(wav_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
