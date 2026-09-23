"""The turn linter, fed the way the display feeds it.

Two tiers, two kinds of test. The pattern tier is checked end to end: a line
goes in through /chunk with the server in whatever state the rule depends on
(a dice request open, a turn order set) and the log either gains the finding
or does not. The judgment tier stubs Jev — but not the way that would make the
test pointless. What is asserted is which question was asked and with what
state: that a public line after a whisper is checked against that whisper,
that a line during an open roll is checked for implying its outcome, and that
a private line is filed as knowledge and never judged at all. The model's
answer is fixed; the question it was handed is the thing under test.

Run as a test:     python3 -m pytest tests/test_turn_lint_live.py
Run as a report:   python3 tests/test_turn_lint_live.py
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

try:
    from tests.live_display import LiveDisplay, print_report
except ImportError:
    from live_display import LiveDisplay, print_report


PARTY = ["Dilaver", "Hisrayt", "Yapraksever"]

LONG = ("Kapak ağır ağır kalkıyor ve altından soğuk bir hava yükseliyor. Merdiven "
        "basamakları ıslak, duvarlarda yosun var, aşağıdan su damlama sesi geliyor. "
        "Dilaver ilk adımı atıyor, meşale titriyor, gölgeler duvarda uzuyor. Hisrayt "
        "arkasından geliyor, elini kılıcının kabzasında tutuyor. Yapraksever en son "
        "giriyor ve kapağı yarı açık bırakıyor, geri dönüş yolu kapanmasın diye.")   # 50+ words


class JevStub:
    """Answers whatever it is asked with fixed confidences, and remembers."""

    def __init__(self) -> None:
        self.calls: list = []
        self.answers = {"sonuc_imasi": 0.2, "bilgi_sizintisi": 0.9, "roman_registeri": 0.3,
                        "tahtada_hareket": 0.8, "npc_hukmu": 0.1, "dusman_can": 0.1,
                        "zar_oynandi": 0.9}

    def __call__(self, state, questions, timeout=None):
        self.calls.append({"state": state, "questions": dict(questions)})
        # zar_oynandi_0, _1, … share one answer: the tests are about which
        # rolls get asked, not about telling them apart.
        return {qid: {"noul": self.answers.get(
                    "zar_oynandi" if qid.startswith("zar_oynandi_") else qid, 0.0)}
                for qid in questions}


class TurnLintLive(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.d = LiveDisplay(module_suffix="lint")
        cls.app = cls.d.app_mod
        cls.d.set_party(PARTY)
        cls.d.open_seats([])            # a DM seat is enough; the log is the oracle
        cls.linter = cls.app._lint()
        assert cls.linter is not None, "linter did not load"
        cls.jev = JevStub()
        cls.linter._ask = cls.jev
        cls.log = cls.d.campaign_dir / ".lint-log.jsonl"
        cls.state_md = cls.d.campaign_dir / "state.md"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.d.close()

    def setUp(self) -> None:
        self.log.write_text("", encoding="utf-8")
        self.state_md.write_text("## Session Flags\n- roll_mode: players\n", encoding="utf-8")
        self.jev.calls.clear()
        with self.linter._lock:
            self.linter._open.clear()
            self.linter._private.clear()
            self.linter._rolls.clear()
            self.linter._played.clear()
        self.jev.answers["zar_oynandi"] = 0.9
        self.d.post("/stats", {"turn_order": None})
        self.d.post("/battle-map", {"clear": True})

    # ── helpers ───────────────────────────────────────────────────────────

    def send(self, text: str, **fields) -> None:
        status, body = self.d.post("/chunk", {"text": text, **fields})
        self.assertEqual(status, 204, body)

    def findings(self, rule: str = "", wait: float = 1.5) -> list:
        """Findings in the log, optionally one rule's — waits for the
        judgment thread when asked for a judgment rule."""
        end = time.time() + wait
        while True:
            rows = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]
            if rule:
                rows = [r for r in rows if r["rule"] == rule]
            if rows or time.time() > end:
                return rows
            time.sleep(0.05)

    def open_request(self, character="Dilaver", dc=15, label="Athletics — kapağı kaldırır"):
        status, body = self.d.post("/dice-request", {
            "characters": [character], "spec": "1d20", "modifier": 3, "label": label, "dc": dc})
        self.assertEqual(status, 200, body)
        return body.get("request_id") or body.get("id")

    def settle(self) -> None:
        time.sleep(0.4)

    # ── the mechanical tier ───────────────────────────────────────────────

    def test_a_dc_in_narration_is_logged(self):
        """anlatımdaki DC log'a düşüyor"""
        self.send("Kilit eski ama sağlam. DC 15'lik bir iş bu.")
        f = self.findings("dc_leak")
        self.assertTrue(f, "DC sızıntısı yakalanmadı")
        self.assertIn("DC 15", f[0]["detail"])

    def test_the_turkish_spelling_counts_too(self):
        """Türkçe yazımı da sayılıyor"""
        self.send("Zorluk derecesi 13, dikkatli ol.")
        self.assertTrue(self.findings("dc_leak"))

    def test_a_leaked_dc_is_matched_to_the_open_request(self):
        """sızan DC açık istekle eşleştiriliyor"""
        self.open_request(dc=17)
        self.send("Bu DC 17, kolay değil.")
        f = self.findings("dc_leak")
        self.assertIn("açık isteğin DC'si", f[0]["detail"])

    def test_a_rote_closer_is_logged(self):
        """kalıp kapanış log'a düşüyor"""
        self.send("Kapı gıcırdayarak açılıyor. İçerisi karanlık. Ne yapıyorsun?")
        self.assertTrue(self.findings("rote_closer"))
        self.log.write_text("", encoding="utf-8")
        self.send("Kapı gıcırdayarak açılıyor. İçerisi karanlık.")
        self.assertFalse(self.findings("rote_closer", wait=0.3))

    def test_a_closing_menu_is_logged(self):
        """seçenek listesiyle kapanan tur log'a düşüyor"""
        self.send("Kapı aralık, içeriden bir mum ışığı sızıyor.\n\n"
                  "1. İçeri gir\n2. Kapıyı dinle\n3. Geri dön")
        f = self.findings("options_menu")
        self.assertTrue(f)
        self.assertIn("3", f[0]["detail"])

    def test_a_list_in_the_middle_is_not_a_menu(self):
        """ortadaki liste menü değil"""
        self.send("Sandıkta şunlar var:\n- bir ip\n- iki mum\n"
                  "Sandığın dibinde ise bir şey kıpırdıyor.")
        self.assertFalse(self.findings("options_menu", wait=0.3))

    def test_an_enemy_hp_number_is_logged(self):
        """düşman canı sayıyla verilince log'a düşüyor"""
        self.send("Goblin sendeliyor. Sadece 3 HP'si kaldı.")
        f = self.findings("enemy_hp")
        self.assertTrue(f)
        self.assertIn("3 HP", f[0]["detail"])

    def test_a_pcs_own_hp_is_not_an_enemy_leak(self):
        """PC'nin kendi canı düşman sızıntısı değil"""
        self.send("Dilaver 12 HP'ye düşüyor, nefesi kesiliyor.")
        self.assertFalse(self.findings("enemy_hp", wait=0.3))

    def test_narration_during_an_open_roll_is_logged(self):
        """açık zar isteği sırasında anlatım log'a düşüyor"""
        self.open_request()
        self.send(LONG)
        f = self.findings("roll_not_final")
        self.assertTrue(f, "zar isteği açıkken uzun anlatım yakalanmadı")
        self.assertIn("Dilaver", f[0]["detail"])

    def test_a_short_tail_after_a_request_is_allowed(self):
        """istekten sonra kısa kuyruk serbest"""
        self.open_request()
        self.send("Kapağa uzanıyorsun. …ya da başka bir şey mi denersin?")
        self.assertFalse(self.findings("roll_not_final", wait=0.3))

    def test_a_resolved_request_no_longer_binds(self):
        """çözülen istek artık bağlamıyor"""
        rid = self.open_request()
        self.d.post(f"/dice-request/{rid}", {})          # POST is not DELETE — use the real verb
        import urllib.request
        req = urllib.request.Request(f"{self.d.base}/dice-request/{rid}", method="DELETE")
        urllib.request.urlopen(req, timeout=5).read()
        self.send(LONG)
        self.assertFalse(self.findings("roll_not_final", wait=0.3),
                         "iptal edilen istek hâlâ anlatımı bağlıyor")

    def test_a_long_narration_in_a_fight_is_logged(self):
        """savaşta uzun anlatım log'a düşüyor"""
        self.d.post("/stats", {"turn_order": {"current": "Dilaver", "order": PARTY}})
        self.send((LONG + " ") * 3)
        self.assertTrue(self.findings("length_heat"))
        self.log.write_text("", encoding="utf-8")
        self.d.post("/stats", {"turn_order": None})
        self.send((LONG + " ") * 3)
        self.assertFalse(self.findings("length_heat", wait=0.3), "savaş dışında da uzunluk kuralı işledi")

    def test_a_d20_rolled_for_a_pc_is_logged_under_players_mode(self):
        """players modunda PC adına atılan d20 log'a düşüyor"""
        self.send("Dilaver rolls 1d20+2: [6] +2 = 8 — Perception", dice=True)
        f = self.findings("pc_auto_roll")
        self.assertTrue(f)
        self.assertIn("dilaver", f[0]["detail"].lower())

    def test_tutor_and_player_blocks_are_never_the_dms_fault(self):
        """tutor ve oyuncu blokları DM'e yazılmıyor"""
        self.send("Bu bir DC 15 Athletics testi olur.", tutor=True)
        self.send("DC 15 mi dedin?", player="Hisrayt")
        self.settle()
        self.assertEqual(self.findings(wait=0.2), [])

    def test_the_campaign_can_switch_it_off(self):
        """kampanya kapatabiliyor"""
        self.state_md.write_text("## Session Flags\n- turn_lint: off\n", encoding="utf-8")
        self.send("DC 15. Ne yapıyorsun?")
        self.settle()
        self.assertEqual(self.findings(wait=0.2), [])

    # ── the board and the words about it ──────────────────────────────────

    SPEC = {"handle": "kavran", "cols": 8, "rows": 6,
            "terrain": [{"tiles": "D3-E3", "kind": "moloz", "difficult": True}]}

    def open_board(self, **extra) -> None:
        """Open a board and let the opening narration spend the placement.

        Placing everyone at combat start is itself a write, so the scene-setting
        line that follows it is covered — as it should be. Tests about a *later*
        turn start after that line, which is where a fight actually is.
        """
        self.d.post("/battle-map", {"spec": self.SPEC, "round": 1,
                                    "pos": {"Dilaver": "B2", "Goblin": "G4"}, **extra})
        time.sleep(0.2)
        self.send("Kapı ardına kadar açık, içerisi moloz dolu. Goblin ocağın yanında duruyor.")
        self.settle()
        self.log.write_text("", encoding="utf-8")
        self.jev.calls.clear()

    def asked_move(self):
        return next((c for c in self.jev.calls if "tahtada_hareket" in c["questions"]), None)

    def test_an_unwritten_token_puts_the_question_to_jev(self):
        """konumu yazılmayan token soruyu Jev'e taşıyor"""
        self.open_board()
        self.send("Öteki goblin bir adım geri atıyor ve ilk kez arkasına bakıyor.")
        self.settle()
        call = self.asked_move()
        self.assertIsNotNone(call, "hareket sorusu sorulmadı")
        # Not gated on the name: the line says "öteki goblin", the token is "Goblin".
        self.assertIn("Goblin", call["state"]["tahtada_duranlar"])
        self.assertTrue(self.findings("tahtada_hareket"), "stub 0.8 dedi, bulgu düşmedi")

    def test_a_token_the_dm_just_moved_is_not_asked_about(self):
        """DM'in az önce oynattığı token sorulmuyor"""
        self.open_board()
        self.d.post("/battle-map", {"pos": {"Goblin": "F4"}})
        time.sleep(0.2)
        self.send("Goblin bir adım geri atıyor ve ilk kez arkasına bakıyor.")
        self.settle()
        call = self.asked_move()
        self.assertNotIn("Goblin", (call or {}).get("state", {}).get("tahtada_duranlar", []))

    def test_a_removed_token_counts_as_written(self):
        """tahtadan kaldırılan token yazılmış sayılıyor"""
        self.open_board()
        self.d.post("/battle-map", {"remove": ["Goblin"]})
        time.sleep(0.2)
        self.send("Goblin dizlerinin üstüne çöküp yan yatıyor, bıçağı taşlara düşüyor.")
        self.settle()
        call = self.asked_move()
        self.assertNotIn("Goblin", (call or {}).get("state", {}).get("tahtada_duranlar", []))

    def test_nothing_is_asked_when_no_board_is_open(self):
        """tahta yokken soru sorulmuyor"""
        self.d.post("/battle-map", {"clear": True})
        time.sleep(0.2)
        self.send("Goblin bir adım geri atıyor ve ilk kez arkasına bakıyor.")
        self.settle()
        self.assertIsNone(self.asked_move(), "tahta yokken hareket soruldu")

    def test_the_move_credit_is_spent_by_one_narration(self):
        """hareket kredisi tek anlatımda harcanıyor"""
        self.open_board()
        self.d.post("/battle-map", {"pos": {"Goblin": "F4"}})
        time.sleep(0.2)
        self.send("Goblin geri çekiliyor.")               # covered by the move
        self.settle()
        self.jev.calls.clear()
        self.send("Goblin bir kez daha geri çekiliyor.")  # nothing written for this one
        self.settle()
        call = self.asked_move()
        self.assertIsNotNone(call, "ikinci anlatım hiç sorulmadı")
        self.assertIn("Goblin", call["state"]["tahtada_duranlar"])

    # ── the judgment tier ─────────────────────────────────────────────────

    def test_a_public_line_after_a_whisper_is_checked_against_it(self):
        """fısıltıdan sonraki açık satır ona karşı sınanıyor"""
        self.send("Madalyonun arkasında Sitler'in mührü var.", to="Dilaver")
        self.send("Dilaver madalyonu cebine koyuyor; Sitler'in mührü hâlâ aklında.")
        f = self.findings("bilgi_sizintisi")
        self.assertTrue(f, "sızıntı sorusu sorulmadı ya da log'a düşmedi")
        self.assertEqual(f[0]["confidence"], 0.9)
        call = next(c for c in self.jev.calls if "bilgi_sizintisi" in c["questions"])
        gizli = call["state"]["gizli"]
        self.assertEqual(gizli[0]["kime"], "Dilaver")
        self.assertIn("Sitler", gizli[0]["metin"])

    def test_a_private_line_is_knowledge_not_a_violation(self):
        """özel satır ihlal değil bilgi"""
        # A whisper is filed as what its recipient now knows. It is never
        # checked for leaking (it is the secret) or for pre-empting a roll
        # (it is addressed, not narrated to the table). It is still DM prose,
        # so the register question may be asked of it like any other line.
        self.open_request()
        self.send("Yalnızca sen fark ediyorsun: basamakta taze bir ayak izi var, senin ölçünde.",
                  to="Dilaver")
        self.settle()
        asked = {q for c in self.jev.calls for q in c["questions"]}
        self.assertNotIn("bilgi_sizintisi", asked, "özel satır kendi kendine sızıntı diye soruldu")
        self.assertNotIn("sonuc_imasi", asked, "adresli satır zar iması diye soruldu")
        self.assertEqual(self.findings(wait=0.2), [])
        with self.linter._lock:
            self.assertEqual([t for _, t, _ in self.linter._private], ["Dilaver"])

    def test_narration_during_an_open_roll_is_checked_for_its_outcome(self):
        """açık zar sırasında anlatım sonuç iması için sınanıyor"""
        self.open_request(label="Athletics — kapağı kaldırır")
        self.send("Kapağa uzanıyorsun, parmakların kenarı buluyor, sırtın geriliyor.")
        self.settle()
        call = next((c for c in self.jev.calls if "sonuc_imasi" in c["questions"]), None)
        self.assertIsNotNone(call, "sonuç iması sorusu sorulmadı")
        self.assertEqual(call["state"]["acik_atis"][0]["karakter"], "Dilaver")
        self.assertIn("kapağı", call["state"]["acik_atis"][0]["etiket"])

    def test_a_confident_no_is_not_a_finding(self):
        """emin 'hayır' bulgu değil"""
        self.open_request()
        self.send("Kapağa uzanıyorsun, parmakların kenarı buluyor, sırtın geriliyor.")
        self.settle()
        self.assertEqual(self.findings("sonuc_imasi", wait=0.3), [],
                         "0.2 güvenle cevaplanan soru log'a düştü")

    def test_long_narration_is_checked_for_register(self):
        """uzun anlatım register için sınanıyor"""
        self.send(LONG)
        self.settle()
        self.assertTrue(any("roman_registeri" in c["questions"] for c in self.jev.calls))

    def test_narration_is_asked_for_an_npc_verdict_and_npc_lines_are_not(self):
        """anlatım NPC hükmü için sınanıyor, NPC repliği sınanmıyor"""
        self.send("Muhtar gözlerini kaçırıyor ve defteri usulca kapatıyor.")
        self.settle()
        self.assertTrue(any("npc_hukmu" in c["questions"] for c in self.jev.calls))
        self.jev.calls.clear()
        self.send("O adam yalancının teki, ona sakın güvenme!", npc="Muhtar")
        self.settle()
        self.assertFalse(any("npc_hukmu" in c["questions"] for c in self.jev.calls),
                         "NPC'nin kendi suçlaması anlatıcı hükmü diye soruldu")

    def test_the_enemy_hp_question_waits_for_a_fight(self):
        """düşman canı sorusu savaşı bekliyor"""
        self.send("Goblin sendeliyor, kolundan kan akıyor, zor ayakta duruyor.")
        self.settle()
        self.assertFalse(any("dusman_can" in c["questions"] for c in self.jev.calls),
                         "savaş yokken düşman canı soruldu")
        self.d.post("/stats", {"turn_order": {"current": "Dilaver", "order": PARTY + ["Goblin"]}})
        self.send("Goblin sendeliyor, kolundan kan akıyor, zor ayakta duruyor.")
        self.settle()
        self.assertTrue(any("dusman_can" in c["questions"] for c in self.jev.calls))

    # ── dice the prose has to play ────────────────────────────────────────

    HIT = "Goblin attacks: d20+4 = 19 vs AC 15 — hit! 1d6+2 = 6 piercing"

    def asked_rolls(self):
        return next((c for c in self.jev.calls
                     if any(q.startswith("zar_oynandi_") for q in c["questions"])), None)

    def test_a_roll_is_asked_about_once_its_turn_is_over(self):
        """zar, turu bitince soruluyor"""
        self.send(self.HIT, dice=True)
        self.send("Goblin hançerini savuruyor; Dilaver son anda geri çekiliyor.")
        self.settle()
        self.assertIsNone(self.asked_rolls(), "tur bitmeden zar soruldu")
        self.send("Dilaver karşılık veriyor.", player="Dilaver")
        self.settle()
        call = self.asked_rolls()
        self.assertIsNotNone(call, "tur bitince zar sorulmadı")
        self.assertIn("d20+4 = 19", call["state"]["zarlar"][0])
        self.assertIn("geri çekiliyor", call["state"]["anlatim"])

    def test_a_dropped_roll_is_logged(self):
        """oynanmayan zar log'a düşüyor"""
        self.jev.answers["zar_oynandi"] = 0.1
        self.send(self.HIT, dice=True)
        self.send("Goblin hançerini savuruyor; Dilaver son anda geri çekiliyor.")
        self.send("Orc attacks: d20+5 = 8 vs AC 15 — miss", dice=True)
        f = self.findings("zar_dusuruldu")
        self.assertTrue(f, "stub 0.1 dedi, bulgu düşmedi")
        self.assertIn("d20+4", f[0]["detail"])
        self.assertEqual(f[0]["kind"], "narration")

    def test_a_played_roll_is_not_a_finding(self):
        """oynanan zar bulgu değil"""
        self.send(self.HIT, dice=True)
        self.send("Hançer Dilaver'in omzuna saplanıyor, zırhın altından kan sızıyor.")
        self.send("Dilaver dişlerini sıkıyor.", player="Dilaver")
        self.settle()
        self.assertIsNotNone(self.asked_rolls())
        self.assertEqual(self.findings("zar_dusuruldu", wait=0.3), [])

    def test_every_line_of_the_turn_can_play_the_roll(self):
        """turdaki her satır zarı oynayabilir"""
        self.send(self.HIT, dice=True)
        self.send("Goblin öne atılıyor, hançeri parlıyor.")
        self.send("Al bakalım, kahraman!", npc="Goblin")
        self.send("Hisrayt yaklaşıyor.", player="Hisrayt")
        self.settle()
        prose = self.asked_rolls()["state"]["anlatim"]
        self.assertIn("öne atılıyor", prose)
        self.assertIn("kahraman", prose)

    def test_dice_with_no_prose_after_them_wait(self):
        """ardından anlatım gelmeyen zar bekliyor"""
        self.send(self.HIT, dice=True)
        self.send("Orc attacks: d20+5 = 8 vs AC 15 — miss", dice=True)
        self.settle()
        self.assertIsNone(self.asked_rolls(), "anlatım yokken zar soruldu")
        with self.linter._lock:
            self.assertEqual(len(self.linter._rolls), 2)

    def test_the_log_is_readable_from_the_display(self):
        """log display'den okunabiliyor"""
        self.send("DC 15.")
        self.findings("dc_leak")
        data = self.d.get("/lint", n=5)
        self.assertTrue(any(f["rule"] == "dc_leak" for f in data["findings"]))


