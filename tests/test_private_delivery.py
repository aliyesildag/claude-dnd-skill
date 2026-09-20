"""End-to-end delivery tests for private ("--to") blocks.

A private line is the one payload in this system whose bug is invisible at the
table: the DM whispers to one player, everyone reads it on their own screen,
and nothing anywhere reports that it went wide. The filter is `_visible_to()`,
but it is applied at four separate places — the live broadcast, the on-connect
replay, the /tail poll and the /snapshot burst — and a whisper only stays
private if all four agree. Unit-testing the predicate proves none of that.

So this file stands up the real thing: the Flask app on a scratch port with a
throwaway runtime dir, one SSE client per seat bound the way a player's browser
binds (`?character=`), plus an unbound one standing in for the DM screen. It
then sends through `send.py` — the command the DM actually types — and records
what each socket received. A leak here means bytes crossed the wire to a seat
that should never have seen them, which is the only definition that matters.

Run as a test:     python3 -m pytest tests/test_private_delivery.py
Run as a report:   python3 tests/test_private_delivery.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import unittest

try:
    from tests.live_display import DM, LiveDisplay
except ImportError:                       # run as a script from the repo root
    from live_display import DM, LiveDisplay


# The seats. The DM screen binds no character and therefore sees everything —
# that is the rule the whole feature rests on, so it is a seat here too.
SEATS = ["Dilaver", "Hisrayt", "Yapraksever", "Kızıl Zenci"]
ALL_VIEWERS = [DM] + SEATS

# How long to keep listening after the expected recipients have all reported.
# A leak arrives on the same broadcast as the legitimate delivery, so this only
# has to cover socket jitter — but it must not be zero, or a leak that is one
# scheduler slice late reads as a pass.
SETTLE = 0.35
DEADLINE = 6.0


class Scenario:
    """One send and the set of viewers entitled to it."""

    def __init__(self, name: str, marker: str, expected: list[str],
                 via: str, note: str = "") -> None:
        self.name = name
        self.marker = marker
        self.expected = set(expected)
        self.via = via
        self.note = note
        self.actual: set[str] = set()

    @property
    def leaked(self) -> set[str]:
        return self.actual - self.expected

    @property
    def missing(self) -> set[str]:
        return self.expected - self.actual

    @property
    def ok(self) -> bool:
        return not self.leaked and not self.missing


class Harness:
    """The live table: server, seats, and the scenarios played through them."""

    def __init__(self) -> None:
        self.d = LiveDisplay(module_suffix="private")
        self.base = self.d.base
        self.send = self.d.send
        # A party the display knows, so send.py's --to pre-flight resolves the
        # names instead of refusing them.
        self.d.set_party(SEATS)
        self.seats = self.d.open_seats(SEATS)
        self.scenarios: list[Scenario] = []
        self.checks: list[tuple[str, bool, str]] = []

    # ── plumbing ──────────────────────────────────────────────────────────

    def run_send_py(self, argv: list[str], body: str, quiet: bool = False) -> int:
        """Invoke send.py's main() the way the shell would. Returns its exit
        code; 0 means it accepted and posted. `quiet` swallows the diagnostics
        of a send we expect to be refused, so the report stays readable."""
        old_argv, old_stdin = sys.argv, sys.stdin
        sys.argv = ["send.py"] + argv
        sys.stdin = io.StringIO(body)
        sink = io.StringIO()
        try:
            with contextlib.redirect_stderr(sink) if quiet \
                    else contextlib.nullcontext():
                self.send.main()
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)
        finally:
            sys.argv, sys.stdin = old_argv, old_stdin

    # ── scenarios ─────────────────────────────────────────────────────────

    def play(self, scenario: Scenario, emit) -> None:
        """Send, wait for the entitled seats, then keep listening for a leak."""
        self.scenarios.append(scenario)
        emit()
        end = time.time() + DEADLINE
        while time.time() < end:
            if all(self.seats[v].saw(scenario.marker) for v in scenario.expected):
                break
            time.sleep(0.05)
        time.sleep(SETTLE)
        scenario.actual = {v for v in ALL_VIEWERS if self.seats[v].saw(scenario.marker)}

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    def run_all(self) -> "Harness":
        self.play(
            Scenario("herkese açık anlatı", "«S1»", ALL_VIEWERS, "send.py"),
            lambda: self.run_send_py([], "Kapak odası soğuk. «S1»"))

        self.play(
            Scenario("fısıltı → Dilaver", "«S2»", [DM, "Dilaver"], "send.py --to"),
            lambda: self.run_send_py(
                ["--to", "Dilaver"], "Tuz çizgisinde bir boşluk görüyorsun. «S2»"))

        self.play(
            Scenario("fısıltı → Kızıl Zenci", "«S3»", [DM, "Kızıl Zenci"],
                     "send.py --to", "boşluklu + Türkçe karakterli isim"),
            lambda: self.run_send_py(
                ["--to", "Kızıl Zenci"], "Sırtında bir ağırlık. «S3»"))

        # The wire contract, not the CLI's: the server lowercases and trims the
        # address, so a DM who types it loosely still reaches the right seat.
        self.play(
            Scenario("büyük harf + boşluk toleransı", "«S4»", [DM, "Hisrayt"],
                     "POST /chunk", 'to="  HISRAYT  "'),
            lambda: self.d.post("/chunk",
                               {"text": "Ocağın altından ses geliyor. «S4»",
                                "to": "  HISRAYT  "}))

        # Fails closed: an address nobody is bound to reaches nobody but the DM.
        self.play(
            Scenario("tanınmayan alıcı → kapalı", "«S5»", [DM],
                     "POST /chunk", "hiç kimsenin bağlı olmadığı isim"),
            lambda: self.d.post("/chunk",
                               {"text": "Kimsenin duymadığı satır. «S5»",
                                "to": "Hayalet"}))

        # Private is orthogonal to the block type — a whispered NPC line has to
        # stay an NPC line, or it lands on the target's screen as narration.
        self.play(
            Scenario("NPC repliği, fısıltı olarak", "«S6»", [DM, "Yapraksever"],
                     "send.py --npc --to"),
            lambda: self.run_send_py(
                ["--npc", "Zülfü Nine", "--to", "Yapraksever"],
                '"Seni bekliyordum." «S6»'))

        npc_blocks = self.seats["Yapraksever"].blocks_with("«S6»")
        self.check("fısıltı blok tipini koruyor (npc)",
                   bool(npc_blocks) and npc_blocks[0].get("npc") == "Zülfü Nine",
                   f"npc={npc_blocks[0].get('npc') if npc_blocks else None!r}")

        # ── the three replay paths ────────────────────────────────────────
        # Everything above is already in the log by now, so a seat joining here
        # is the late-joiner case: a player who refreshed, or whose phone woke.
        latecomer = self.d.transient_seat("Hisrayt (geç katılan)", character="Hisrayt")
        time.sleep(SETTLE)
        self.check("geç katılanın tekrarı süzülüyor",
                   latecomer.saw("«S1»") and latecomer.saw("«S4»")
                   and not latecomer.saw("«S2»") and not latecomer.saw("«S3»"),
                   "kendi fısıltısı + açık satırlar var, başkasınınki yok")
        latecomer.close()

        tail = self.d.get("/tail", character="Hisrayt", since=0)
        tail_text = json.dumps(tail, ensure_ascii=False)
        self.check("/tail yoklaması süzülüyor",
                   "«S2»" not in tail_text and "«S3»" not in tail_text,
                   "SSE'si engellenen istemcinin yedek yolu")

        snap = self.d.get("/snapshot", character="Hisrayt")
        snap_text = json.dumps(snap, ensure_ascii=False)
        self.check("/snapshot açılış paketi süzülüyor",
                   "«S4»" in snap_text
                   and "«S2»" not in snap_text and "«S3»" not in snap_text,
                   "yoklama istemcisinin ilk ekranı")

        # ── the sender's own guard ────────────────────────────────────────
        code = self.run_send_py(["--to", "Qwertyuiop"], "Gitmemesi gereken satır.",
                                quiet=True)
        self.check("send.py tanınmayan --to adresini reddediyor",
                   code == 2, f"çıkış kodu {code}")

        return self

    def close(self) -> None:
        self.d.close()


_HARNESS: "Harness | None" = None


def get_harness() -> Harness:
    """Boot once; both the test cases and the report read the same run."""
    global _HARNESS
    if _HARNESS is None:
        _HARNESS = Harness().run_all()
    return _HARNESS


# ─── Tests ────────────────────────────────────────────────────────────────────

class PrivateDeliveryTest(unittest.TestCase):
    """Each scenario, asserted twice: everyone entitled got it, and nobody
    else did. The second half is the one that matters."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = get_harness()

    def _scenario(self, name: str) -> Scenario:
        return next(s for s in self.h.scenarios if s.name == name)

    def test_public_narration_reaches_every_seat(self) -> None:
        s = self._scenario("herkese açık anlatı")
        self.assertEqual(s.missing, set(), "açık satır bir koltuğa ulaşmadı")

    def test_whisper_reaches_only_its_target(self) -> None:
        for name in ("fısıltı → Dilaver", "fısıltı → Kızıl Zenci",
                     "NPC repliği, fısıltı olarak"):
            with self.subTest(scenario=name):
                s = self._scenario(name)
                self.assertEqual(s.leaked, set(), "fısıltı masaya sızdı")
                self.assertEqual(s.missing, set(), "fısıltı alıcısına ulaşmadı")

    def test_address_is_case_and_space_insensitive(self) -> None:
        s = self._scenario("büyük harf + boşluk toleransı")
        self.assertTrue(s.ok, f"sızan: {s.leaked}, eksik: {s.missing}")

    def test_unknown_address_fails_closed(self) -> None:
        s = self._scenario("tanınmayan alıcı → kapalı")
        self.assertEqual(s.leaked, set(),
                         "tanınmayan alıcıya yazılan satır bir oyuncuya gitti")

    def test_dm_screen_sees_every_whisper(self) -> None:
        for s in self.h.scenarios:
            with self.subTest(scenario=s.name):
                self.assertIn(DM, s.actual, "DM ekranı satırı görmedi")

    def test_replay_paths_and_sender_guard(self) -> None:
        for name, passed, detail in self.h.checks:
            with self.subTest(check=name):
                self.assertTrue(passed, detail)


