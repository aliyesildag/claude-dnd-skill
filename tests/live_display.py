"""Boot the real display server for a test, on a scratch port and a scratch disk.

Some of what this display promises cannot be checked by calling a function.
"Only Dilaver received it" is a statement about sockets; "the window is still
open for the other player" is a statement about two requests racing. Both are
properties of the running server, and both are invisible to a unit test of the
predicate underneath them.

So: a throwaway runtime dir (set before the app is imported, or the test writes
into the live campaign's state), a free port, the app on a background thread,
and one SSE reader per seat bound the way a player's browser binds. Nothing is
stubbed by default — a test that wants Jev's judgment out of the way replaces
the module itself.

This file is a helper, not a test: no test_* names, nothing collected.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "skills" / "dnd" / "display"

# The DM screen binds no character and therefore sees everything. That is not a
# detail of the tests — it is the rule both features rest on, so it gets a name.
DM = "DM ekranı"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def load_module(path: Path, name: str):
    """Import a module from an arbitrary path under a name of our choosing, so
    two test files can each hold their own copy of the app."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Seat:
    """One browser's SSE connection, read on its own thread.

    Keeps everything that arrives, because proving a payload did NOT reach a
    seat means having the complete list of what did.
    """

    def __init__(self, base: str, label: str, character: str = "") -> None:
        self.label = label
        self.character = character
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._connected = threading.Event()
        url = f"{base}/stream"
        if character:
            url += "?" + urllib.parse.urlencode({"character": character})
        self._resp = urllib.request.urlopen(url, timeout=10)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self._resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.startswith("data: "):
                    continue
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if payload.get("sse_alive"):
                    self._connected.set()
                with self._lock:
                    self.events.append(payload)
        except Exception:
            pass          # socket closed at teardown — nothing to report

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout)

    def all_events(self, since: int = 0) -> list[dict]:
        with self._lock:
            return list(self.events[since:])

    def mark(self) -> int:
        """A line in this seat's log, to ask questions after.

        A seat keeps every event for the life of the server, so "did this
        arrive?" answered over the whole log is really "did this ever arrive,
        in any test" — which is a question that answers yes long after the
        code under test stopped doing it. Mark before the action, ask after.
        """
        with self._lock:
            return len(self.events)

    def payloads(self, key: str, since: int = 0) -> list:
        """Every value broadcast under this key, including the ones that came
        in the on-connect burst — state a seat gets back on reconnect counts."""
        return [e[key] for e in self.all_events(since) if key in e]

    def await_payload(self, key: str, match=None, timeout: float = 5.0,
                      since: int = 0):
        """Wait for a payload under `key` (optionally satisfying `match`) and
        return it, or None if it never arrived."""
        end = time.time() + timeout
        while time.time() < end:
            for value in self.payloads(key, since):
                if match is None or match(value):
                    return value
            time.sleep(0.05)
        return None

    def blocks_with(self, marker: str) -> list[dict]:
        """Every text payload carrying this marker, replayed ones included —
        a leak through the replay path is still a leak."""
        hits = []
        for e in self.all_events():
            for candidate in [e] + list(e.get("replay_batch") or []):
                if marker in str(candidate.get("text", "")):
                    hits.append(candidate)
        return hits

    def saw(self, marker: str) -> bool:
        return bool(self.blocks_with(marker))

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass


