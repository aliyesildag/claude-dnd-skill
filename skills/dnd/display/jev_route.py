#!/usr/bin/env python3
"""Turn a player's free text into a typed call the DM can act on.

The table types in Turkish, in character, with typos and an English keyboard.
Reading that back into "which check, against what, from whom, and is it private"
is the part of the turn that costs the table its time, and the part where the
silent failures live: a skill chosen two different ways in two scenes, a name
spelled `Hısrayt` where the display knows `Hisrayt`, a d8 sent as a d20.

Jev answers all of those at once, from a fixed set of options, so the answer is
canonical by construction — it cannot return a name the campaign does not have.
Numbers stay here in code: this file owns the DC table and the dice spec, the
model only says which band and which check.

    python3 tools/jev_route.py --campaign temiz-kagit \
        --character "Yapraksever" \
        --text "kara özü ve dalları dikkatlice topluyorum" \
        --scene "Sur dibi, gece, üç ölü Nefes Çalısı" \
        --present "Yakup Hundur,Dilaver" \
        --option "Perception ile çevreyi tara" --option "Sopayla vur"

Prints JSON on stdout. Exit 0 on a usable answer, 3 when every judgment came
back below its threshold and the DM should decide unaided.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import jev_check

CAMPAIGN_ROOT = Path(os.environ.get("DND_CAMPAIGN_ROOT", Path.home() / ".claude" / "dnd")) / "campaigns"

# The DC band is a judgment; the number behind it is a house rule and stays here.
DC_BANDS = {
    "kolay": 10,
    "orta": 13,
    "zor": 15,
    "çok zor": 18,
}

# 5e 2024 skills, described so the model picks on meaning rather than on the
# English label. The Nature/Survival pair below is the one this table actually
# got wrong at the table, so both definitions name the distinction explicitly.
SKILLS = {
    "Acrobatics": "Denge, takla, düşerken toparlanma, dar yerde ayakta kalma.",
    "Animal Handling": "Bir hayvanı sakinleştirmek, yönlendirmek, niyetini okumak.",
    "Arcana": "Büyü, mühür, ritüel, büyülü nesne bilgisi.",
    "Athletics": "Güç işi: tırmanmak, itmek, tutmak, yüzmek, boğuşmak.",
    "Deception": "Bilerek yanlış inandırmak, blöf, sahte kimlik.",
    "History": "Geçmiş olaylar, hanedanlar, eski kayıtlar bilgisi.",
    "Insight": "Karşıdakinin niyetini, yalanını, gizlediğini okumak.",
    "Intimidation": "Korkuyla ya da tehditle boyun eğdirmek.",
    "Investigation": "İpucu aramak, çıkarım yapmak, bir şeyin nasıl çalıştığını sökmek.",
    "Medicine": "Yara, hastalık, zehir, bir bedende ne olduğunu anlamak.",
    "Nature": "Bu nedir ve ne işe yarar: bitki, hayvan, doğa olayı bilgisi.",
    "Perception": "Fark etmek: görmek, duymak, koklamak, gizleneni yakalamak.",
    "Performance": "Sahne: çalmak, söylemek, kalabalığı tutmak.",
    "Persuasion": "İyi niyetle ikna etmek, pazarlık, rica.",
    "Religion": "Tanrılar, tapınak düzeni, ayin ve dini metin bilgisi.",
    "Sleight of Hand": "El çabukluğu: aşırmak, gizlice yerleştirmek, saklamak.",
    "Stealth": "Görülmeden ve duyulmadan hareket etmek, takip etmek.",
    "Survival": "Doğada iş görmek: iz sürmek, yön bulmak, bozmadan toplamak, yol yapmak.",
}


def _campaign_names(campaign: str) -> "tuple[list[str], list[str]]":
    """Party names the display binds, and the NPC names the campaign has cast.

    Both come from campaign files rather than from a hand-kept list here, so a
    character added between sessions is routable the moment its file exists.
    """
    base = CAMPAIGN_ROOT / campaign
    # The display binds "Kızıl Zenci", the file is kizil-zenci.md. Route on the
    # name the table sees, which each sheet carries as its first heading.
    party = []
    for sheet in sorted((base / "characters").glob("*.md")):
        try:
            first = sheet.read_text(encoding="utf-8").lstrip().splitlines()[0]
        except (OSError, IndexError):
            continue
        party.append(first.lstrip("# ").strip() or sheet.stem)
    npcs: list[str] = []
    cast = base / "ses-haritasi.json"
    if cast.exists():
        try:
            data = json.loads(cast.read_text(encoding="utf-8"))
            npcs = sorted(k for k in data if not k.startswith("_"))
        except (OSError, ValueError):
            pass
    return party, npcs


def _questions(options: "list[str]", npcs: dict, party: "list[str]") -> dict:
    q: dict = {
        "zar_gerekli": {
            "type": "noul",
            "instructions": (
                "Bu beyan bir d20 testi gerektiriyor mu? D&D kuralında şu üç durumda "
                "zar atılır: birinin fark etmesi mümkünken gizlenmek ya da takip etmek, "
                "birini ikna etmek kandırmak ya da korkutmak, ve beceriyle yapılan "
                "sonucu belirsiz her iş. Kimsenin karşı koymadığı, aceleye gelmeyen ve "
                "başarısızlığı anlamsız olan iş zar istemez."
            ),
            "criteria": {
                "true": (
                    "Biri fark edebilir, biri karşı koyabilir, ya da beceri gerektiren "
                    "işin sonucu belirsiz. Gizlenmek, takip etmek, iz sürmek, ikna etmek, "
                    "kandırmak, tırmanmak, bir şeyi fark etmeye çalışmak bu sınıfa girer."
                ),
                "false": (
                    "Sonuç kesin: yürümek, oturmak, bir şey vermek, açıkça konuşmak, "
                    "kimsenin engellemediği sıradan bir iş."
                ),
            },
        },
        "skill": {
            "type": "choice",
            "instructions": (
                "Bu beyan hangi yeteneğe bakıyor? Oyuncunun yaklaşımına bak, "
                "kullandığı kelimeye değil. Zar gerekmiyorsa da en yakınını seç."
            ),
            "criteria": dict(SKILLS),
        },
        "zorluk": {
            "type": "score",
            "instructions": (
                "Bu iş sahnenin koşullarında ne kadar zor? Kalabalık, karanlık, "
                "karşıdakinin uyanıklığı ve acele hesaba katılır."
            ),
            "criteria": [
                "kolay: sakin ortam, hazırlıklı, karşı koyan yok",
                "orta: olağan koşullar, dikkatli bir karşı taraf",
                "zor: kalabalık, gece, uyanık ya da eğitimli bir karşı taraf",
                "çok zor: doğrudan karşı koyan, tehlikeli ya da alenen imkânsıza yakın",
            ],
        },
        "ozel": {
            "type": "noul",
            "instructions": (
                "Sonucu yalnızca bu oyuncunun görmesi doğru olur mu? Tek başına "
                "algıladığı, hatırladığı ya da kimseye söylemeden yaptığı şeyler özeldir."
            ),
            "criteria": {
                "true": "Sahnede yalnız ya da bilgi kişiye özel, masaya söylemek oyuncunun kararı.",
                "false": "Herkesin gördüğü, duyduğu ya da birlikte yaşadığı bir şey.",
            },
        },
    }
    if party:
        q["karakter"] = {
            "type": "choice",
            "instructions": "Bu beyanı hangi karakter yapıyor?",
            "criteria": {n: f"{n} adlı oyuncu karakteri" for n in party},
        }
    if npcs:
        q["hedef"] = {
            "type": "choice",
            "instructions": (
                "Beyan sahnedeki hangi kişiye yöneliyor? Kimseye yönelmiyorsa 'yok' seç."
            ),
            "criteria": {**npcs, "yok": "Kimseye yönelmiyor."},
        }
    if options:
        q["dal"] = {
            "type": "choice",
            "instructions": (
                "Beyan, ekrandaki seçeneklerden hangisine düşüyor? Hiçbirine "
                "uymuyorsa 'başka' seç."
            ),
            "criteria": {
                **{f"secenek_{i + 1}": o for i, o in enumerate(options)},
                "başka": "Listedeki hiçbir seçeneğe uymayan, oyuncunun kendi bulduğu bir hamle.",
            },
        }
    return q


def ask(state: dict, questions: dict) -> dict:
    answers = jev_check._ask(state, questions)
    if not answers:
        raise RuntimeError("no answer from the model (no key, no network, or a refusal)")
    return {"answers": answers}


def main() -> None:
    ap = argparse.ArgumentParser(description="Route a player's free text into a typed DM call.")
    ap.add_argument("--campaign", default="temiz-kagit")
    ap.add_argument("--character", default="", help="who the display says is typing, if known")
    ap.add_argument("--text", required=True, help="the player's declaration, verbatim")
    ap.add_argument("--scene", default="", help="one line: where the party is and what is happening")
    ap.add_argument("--present", default="", help="comma-separated NPCs on scene")
    ap.add_argument("--context", action="append", default=[],
                    help="a campaign fact the judgment needs (repeat); e.g. what a thing in the "
                         "declaration actually is. Without it the model guesses from the word alone.")
    ap.add_argument("--option", action="append", default=[],
                    help="one screen option; repeat for each (the branch selector)")
    ap.add_argument("--min-confidence", type=float, default=0.55,
                    help="below this the answer is reported as unresolved (default 0.55)")
    args = ap.parse_args()

    party, cast = _campaign_names(args.campaign)
    present = [n.strip() for n in args.present.split(",") if n.strip()]
    # Narrow the NPC list to who is on scene when the caller says so: a shorter
    # list of live options beats a complete list of mostly absent ones.
    # Describe who is on scene rather than only naming them: "hangi kişiye
    # yöneliyor" is answered by what someone is, and a bare name says nothing.
    roles = jev_check.cast_with_roles(args.campaign)
    npcs = {n: roles.get(n, n) for n in (present or cast)}

    state = {
        "beyan": args.text,
        "sahne": args.scene or "(sahne bilgisi verilmedi)",
        "sahnedekiler": npcs,
        "parti": party,
    }
    if args.character:
        state["yazan_oyuncu"] = args.character
    if args.context:
        state["kampanya_bilgisi"] = args.context

    answers = ask(state, _questions(args.option, npcs, party)).get("answers", {})

    def choice(key):
        a = answers.get(key) or {}
        return a.get("choice"), float(a.get("confidence") or 0.0)

    skill, skill_conf = choice("skill")
    hedef, hedef_conf = choice("hedef")
    dal, dal_conf = choice("dal")
    karakter, karakter_conf = choice("karakter")

    band_answer = answers.get("zorluk") or {}
    legend = band_answer.get("legend") or {}
    band_label = legend.get(str(int(round(float(band_answer.get("score") or 0)))), "")
    band = band_label.split(":")[0].strip() if band_label else "orta"
    zar = float((answers.get("zar_gerekli") or {}).get("noul") or 0.0)
    ozel = float((answers.get("ozel") or {}).get("noul") or 0.0)

    out = {
        "karakter": karakter if karakter_conf >= args.min_confidence else (args.character or None),
        "zar_gerekli": zar >= 0.5,
        "zar_olasilik": round(zar, 3),
        "skill": skill,
        "skill_guven": round(skill_conf, 3),
        "zorluk": band,
        "dc": DC_BANDS.get(band, 13),
        "hedef": None if hedef in (None, "yok") else hedef,
        "hedef_guven": round(hedef_conf, 3),
        "dal": dal,
        "dal_guven": round(dal_conf, 3),
        "ozel": ozel >= 0.5,
        "ozel_olasilik": round(ozel, 3),
        # Anything the DM should look at rather than accept. Confidence says how
        # concentrated the distribution is, not whether the call is right, so a
        # flagged line is a prompt to decide, not an error.
        "belirsiz": [k for k, c in (("skill", skill_conf), ("hedef", hedef_conf), ("dal", dal_conf))
                     if c < args.min_confidence],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(3 if len(out["belirsiz"]) == 3 else 0)


if __name__ == "__main__":
    main()
