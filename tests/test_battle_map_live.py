"""The battle board, as the table receives it.

The board is drawn on the server from its spec and shipped as SVG, so what a
seat has is a string — and a string either contains the hidden goblin's name
or it does not. That is the property this file exists for: a token the DM
hid is absent from every byte a seat receives, live and on reconnect and over
the polling fallback, while the DM screen gets the whole board. Everything
else here — moves, rounds, the active-turn ring, clearing — is the board doing
what a board does, checked the same way: by reading what arrived.

`send.py --battle-map` is driven too, because resolving and validating the
spec before the send is the DM-facing half of the feature.

Run as a test:     python3 -m pytest tests/test_battle_map_live.py
Run as a report:   python3 tests/test_battle_map_live.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    from tests.live_display import DM, LiveDisplay, print_report
except ImportError:                       # run as a script from the repo root
    from live_display import DM, LiveDisplay, print_report


PARTY = ["Dilaver", "Hisrayt", "Yapraksever"]
SEAT = "Dilaver"

SPEC = {
    "handle": "kavran",
    "cols": 8, "rows": 6,
    "terrain": [
        {"tiles": "B2-C3", "kind": "su"},
        {"tiles": "F1", "kind": "sütun", "impassable": True},
        {"tiles": "D5-E5", "kind": "moloz", "difficult": True},
    ],
}
HIDDEN = "Pusucu"       # a name that appears nowhere else on the table


class BattleMapLive(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.d = LiveDisplay(module_suffix="battlemap")
        cls.app = cls.d.app_mod
        cls.d.set_party(PARTY)
        cls.seats = cls.d.open_seats(PARTY)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.d.close()

    def setUp(self) -> None:
        # Every test starts from no board and no turn, whatever the last one did.
        self.d.post("/battle-map", {"clear": True})
        self.d.post("/stats", {"turn_order": None})
        time.sleep(0.15)
        self.marks = {name: seat.mark() for name, seat in self.seats.items()}

    # ── helpers ───────────────────────────────────────────────────────────

    def boards(self, seat: str) -> list:
        """battle_map payloads this seat received since setUp, in order."""
        return self.seats[seat].payloads("battle_map", since=self.marks[seat])

    def latest(self, seat: str, timeout: float = 4.0):
        """The most recent board a seat holds — what its screen is showing."""
        end = time.time() + timeout
        while time.time() < end:
            got = self.boards(seat)
            if got:
                return got[-1]
            time.sleep(0.05)
        return None

    def await_board(self, seat: str, match, timeout: float = 4.0):
        return self.seats[seat].await_payload(
            "battle_map", match, timeout=timeout, since=self.marks[seat])

    def open_board(self, **extra) -> None:
        status, body = self.d.post("/battle-map", {"spec": SPEC, "round": 1, **extra})
        self.assertEqual(status, 204, body)

    def settle(self) -> None:
        time.sleep(0.35)

    # ── the board arrives ─────────────────────────────────────────────────

    def test_opening_a_board_reaches_every_seat(self):
        """açılan tahta her koltuğa ulaşıyor"""
        self.open_board(pos={"Dilaver": "A1", "Goblin": "G4"})
        for name in [DM] + PARTY:
            with self.subTest(seat=name):
                b = self.await_board(name, lambda x: x and "Goblin" in x["svg"])
                self.assertIsNotNone(b, f"{name} tahtayı almadı")
                self.assertEqual(b["handle"], "kavran")
                self.assertIn("Dilaver", b["svg"])

    def test_an_invalid_spec_is_refused_with_reasons(self):
        """bozuk spec gerekçesiyle reddediliyor"""
        status, body = self.d.post("/battle-map",
                                   {"spec": {"handle": "x", "cols": 0, "rows": 5}})
        self.assertEqual(status, 400)
        self.assertTrue(any("cols" in e for e in body.get("details", [])), body)

    def test_a_move_without_a_board_is_refused(self):
        """tahta yokken hamle reddediliyor"""
        status, _ = self.d.post("/battle-map", {"pos": {"Dilaver": "A1"}})
        self.assertEqual(status, 409)

    # ── what the seats must never see ─────────────────────────────────────

    def test_a_hidden_token_never_reaches_a_seat(self):
        """gizli token hiçbir koltuğa ulaşmıyor"""
        self.open_board(pos={"Dilaver": "A1", HIDDEN: "G4"}, hide=[HIDDEN])
        dm = self.await_board(DM, lambda x: x and HIDDEN in x["svg"])
        self.assertIsNotNone(dm, "DM ekranı gizli tokenı görmüyor")
        self.settle()
        for name in PARTY:
            with self.subTest(seat=name):
                blob = json.dumps(self.seats[name].all_events(self.marks[name]),
                                  ensure_ascii=False)
                self.assertNotIn(HIDDEN, blob, f"{name} koltuğuna gizli tokenın baytı gitti")
                self.assertIn("Dilaver", self.latest(name)["svg"], "açık tahta da gelmedi")

    def test_revealing_puts_it_on_every_screen(self):
        """açığa çıkarınca herkeste beliriyor"""
        self.open_board(pos={HIDDEN: "G4"}, hide=[HIDDEN])
        self.settle()
        self.d.post("/battle-map", {"reveal": [HIDDEN]})
        b = self.await_board(SEAT, lambda x: x and HIDDEN in x["svg"])
        self.assertIsNotNone(b, "açığa çıkan token koltuğa gelmedi")

    def test_a_late_joining_seat_gets_the_board_but_not_the_secret(self):
        """geç katılan koltuk tahtayı alıyor, sırrı almıyor"""
        self.open_board(pos={"Dilaver": "A1", HIDDEN: "G4"}, hide=[HIDDEN])
        self.settle()
        late = self.d.transient_seat("Hisrayt (geç)", character="Hisrayt")
        b = late.await_payload("battle_map", lambda x: x and "Dilaver" in x["svg"])
        self.assertIsNotNone(b, "geç katılan tahtayı almadı")
        time.sleep(0.3)
        self.assertNotIn(HIDDEN, json.dumps(late.all_events(), ensure_ascii=False))
        late.close()

        dm_late = self.d.transient_seat("DM (geç)")
        full = dm_late.await_payload("battle_map", lambda x: x and HIDDEN in x["svg"])
        self.assertIsNotNone(full, "geç bağlanan DM ekranı tam tahtayı almadı")
        dm_late.close()

    def test_the_polling_paths_keep_the_secret_too(self):
        """yoklama yolları da sırrı tutuyor"""
        # Ask /tail the way a client does — from where the journal stood before
        # this board opened. The journal is server-lifetime, and an earlier
        # test revealed this same token on purpose; that broadcast is public
        # and is supposed to be there.
        seq = self.d.get("/tail", since=0)["seq"]
        self.open_board(pos={HIDDEN: "G4"}, hide=[HIDDEN])
        self.settle()
        for path, kw in (("/tail", {"since": seq}), ("/snapshot", {})):
            with self.subTest(path=path):
                seat = json.dumps(self.d.get(path, character="Hisrayt", **kw), ensure_ascii=False)
                dm = json.dumps(self.d.get(path, **kw), ensure_ascii=False)
                self.assertNotIn(HIDDEN, seat, f"{path} koltuğa sırrı verdi")
                self.assertIn(HIDDEN, dm, f"{path} DM'e tam tahtayı vermedi")

    # ── the board does what a board does ──────────────────────────────────

    def test_a_move_redraws_the_token_where_it_went(self):
        """hamle tokenı gittiği yere çiziyor"""
        self.open_board(pos={"Dilaver": "A1"})
        self.settle()
        first = self.latest(SEAT)["svg"]
        self.d.post("/battle-map", {"pos": {"Dilaver": "D4"}})
        moved = self.await_board(SEAT, lambda x: x and x["svg"] != first)
        self.assertIsNotNone(moved, "hamle çizilmedi")
        # D4 → col 3, row 3 → circle centre (3.5, 3.5)
        self.assertIn('cx="3.5" cy="3.5"', moved["svg"])
        self.assertNotIn('cx="0.5" cy="0.5"', moved["svg"], "eski kare hâlâ dolu")

    def test_a_bad_or_off_grid_tile_is_refused(self):
        """bozuk ya da tahta dışı kare reddediliyor"""
        self.open_board()
        self.assertEqual(self.d.post("/battle-map", {"pos": {"Dilaver": "Z9"}})[0], 400)
        self.assertEqual(self.d.post("/battle-map", {"pos": {"Dilaver": "4C"}})[0], 400)
        self.assertEqual(self.d.post("/battle-map", {"pos": {"Dilaver": "H6"}})[0], 204)

    def test_party_names_are_pcs_and_others_are_npcs(self):
        """parti üyesi PC, gerisi NPC çiziliyor"""
        self.open_board(pos={"Dilaver": "A1", "Goblin": "B1"})
        b = self.await_board(SEAT, lambda x: x and "Goblin" in x["svg"])
        svg = b["svg"]
        self.assertIn('cx="0.5" cy="0.5" r="0.38" fill="#d4b24c"', svg, "PC altın değil")
        self.assertIn('cx="1.5" cy="0.5" r="0.38" fill="#b0432f"', svg, "NPC kızıl değil")

    def test_the_turn_ring_follows_the_turn_order(self):
        """sıra halkası sırayı takip ediyor"""
        self.open_board(pos={"Dilaver": "A1", "Goblin": "B1"})
        self.settle()
        self.assertNotIn('class="halo"', self.latest(SEAT)["svg"], "sıra yokken halka var")
        self.d.post("/stats", {"turn_order": {"current": "Goblin", "order": ["Goblin", "Dilaver"]}})
        b = self.await_board(SEAT, lambda x: x and 'class="halo"' in x["svg"])
        self.assertIsNotNone(b, "sıra değişince tahta yeniden çizilmedi")
        self.assertIn('class="halo" cx="1.5" cy="0.5"', b["svg"], "halka yanlış tokenda")

    def test_unplaced_tokens_are_named_in_the_caption(self):
        """yerleştirilmemiş tokenlar altyazıda"""
        self.open_board(pos={"Dilaver": "A1", "Hisrayt": "-"})
        b = self.await_board(SEAT, lambda x: x and "harita dışı" in x.get("label", ""))
        self.assertIsNotNone(b)
        self.assertIn("Hisrayt", b["label"])
        self.assertIn("tur 1", b["label"])

    def test_removing_takes_the_token_off_for_good(self):
        """çıkarılan token tahtadan gidiyor"""
        self.open_board(pos={"Goblin": "B1"})
        self.settle()
        self.d.post("/battle-map", {"remove": ["Goblin"]})
        b = self.await_board(SEAT, lambda x: x and "Goblin" not in x["svg"])
        self.assertIsNotNone(b, "çıkarılan token hâlâ çiziliyor")

    def test_clearing_closes_the_board_everywhere(self):
        """kapatınca tahta her yerden gidiyor"""
        self.open_board(pos={"Dilaver": "A1"})
        self.settle()
        self.d.post("/battle-map", {"clear": True})
        for name in [DM] + PARTY:
            with self.subTest(seat=name):
                gone = self.await_board(name, lambda x: x is None)
                self.assertIsNone(gone, f"{name} kapanışı almadı")
        snap = json.dumps(self.d.get("/snapshot"), ensure_ascii=False)
        self.assertNotIn('"svg"', snap, "kapanan tahta yeniden bağlanana geri geliyor")

    # ── the DM's own command ──────────────────────────────────────────────

    def run_send_py(self, argv: list) -> int:
        old_argv, old_stdin = sys.argv, sys.stdin
        sys.argv, sys.stdin = ["send.py"] + argv, io.StringIO("")
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.d.send.main()
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)
        finally:
            sys.argv, sys.stdin = old_argv, old_stdin

    def test_send_py_opens_a_board_from_a_spec_file(self):
        """send.py spec dosyasından tahta açıyor"""
        with tempfile.NamedTemporaryFile("w", suffix=".grid.json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(SPEC, f)
        code = self.run_send_py(["--battle-map", f.name, "--map-pos", "Dilaver:C4",
                                 "--map-pos", "Goblin:G4", "--map-hide", "Goblin"])
        self.assertEqual(code, 0)
        b = self.await_board(SEAT, lambda x: x and "Dilaver" in x["svg"])
        self.assertIsNotNone(b, "send.py'nin açtığı tahta gelmedi")
        self.assertNotIn("Goblin", b["svg"], "send.py --map-hide etkisiz")
        Path(f.name).unlink()

    def test_send_py_refuses_an_invalid_spec_before_sending(self):
        """send.py bozuk spec'i göndermeden reddediyor"""
        with tempfile.NamedTemporaryFile("w", suffix=".grid.json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"handle": "x", "cols": 40, "rows": 3}, f)
        code = self.run_send_py(["--battle-map", f.name])
        self.assertEqual(code, 2)
        self.settle()
        self.assertEqual(self.boards(SEAT), [], "geçersiz spec yine de yayınlandı")
        Path(f.name).unlink()


SECTIONS = [
    ("tahta geliyor", [
        "test_opening_a_board_reaches_every_seat",
        "test_an_invalid_spec_is_refused_with_reasons",
        "test_a_move_without_a_board_is_refused",
    ]),
    ("koltukların asla görmeyeceği şey", [
        "test_a_hidden_token_never_reaches_a_seat",
        "test_revealing_puts_it_on_every_screen",
        "test_a_late_joining_seat_gets_the_board_but_not_the_secret",
        "test_the_polling_paths_keep_the_secret_too",
    ]),
    ("tahta tahtalık yapıyor", [
        "test_a_move_redraws_the_token_where_it_went",
        "test_a_bad_or_off_grid_tile_is_refused",
        "test_party_names_are_pcs_and_others_are_npcs",
        "test_the_turn_ring_follows_the_turn_order",
        "test_unplaced_tokens_are_named_in_the_caption",
        "test_removing_takes_the_token_off_for_good",
        "test_clearing_closes_the_board_everywhere",
    ]),
    ("DM'in kendi komutu", [
        "test_send_py_opens_a_board_from_a_spec_file",
        "test_send_py_refuses_an_invalid_spec_before_sending",
    ]),
]


def report() -> int:
    return print_report(
        "SAVAŞ HARİTASI TESTİ · spec'ten çizilen tahta",
        f"{SPEC['handle']} · {SPEC['cols']}×{SPEC['rows']} · su, sütun, moloz · gizli token: {HIDDEN}",
        BattleMapLive, SECTIONS, "gizli goblin gizli kalıyor")


if __name__ == "__main__":
    sys.exit(report())