class LiveDisplay:
    """The running server, plus the seats watching it."""

    def __init__(self, campaign: str = "test-masa", module_suffix: str = "") -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dnd-live-")
        # Before the import, not after: the app resolves its runtime paths at
        # module level, and a late override would leave it writing to the real
        # campaign's state files.
        os.environ["DND_RUNTIME_DIR"] = self.tmp.name
        os.environ["DND_CAMPAIGN_ROOT"] = self.tmp.name

        # `runtime_paths` resolves the writable dir once, at import, and caches
        # it in a module global. Another test file that imported send.py or
        # jev_window.py earlier in the same pytest process has already done
        # that — against the real ~/.claude/dnd — and our fresh import of the
        # app would quietly reuse it. Then the "isolated" server writes the
        # live campaign's stats, log and active-campaign pointer.
        # Setting the env vars is not enough; the cache has to go with them.
        for cached in ("runtime_paths", "paths"):
            sys.modules.pop(cached, None)

        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        suffix = module_suffix or str(self.port)
        self.app_mod = load_module(DISPLAY / "dnd-display-app.py", f"_live_app_{suffix}")
        self._assert_sandboxed()
        self.send = load_module(DISPLAY / "send.py", f"_live_send_{suffix}")
        self._retarget_send()

        # A campaign stamp, because several code paths read one and quietly do
        # nothing without it — the response window among them. Its directory has
        # to exist too, or every feed line ends in a _persist_tail failure.
        self.campaign = campaign
        self.campaign_dir = Path(self.tmp.name) / "campaigns" / campaign
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        Path(self.app_mod.CAMP_FILE).write_text(campaign, encoding="utf-8")

        # The report these tests print is meant to be read as a whole, so the
        # server's own chatter is muted rather than left to interleave with it.
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.app_mod.app.logger.setLevel(logging.ERROR)
        import flask.cli
        flask.cli.show_server_banner = lambda *a, **k: None

        threading.Thread(
            target=lambda: self.app_mod.app.run(
                host="127.0.0.1", port=self.port,
                threaded=True, debug=False, use_reloader=False),
            daemon=True,
        ).start()
        self._wait_for_server()
        self.seats: dict[str, Seat] = {}
        self._transient: list[Seat] = []

    # ── plumbing ──────────────────────────────────────────────────────────

    def _assert_sandboxed(self) -> None:
        """Refuse to run against anything but the scratch dir.

        The belt to the purge's braces, and the one that matters: a silent
        failure here does not fail a test, it edits the user's campaign. So it
        is checked rather than assumed, before a single request is served.
        """
        tmp = Path(self.tmp.name).resolve()
        for label, path in (("runtime", self.app_mod.LOG_FILE),
                            ("campaign pointer", self.app_mod.CAMP_FILE)):
            resolved = Path(path).resolve()
            if tmp not in resolved.parents:
                raise RuntimeError(
                    f"live display would write its {label} to {resolved}, "
                    f"outside the scratch dir {tmp} — refusing to start")

    def _retarget_send(self) -> None:
        """send.py hardcodes localhost:5001. Point it here so the tests drive
        the command the DM actually types, not a reimplementation of it."""
        s = self.send
        s.BASE_URL = self.base
        s.FLASK_URL = f"{self.base}/chunk"
        s.STATS_URL = f"{self.base}/stats"
        s.HEALTH_URL = f"{self.base}/health"
        s.PARTY_URL = f"{self.base}/party"
        s.DICE_REQ_URL = f"{self.base}/dice-request"

    def _wait_for_server(self, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                urllib.request.urlopen(f"{self.base}/ping", timeout=1).read()
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("display server did not come up")

    def post(self, path: str, body: dict) -> "tuple[int, dict]":
        """POST and return (status, parsed body). A 4xx is an answer here, not
        an exception: half these tests are about which refusal you get."""
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, _maybe_json(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _maybe_json(exc.read())

    def get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=5) as resp:
            return _maybe_json(resp.read())

    # ── table setup ───────────────────────────────────────────────────────

    def set_party(self, players: list) -> None:
        """Seat a party the display knows. Accepts names or full player dicts."""
        rows = [{"name": p} if isinstance(p, str) else dict(p) for p in players]
        self.post("/stats", {"players": rows, "replace_players": True})

    def seat(self, label: str, character: str = "") -> Seat:
        """A seat that stays for the run, and joins `self.seats`."""
        s = self._connect(label, character)
        self.seats[label] = s
        return s

    def transient_seat(self, label: str, character: str = "") -> Seat:
        """A seat one test opens and closes — a reconnect, a late joiner.

        Kept out of `self.seats` on purpose: a test that sweeps every seat is
        asking about the table, and a socket another test already hung up on
        is not at the table. Still closed at teardown if its test did not.
        """
        s = self._connect(label, character)
        self._transient.append(s)
        return s

    def _connect(self, label: str, character: str) -> Seat:
        s = Seat(self.base, label, character=character)
        if not s.wait_connected():
            raise RuntimeError(f"seat {label!r} never opened its stream")
        return s

    def open_seats(self, names: list, with_dm: bool = True) -> dict:
        if with_dm:
            self.seat(DM)
        for name in names:
            self.seat(name, character=name)
        return self.seats

    def close(self) -> None:
        for s in list(self.seats.values()) + self._transient:
            s.close()
        self.tmp.cleanup()


def _maybe_json(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


# ─── Report ───────────────────────────────────────────────────────────────────
# The live test files double as readable reports (python3 tests/test_x.py).
# One collector and one printer, shared, because the first copy of this got a
# detail wrong that mattered: unittest reports a failing subTest through
# addSubTest, not addFailure, and a collector that only listens to the latter
# prints a green report over a red test.

_TTY = sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


class Collector(unittest.TextTestResult):
    """Exactly one row per test, whatever path unittest takes to report it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rows: dict = {}          # method name → (state, detail)

    @staticmethod
    def _detail(err) -> str:
        return str(err[1]).split("\n")[0][:60] if err else ""

    def _note(self, test, state: str, detail: str = "") -> None:
        name = getattr(test, "_testMethodName", str(test))
        prev = self.rows.get(name)
        if prev and prev[0] != "ok":
            return                    # first failure wins; a later success does not erase it
        self.rows[name] = (state, detail)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._note(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._note(test, "fail", self._detail(err))

    def addError(self, test, err):
        super().addError(test, err)
        self._note(test, "error", self._detail(err))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._note(test, "skip", reason[:60])

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            params = ", ".join(f"{k}={v}" for k, v in (subtest.params or {}).items())
            self._note(test, "fail", f"[{params}] {self._detail(err)}"[:60])


def print_report(title: str, subtitle: str, case, sections: list, ok_line: str) -> int:
    """Run `case`'s tests in the order `sections` gives and print them as a
    checklist. Every test the class defines must appear in a section — a rule
    nobody wrote down is a rule nobody reads."""
    import io
    width = 78
    print()
    print(paint("╔" + "═" * width + "╗", "38;5;180"))
    print(paint("║", "38;5;180") + paint(("  " + title).ljust(width), "1;38;5;223")
          + paint("║", "38;5;180"))
    print(paint("╚" + "═" * width + "╝", "38;5;180"))
    if subtitle:
        print(paint("  " + subtitle, "38;5;240"))
    print()

    ordered = [m for _, names in sections for m in names]
    defined = {m for m in dir(case) if m.startswith("test_")}
    missing = defined - set(ordered)
    assert not missing, f"report is missing tests: {sorted(missing)}"

    suite = unittest.TestSuite(case(m) for m in ordered)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0,
                                     resultclass=Collector).run(suite)

    for head, names in sections:
        print(paint(f"  ── {head} " + "─" * (width - len(head) - 5), "38;5;240"))
        for name in names:
            label = case(name).shortDescription() or name
            state, detail = result.rows.get(name, ("error", "çalışmadı"))
            mark = {"ok": paint("✓", "38;5;77"), "skip": paint("–", "38;5;240")}.get(
                state, paint("✗", "1;38;5;203"))
            print(f"   {mark}  {label:<52}" + paint(detail, "38;5;240"))

    bad = sum(1 for s, _ in result.rows.values() if s in ("fail", "error"))
    bad = max(bad, len(result.failures) + len(result.errors))
    print()
    if not bad:
        print(paint(f"  {result.testsRun} kural · hepsi tutuyor — {ok_line}", "1;38;5;77"))
    else:
        print(paint(f"  {result.testsRun} kuraldan {bad} tanesi tutmuyor", "1;38;5;203"))
    print()
    return 0 if not bad else 1