SECTIONS = [
    ("kalıp — anında", [
        "test_a_dc_in_narration_is_logged",
        "test_the_turkish_spelling_counts_too",
        "test_a_leaked_dc_is_matched_to_the_open_request",
        "test_a_rote_closer_is_logged",
        "test_a_closing_menu_is_logged",
        "test_a_list_in_the_middle_is_not_a_menu",
        "test_an_enemy_hp_number_is_logged",
        "test_a_pcs_own_hp_is_not_an_enemy_leak",
        "test_narration_during_an_open_roll_is_logged",
        "test_a_short_tail_after_a_request_is_allowed",
        "test_a_resolved_request_no_longer_binds",
        "test_a_long_narration_in_a_fight_is_logged",
        "test_a_d20_rolled_for_a_pc_is_logged_under_players_mode",
        "test_tutor_and_player_blocks_are_never_the_dms_fault",
        "test_the_campaign_can_switch_it_off",
    ]),
    ("tahta ile sözün uyumu", [
        "test_an_unwritten_token_puts_the_question_to_jev",
        "test_a_token_the_dm_just_moved_is_not_asked_about",
        "test_a_removed_token_counts_as_written",
        "test_nothing_is_asked_when_no_board_is_open",
        "test_the_move_credit_is_spent_by_one_narration",
    ]),
    ("yargı — Jev'e ne soruluyor", [
        "test_a_public_line_after_a_whisper_is_checked_against_it",
        "test_a_private_line_is_knowledge_not_a_violation",
        "test_narration_during_an_open_roll_is_checked_for_its_outcome",
        "test_a_confident_no_is_not_a_finding",
        "test_long_narration_is_checked_for_register",
        "test_narration_is_asked_for_an_npc_verdict_and_npc_lines_are_not",
        "test_the_enemy_hp_question_waits_for_a_fight",
        "test_the_log_is_readable_from_the_display",
    ]),
    ("zar ile sözün uyumu", [
        "test_a_roll_is_asked_about_once_its_turn_is_over",
        "test_a_dropped_roll_is_logged",
        "test_a_played_roll_is_not_a_finding",
        "test_every_line_of_the_turn_can_play_the_roll",
        "test_dice_with_no_prose_after_them_wait",
    ]),
]


def report() -> int:
    return print_report(
        "TUR LİNTER TESTİ · masaya ulaşan satırın denetimi",
        "sadece log · kalıplar anında, yargılar Jev'e · özel satırlar bilgi kaydı",
        TurnLintLive, SECTIONS, "linter masayı bekletmiyor, DM'i yargılamıyor, sadece yazıyor")


if __name__ == "__main__":
    sys.exit(report())
