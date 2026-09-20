"""The response window as the table meets it: buttons, races and the clock.

`test_response_window.py` covers the judgment — which feature a sheet yields,
whether a spend is legal, which die the follow-up rolls. None of that is what
breaks at the table. What breaks is the other half: an ally's button spending
someone else's Second Wind, a double-tapped Heroic Inspiration burning two
charges, one player's window wiping another's off the screen, a seat that
reconnects mid-countdown to no buttons at all, and a window that closes with
nobody ever told how the roll ended.

Every one of those is a property of the running server, so the server runs.
Jev is stubbed — deliberately, and only for the judgment: `follow_up` and
`roll_kind` are arithmetic and stay real, because which die a reroll uses is
exactly the kind of thing a stub would quietly get right while the code got
it wrong.

Run as a test:     python3 -m pytest tests/test_response_window_live.py
Run as a report:   python3 tests/test_response_window_live.py
"""

from __future__ import annotations

import sys
import time
import unittest

try:
    from tests.live_display import DM, DISPLAY, LiveDisplay, load_module, print_report
except ImportError:                       # run as a script from the repo root
    from live_display import DM, DISPLAY, LiveDisplay, load_module, print_report


ROLLER = "Dilaver"            # rolled and failed; holds Heroic Inspiration
ALLY = "Hisrayt"              # holds Guidance — an offer on someone else's roll
BYSTANDER = "Yapraksever"     # offers nothing; watches
PARTY = [ROLLER, ALLY, BYSTANDER]

# The roll under test, everywhere: an ability check that came up short.
LABEL = "Athletics — mahzen kapağını kaldırır"
SPEC, MODIFIER, TOTAL, DC = "1d20", 3, 8, 15

# One offer per resource branch, because what is under test is which counter
# gets read — not whether a bard really has these. The tags are chosen to reach
# every arm of `_can_afford`, not to settle a rules argument.
HI = f"hi:{ROLLER}"                    # heroic_inspiration — reroll, roller's own
TACTICAL = f"sheet:{ROLLER}:tactical"  # sinirli_kullanim — +1d10, counter declared
BARDIC = f"sheet:{ALLY}:bardic"        # sinirli_kullanim — +1d6, the ally's own
SLOTTED = f"sheet:{BYSTANDER}:slot"    # buyu_slotu + konsantrasyon — +1d4
UNTRACKED = f"sheet:{ROLLER}:untracked"  # sinirli_kullanim, no counter declared

# Long enough that no test races the clock, except the one that means to.
PATIENT = 60
IMPATIENT = 3