# ─── Report ───────────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def report() -> int:
    h = get_harness()
    width = 78
    print()
    print(_c("╔" + "═" * width + "╗", "38;5;180"))
    title = "  KİŞİYE ÖZEL MESAJ TESLİMAT TESTİ · dnd display"
    print(_c("║", "38;5;180") + _c(title.ljust(width), "1;38;5;223")
          + _c("║", "38;5;180"))
    print(_c("╚" + "═" * width + "╝", "38;5;180"))
    print(f"  sunucu    {h.base}    koltuk sayısı {len(h.seats)}")
    print()

    heads = [DM] + SEATS
    cols = [max(9, len(x) + 2) for x in heads]
    print(_c("  ── teslimat matrisi " + "─" * (width - 21), "38;5;240"))
    print("   " + " " * 34 + "".join(x.center(w) for x, w in zip(heads, cols)))

    for i, s in enumerate(h.scenarios, 1):
        cells = []
        for viewer, w in zip(heads, cols):
            got, want = viewer in s.actual, viewer in s.expected
            if got and want:
                cells.append(_c("✓".center(w), "38;5;77"))
            elif not got and not want:
                cells.append(_c("·".center(w), "38;5;238"))
            elif got and not want:
                cells.append(_c("SIZDI".center(w), "1;38;5;203"))
            else:
                cells.append(_c("EKSİK".center(w), "1;38;5;214"))
        label = f"{i}. {s.name}"
        print(f"   {label:<34}" + "".join(cells))
        via = f"      ↳ {s.via}" + (f" — {s.note}" if s.note else "")
        print(_c(via, "38;5;240"))

    print()
    print(_c("  ── ayrıca " + "─" * (width - 11), "38;5;240"))
    for name, passed, detail in h.checks:
        mark = _c("✓", "38;5;77") if passed else _c("✗", "1;38;5;203")
        print(f"   {mark}  {name:<44}" + _c(detail, "38;5;240"))

    leaks = sum(len(s.leaked) for s in h.scenarios)
    failed = [s for s in h.scenarios if not s.ok] + \
             [n for n, p, _ in h.checks if not p]
    print()
    if not failed:
        line = (f"  {len(h.scenarios)} senaryo · {len(h.checks)} ek kontrol · "
                f"0 sızıntı — fısıltılar masada kalıyor")
        print(_c(line, "1;38;5;77"))
    else:
        print(_c(f"  {len(failed)} başarısız · {leaks} sızıntı", "1;38;5;203"))
    print()
    return 0 if not failed else 1


if __name__ == "__main__":
    code = report()
    get_harness().close()
    sys.exit(code)