class JevStub:
    """The model's judgment, fixed. Its arithmetic, real.

    Hands back every offer as legal and lets the display decide what is still
    payable, because that decision is the thing these tests are about.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.calls: list[dict] = []

    def roll_kind(self, label):
        return self._real.roll_kind(label)

    def follow_up(self, *args, **kwargs):
        return self._real.follow_up(*args, **kwargs)

    def offers(self, campaign, roller, present, kind, passed, inspiration=None):
        self.calls.append({"roller": roller, "kind": kind, "passed": passed})
        out = []
        # Offered unconditionally on purpose: whether the roller is still
        # holding it is a counter, and reading counters is what is under test.
        if True:
            out.append({"id": f"hi:{roller}", "character": roller,
                        "feature": "Heroic Inspiration",
                        "detail": "Zarı yeniden at — yeni sonuç geçerli.",
                        "etki": "yeniden_at", "kaynak": "heroic_inspiration"})
        if roller == ROLLER:
            out.append({"id": TACTICAL, "character": ROLLER,
                        "feature": "Tactical Mind",
                        "detail": "Second Wind harca, zara 1d10 ekle.",
                        "etki": "1d10_ekle", "kaynak": "sinirli_kullanim",
                        "konsantrasyon": False})
            out.append({"id": UNTRACKED, "character": ROLLER,
                        "feature": "Gözüpek Atılım",
                        "detail": "Sayacı tutulmayan bir özellik.",
                        "etki": "sabit_ekle", "kaynak": "sinirli_kullanim"})
        if ALLY in present:
            out.append({"id": BARDIC, "character": ALLY,
                        "feature": "Bardic Inspiration",
                        "detail": "Tuttuğun zarı ekle.",
                        "etki": "1d6_ekle", "kaynak": "sinirli_kullanim"})
        if BYSTANDER in present:
            out.append({"id": SLOTTED, "character": BYSTANDER,
                        "feature": "Kök Bağı",
                        "detail": "Bir slot harca, teste 1d4 ekle.",
                        "etki": "1d4_ekle", "kaynak": "buyu_slotu",
                        "konsantrasyon": True})
        return out


class ResponseWindowLive(unittest.TestCase):
    """One server, one party, a fresh window per test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.d = LiveDisplay(module_suffix="respwin")
        cls.app = cls.d.app_mod
        cls.jev = JevStub(load_module(DISPLAY / "jev_window.py", "_respwin_real_jev"))
        cls.app._jev_window = cls.jev
        cls.seats = cls.d.open_seats(PARTY)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.d.close()

    def setUp(self) -> None:
        self.app.RESPONSE_WINDOW_SECONDS = PATIENT
        self.app._resp_windows.clear()
        # Reseat the party every test. Heroic Inspiration is a counter, and a
        # test that spends it would otherwise decide what the next test is
        # even offered — the order these run in is not ours to depend on.
        self.set_party()

    def set_party(self, **overrides) -> None:
        """Reseat the table, optionally changing one character's row.

        Every counter is restated every test: a test that spends a charge
        would otherwise decide what the next test is even offered, and the
        order these run in is not ours to depend on.
        """
        rows = {
            ROLLER: {"name": ROLLER, "inspiration": True, "conditions": [],
                     "uses": {"Second Wind": {"left": 2, "max": 2,
                                              "feeds": ["Tactical Mind"]}}},
            ALLY: {"name": ALLY, "inspiration": False, "conditions": [],
                   "uses": {"Bardic Inspiration": {"left": 3, "max": 3}}},
            BYSTANDER: {"name": BYSTANDER, "inspiration": False, "conditions": [],
                        "spell_slots": {"1": {"max": 3, "used": 0}}},
        }
        for name, patch in overrides.items():
            rows[name] = {**rows[name], **patch}
        self.d.set_party(list(rows.values()))

    # ── helpers ───────────────────────────────────────────────────────────

    def try_open_window(self, roller: str = ROLLER, total: int = TOTAL,
                        dc: "int | None" = DC, label: str = LABEL,
                        timeout: float = 5.0):
        """Open a window the way the dice endpoint does, and hand back the
        payload the table actually received — or None if none was sent."""
        meta = {"dc": dc, "label": label, "spec": SPEC,
                "modifier": MODIFIER, "advantage": "normal"}
        before = {w["window_id"] for w in self.seats[DM].payloads("response_window")}
        since = self.mark()
        self.app._open_response_window(roller, meta, total, "req-" + roller.lower())
        return self.seats[DM].await_payload(
            "response_window", lambda w: w["window_id"] not in before,
            timeout=timeout, since=since)

    def open_window(self, **kwargs) -> dict:
        window = self.try_open_window(**kwargs)
        self.assertIsNotNone(window, "pencere hiç yayınlanmadı")
        return window

    def offered(self, **party) -> set:
        """The offer ids a window carries, for a table in this state.

        Always reseats, overrides or not — the point of most of these is to
        compare a changed table against the default one, and a default that
        inherited the last test's spent charges compares nothing.
        """
        self.set_party(**party)
        return {o["id"] for o in self.open_window()["offers"]}

    def closures(self, window_id: str) -> list:
        return [c for c in self.seats[DM].payloads("response_window_closed")
                if c["window_id"] == window_id]

    def mark(self) -> int:
        """Where the DM seat's log stands right now. Every await below is
        asked from a mark, never over the whole run — see Seat.mark."""
        return self.seats[DM].mark()

    def feed_lines(self, since: int = 0) -> list:
        return [str(e.get("text", "")) for e in self.seats[DM].all_events(since)]

    def await_feed(self, needle: str, since: int = 0, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if any(needle in line for line in self.feed_lines(since)):
                return True
            time.sleep(0.05)
        return False

    def spend(self, window_id, offer_id, character):
        return self.d.post(f"/response-window/{window_id}/spend",
                           {"offer_id": offer_id, "character": character})

    def dice_requests(self) -> set:
        return {r["request_id"] for r in self.seats[DM].payloads("dice_request")}

    def spend_and_catch_roll(self, window_id, offer_id, character) -> dict:
        """Spend, then return the dice request that spend issued.

        By request id rather than by label: seats keep every event for the
        life of the server, and two spends of the same feature read identically
        from the outside."""
        before = self.dice_requests()
        since = self.mark()
        status, body = self.spend(window_id, offer_id, character)
        self.assertEqual(status, 200, f"harcama reddedildi: {body}")
        issued = self.seats[DM].await_payload(
            "dice_request", lambda r: r["request_id"] not in before, since=since)
        self.assertIsNotNone(issued, "harcama bir zar isteği doğurmadı")
        return issued

    # ── when a window opens at all ────────────────────────────────────────

    def test_a_failed_check_opens_the_window_on_every_screen(self):
        """başarısız test her ekranda pencereyi açıyor"""
        window = self.open_window()
        self.assertEqual(window["roller"], ROLLER)
        self.assertEqual({o["id"] for o in window["offers"]},
                         {HI, TACTICAL, UNTRACKED, BARDIC, SLOTTED})
        for name, seat in self.seats.items():
            with self.subTest(seat=name):
                got = seat.await_payload(
                    "response_window", lambda w: w["window_id"] == window["window_id"])
                self.assertIsNotNone(got, f"{name} pencereyi almadı")

    def test_a_passing_roll_opens_nothing(self):
        """geçen atış pencere açmıyor"""
        before = len(self.seats[DM].payloads("response_window"))
        meta = {"dc": DC, "label": LABEL, "spec": SPEC, "modifier": MODIFIER,
                "advantage": "normal"}
        self.app._open_response_window(ROLLER, meta, DC + 2, "req-pass")
        time.sleep(0.4)
        self.assertEqual(len(self.seats[DM].payloads("response_window")), before,
                         "kurtarılacak bir şey yokken pencere açıldı")

    def test_a_roll_with_no_dc_opens_nothing(self):
        """DC'siz atış pencere açmıyor"""
        before = len(self.seats[DM].payloads("response_window"))
        meta = {"dc": None, "label": LABEL, "spec": SPEC, "modifier": MODIFIER,
                "advantage": "normal"}
        self.app._open_response_window(ROLLER, meta, TOTAL, "req-nodc")
        time.sleep(0.4)
        self.assertEqual(len(self.seats[DM].payloads("response_window")), before,
                         "başarı ölçütü yokken pencere açıldı")

    # ── what the counters say ─────────────────────────────────────────────

    def test_a_spent_charge_is_not_offered_again(self):
        """biten kullanım bir daha teklif edilmiyor"""
        # Tactical Mind spends a Second Wind use, and the counter is named for
        # the resource rather than the feature — so this also pins the link.
        ids = self.offered(**{ROLLER: {"uses": {
            "Second Wind": {"left": 0, "max": 2, "feeds": ["Tactical Mind"]}}}})
        self.assertNotIn(TACTICAL, ids, "Second Wind bittiği hâlde teklif edildi")
        self.assertIn(BARDIC, ids, "başkasının sayacı da düştü")

    def test_inspiration_nobody_holds_is_not_offered(self):
        """elde olmayan ilham teklif edilmiyor"""
        self.assertNotIn(HI, self.offered(**{ROLLER: {"inspiration": False}}))
        self.assertIn(HI, self.offered())

    def test_an_incapacitated_holder_is_offered_nothing(self):
        """kendinde olmayan oyuncuya hiçbir şey teklif edilmiyor"""
        ids = self.offered(**{ALLY: {"conditions": ["Unconscious"]}})
        self.assertNotIn(BARDIC, ids, "bayılmış oyuncunun düğmesi duruyor")
        self.assertIn(TACTICAL, ids, "atanın kendi teklifleri de düştü")

    def test_a_slot_feature_needs_a_free_slot(self):
        """slot isteyen özellik boş slot istiyor"""
        drained = {"spell_slots": {"1": {"max": 3, "used": 3}}}
        self.assertNotIn(SLOTTED, self.offered(**{BYSTANDER: drained}))
        self.assertIn(SLOTTED, self.offered())

    def test_an_untracked_resource_is_not_refused(self):
        """sayacı tutulmayan kaynak reddedilmiyor"""
        # Refusing a legal feature costs the player the feature; offering one
        # they cannot pay costs a glance. Untracked has to fail open.
        self.assertIn(UNTRACKED, self.offered())

    def test_a_window_nobody_can_pay_for_never_opens(self):
        """kimsenin karşılayamadığı pencere hiç açılmıyor"""
        self.set_party(**{
            ROLLER: {"conditions": ["Unconscious"]},
            ALLY: {"conditions": ["Stunned"]},
            BYSTANDER: {"spell_slots": {"1": {"max": 3, "used": 3}}},
        })
        self.assertIsNone(self.try_open_window(timeout=1.5),
                          "basılamayacak düğmelerle pencere açıldı")

    def test_spending_a_charge_drops_the_counter(self):
        """kullanım harcanınca sayaç düşüyor"""
        w = self.open_window()
        since = self.mark()
        self.spend(w["window_id"], TACTICAL, ROLLER)
        stats = self.seats[DM].await_payload(
            "stats", since=since, match=lambda s: next(
                (p for p in s.get("players", []) if p["name"] == ROLLER), {}
            ).get("uses", {}).get("Second Wind", {}).get("left") == 1)
        self.assertIsNotNone(stats, "harcanan Second Wind sayaçta durmaya devam etti")

    # ── nobody holds two ──────────────────────────────────────────────────

    def offer_for(self, offer_id: str, **party) -> dict:
        self.set_party(**party)
        return next(o for o in self.open_window()["offers"] if o["id"] == offer_id)

    def test_a_concentration_offer_says_what_it_would_drop(self):
        """konsantrasyon teklifi neyi düşüreceğini söylüyor"""
        o = self.offer_for(SLOTTED, **{BYSTANDER: {"concentration": "Sarmaşık"}})
        self.assertEqual(o.get("drops_concentration"), "Sarmaşık")
        self.assertIn("Sarmaşık", o["detail"],
                      "bedel düğmede yazmıyor — okunmayan bedel bedel değil")

    def test_a_free_holder_gets_no_warning(self):
        """boştaki oyuncuya uyarı çıkmıyor"""
        o = self.offer_for(SLOTTED)
        self.assertNotIn("drops_concentration", o)

    def test_re_upping_the_same_effect_costs_nothing(self):
        """aynı etkiyi tazelemek bedelsiz"""
        o = self.offer_for(SLOTTED, **{BYSTANDER: {"concentration": "Kök Bağı"}})
        self.assertNotIn("drops_concentration", o)

    def test_spending_moves_the_concentration_instead_of_adding_one(self):
        """harcama konsantrasyonu taşıyor, üstüne eklemiyor"""
        self.set_party(**{BYSTANDER: {
            "concentration": "Sarmaşık",
            "effects": [{"name": "Sarmaşık", "concentration": True}]}})
        w = self.open_window()
        since = self.mark()
        self.spend(w["window_id"], SLOTTED, BYSTANDER)
        row = self.seats[DM].await_payload(
            "stats", since=since, match=lambda s: next(
                (p for p in s.get("players", []) if p["name"] == BYSTANDER), {}
            ).get("concentration") == "Kök Bağı")
        self.assertIsNotNone(row, "konsantrasyon yeni etkiye geçmedi")
        held = next(p for p in row["players"] if p["name"] == BYSTANDER)
        self.assertEqual([e["name"] for e in held.get("effects", [])], [],
                         "eski konsantrasyon etkisi duruyor — ikisi birden tutuluyor")

    def test_the_dropped_concentration_is_announced(self):
        """düşen konsantrasyon masaya duyuruluyor"""
        self.set_party(**{BYSTANDER: {"concentration": "Sarmaşık"}})
        w = self.open_window()
        since = self.mark()
        self.spend(w["window_id"], SLOTTED, BYSTANDER)
        self.assertTrue(self.await_feed("Sarmaşık", since=since),
                        "konsantrasyon sessizce düştü")

    # ── whose button is whose ─────────────────────────────────────────────

    def test_an_offer_can_only_be_spent_by_its_owner(self):
        """teklifi yalnızca sahibi harcayabiliyor"""
        w = self.open_window()
        status, body = self.spend(w["window_id"], BARDIC, ROLLER)
        self.assertEqual(status, 403, f"başkasının teklifi harcandı: {body}")
        self.assertIn(w["window_id"], self.app._resp_windows,
                      "reddedilen basış pencereyi yine de kapattı")

    def test_an_unknown_offer_is_refused(self):
        """tanınmayan teklif reddediliyor"""
        w = self.open_window()
        status, _ = self.spend(w["window_id"], "sheet:Dilaver:yok", ROLLER)
        self.assertEqual(status, 404)
        self.assertIn(w["window_id"], self.app._resp_windows)

    def test_the_second_press_finds_the_window_closed(self):
        """ikinci basış pencereyi kapalı buluyor"""
        w = self.open_window()
        first, _ = self.spend(w["window_id"], TACTICAL, ROLLER)
        second, body = self.spend(w["window_id"], TACTICAL, ROLLER)
        self.assertEqual(first, 200)
        self.assertEqual(second, 409, f"aynı özellik iki kez harcandı: {body}")
        self.assertEqual(len(self.closures(w["window_id"])), 1,
                         "pencere iki kez kapandı")

    # ── what a spend actually does ────────────────────────────────────────

    def test_spending_inspiration_drops_the_counter(self):
        """ilham harcanınca sayaç düşüyor"""
        w = self.open_window()
        since = self.mark()
        status, _ = self.spend(w["window_id"], HI, ROLLER)
        self.assertEqual(status, 200)
        stats = self.seats[DM].await_payload(
            "stats", since=since, match=lambda s: not next(
                (p for p in s.get("players", []) if p["name"] == ROLLER), {}
            ).get("inspiration", False))
        self.assertIsNotNone(stats, "ilham hâlâ duruyor — sonraki atışta yine teklif edilir")

    def test_a_bonus_die_is_rolled_by_whoever_owns_the_feature(self):
        """ek zarı özelliğin sahibi atıyor"""
        w = self.open_window()
        req = self.spend_and_catch_roll(w["window_id"], BARDIC, ALLY)
        self.assertEqual(req["characters"], [ALLY],
                         "zarı tutan değil, atan atıyor")
        self.assertEqual(req["spec"], "1d6")

    def test_a_reroll_replaces_the_rollers_own_die(self):
        """yeniden atışı atan kendi zarıyla yapıyor"""
        w = self.open_window()
        req = self.spend_and_catch_roll(w["window_id"], HI, ROLLER)
        self.assertEqual(req["characters"], [ROLLER])
        self.assertEqual(req["spec"], SPEC, "yeniden atış özgün zarı kullanmıyor")
        self.assertEqual(req["dc"], DC, "yeniden atış aynı DC'ye karşı değil")

    def test_a_seat_in_full_view_rolls_on_its_own_screen(self):
        """bağlı koltuk zarı kendi ekranında atıyor"""
        # This table plays remote: every player runs the full display bound to
        # their own character, so nobody is ever "absent" and the shared screen
        # must not roll on their behalf. An unbound name is the other case.
        w = self.open_window()
        req = self.spend_and_catch_roll(w["window_id"], BARDIC, ALLY)
        self.assertEqual(req["onscreen_targets"], [],
                         "bağlı koltuk yokmuş gibi ele alındı")

        absent, _ = self.d.post("/dice-request", {
            "characters": ["Kızıl Zenci"], "spec": "1d20", "label": "kontrol"})
        stray = self.seats[DM].await_payload(
            "dice_request", lambda r: r.get("label") == "kontrol")
        self.assertEqual(stray["onscreen_targets"], ["Kızıl Zenci"],
                         "hiç bağlı olmayan isim için ekranda atılmıyor")

    def test_the_spend_is_announced_on_the_feed(self):
        """harcama akışta duyuruluyor"""
        w = self.open_window()
        since = self.mark()
        self.spend(w["window_id"], TACTICAL, ROLLER)
        self.assertTrue(self.await_feed("Tactical Mind", since=since),
                        "DM, neye karşı anlatacağını akışta görmüyor")

    # ── giving the seconds back ───────────────────────────────────────────

    def test_only_the_roller_may_pass(self):
        """pencereyi yalnızca atan kapatabiliyor"""
        w = self.open_window()
        status, _ = self.d.post(f"/response-window/{w['window_id']}/pass",
                                {"character": ALLY})
        self.assertEqual(status, 403, "müttefik başkasının kararını bitirdi")
        self.assertIn(w["window_id"], self.app._resp_windows)

        status, _ = self.d.post(f"/response-window/{w['window_id']}/pass",
                                {"character": ROLLER})
        self.assertEqual(status, 200)
        self.assertNotIn(w["window_id"], self.app._resp_windows)

    def test_a_window_nobody_used_still_reports_the_outcome(self):
        """kimsenin kullanmadığı pencere yine de sonucu bildiriyor"""
        w = self.open_window()
        since = self.mark()
        self.d.post(f"/response-window/{w['window_id']}/pass", {"character": ROLLER})
        self.assertTrue(self.await_feed("kimse bir şey harcamadı", since=since),
                        "DM sonucu bekliyor ve kimse söylemedi")

    def test_the_clock_closes_the_window_by_itself(self):
        """süre dolunca pencere kendi kapanıyor"""
        self.app.RESPONSE_WINDOW_SECONDS = IMPATIENT
        w = self.open_window()
        end = time.time() + IMPATIENT + 4
        while time.time() < end and not self.closures(w["window_id"]):
            time.sleep(0.1)
        closed = self.closures(w["window_id"])
        self.assertTrue(closed, "süre doldu, pencere ekranlarda kaldı")
        self.assertEqual(closed[0]["reason"], "timeout")

    # ── four players roll at once ─────────────────────────────────────────

    def test_one_window_closing_leaves_the_other_open(self):
        """bir pencere kapanınca diğeri açık kalıyor"""
        mine = self.open_window(roller=ROLLER)
        theirs = self.open_window(roller=ALLY, label="Perception — sesi dinler")
        self.spend(mine["window_id"], TACTICAL, ROLLER)
        time.sleep(0.4)
        self.assertEqual(self.closures(theirs["window_id"]), [],
                         "başkasının penceresi de kapandı")
        self.assertIn(theirs["window_id"], self.app._resp_windows)

    def test_a_reconnecting_seat_gets_its_buttons_back(self):
        """yeniden bağlanan koltuk düğmelerini geri alıyor"""
        w = self.open_window()
        rejoin = self.d.transient_seat(f"{ROLLER} (yeniden bağlanan)", character=ROLLER)
        restored = rejoin.await_payload(
            "response_window", lambda x: x["window_id"] == w["window_id"])
        self.assertIsNotNone(restored, "sayım sürerken bağlanan koltuk düğmesiz kaldı")
        self.assertGreater(restored["remaining"], 0)
        self.assertLessEqual(restored["remaining"], float(PATIENT))
        rejoin.close()

    def test_a_closed_window_is_not_replayed_to_a_late_joiner(self):
        """kapanmış pencere geç katılana gösterilmiyor"""
        w = self.open_window()
        self.d.post(f"/response-window/{w['window_id']}/pass", {"character": ROLLER})
        late = self.d.transient_seat(f"{ROLLER} (geç katılan)", character=ROLLER)
        time.sleep(0.4)
        self.assertEqual(
            [x for x in late.payloads("response_window")
             if x["window_id"] == w["window_id"]], [],
            "bitmiş bir karar yeniden açıldı")
        late.close()


# The report reads in the order the table meets these rules, which is not the
# alphabetical order unittest runs them in — so the order is written down.
SECTIONS = [
    ("pencere ne zaman açılır", [
        "test_a_failed_check_opens_the_window_on_every_screen",
        "test_a_passing_roll_opens_nothing",
        "test_a_roll_with_no_dc_opens_nothing",
    ]),
    ("teklifi ne karşılıyor", [
        "test_a_spent_charge_is_not_offered_again",
        "test_inspiration_nobody_holds_is_not_offered",
        "test_an_incapacitated_holder_is_offered_nothing",
        "test_a_slot_feature_needs_a_free_slot",
        "test_an_untracked_resource_is_not_refused",
        "test_a_window_nobody_can_pay_for_never_opens",
        "test_spending_a_charge_drops_the_counter",
    ]),
    ("kimse iki tane tutamaz", [
        "test_a_concentration_offer_says_what_it_would_drop",
        "test_a_free_holder_gets_no_warning",
        "test_re_upping_the_same_effect_costs_nothing",
        "test_spending_moves_the_concentration_instead_of_adding_one",
        "test_the_dropped_concentration_is_announced",
    ]),
    ("hangi düğme kimin", [
        "test_an_offer_can_only_be_spent_by_its_owner",
        "test_an_unknown_offer_is_refused",
        "test_the_second_press_finds_the_window_closed",
    ]),
    ("basınca ne oluyor", [
        "test_spending_inspiration_drops_the_counter",
        "test_a_bonus_die_is_rolled_by_whoever_owns_the_feature",
        "test_a_reroll_replaces_the_rollers_own_die",
        "test_a_seat_in_full_view_rolls_on_its_own_screen",
        "test_the_spend_is_announced_on_the_feed",
    ]),
    ("saniyeleri geri vermek", [
        "test_only_the_roller_may_pass",
        "test_a_window_nobody_used_still_reports_the_outcome",
        "test_the_clock_closes_the_window_by_itself",
    ]),
    ("dört kişi aynı anda atınca", [
        "test_one_window_closing_leaves_the_other_open",
        "test_a_reconnecting_seat_gets_its_buttons_back",
        "test_a_closed_window_is_not_replayed_to_a_late_joiner",
    ]),
]


def report() -> int:
    return print_report(
        "CEVAP PENCERESİ TESTİ · zar düştükten sonraki saniyeler",
        f"{ROLLER} — {LABEL} · {TOTAL} vs DC {DC} — kaldı · Heroic Inspiration · "
        f"Tactical Mind (Second Wind) · Bardic Inspiration ({ALLY}) · Kök Bağı ({BYSTANDER}, slot)",
        ResponseWindowLive, SECTIONS, "pencere kimseye başkasının kararını verdirmiyor")


if __name__ == "__main__":
    sys.exit(report())
