# Narrator TTS setup (optional)

The display companion can read narrator and NPC blocks aloud. It's optional, off by default, and the rest of the skill works fine without it.

Two backends ship:

| Provider | When it's used | Cost |
|---|---|---|
| **Azure Speech** *(default)* | whenever an Azure key resolves | **free** up to 500,000 characters/month, permanently |
| Gemini Flash TTS | fallback, when only a Gemini key is present | ~$0.039 per minute of audio |

Azure is the recommended path: its free tier is large enough for a heavy table, and its Turkish voices are a generation ahead of anything else free. Set `DND_TTS_PROVIDER=gemini` to force the fallback.

If you configure neither, the display renders text exactly as it does today — no audio, no warnings, no behavior change.

## What you get

- A speaker button at the bottom of every narrator and NPC block. Click to hear that block read aloud.
- A voice dropdown next to it. Change voice mid-session; the choice persists per-campaign.
- An optional **Auto Narrate** toggle in the top-right audio controls. When on, every new narrator/NPC block auto-plays in **your browser only** — for a TV or cast device while player phones stay quiet. **Off by default**, and worth leaving off: see *Watching the quota* below.
- A running character counter so you always know where the month stands against the free quota.

## What it costs

**Azure F0 (free tier): 500,000 characters per month, and it never expires.**

Measured on this skill's own narration: **1,089 characters produce 79 seconds of speech**, so one hour of audio costs about 49,600 characters. That puts the free quota at roughly **10 hours of narration per month**.

Two ways to spend it:

| Pattern | Audio/month | Characters | Fits in free tier? |
|---|---|---|---|
| Scene blocks only (~10 per session) | ~3.3 h | ~165k | yes, comfortably |
| Auto-narrate every DM block | 11–16 h | 540k–810k | **no** |

Past 500k, Azure F0 stops serving rather than billing you — you'd have to move the resource to the S0 tier to pay for more ($16/1M characters for Neural, $22/1M for Neural HD).

The Gemini fallback has no free tier worth planning around: measured at **$0.0386 per minute of audio**, so the same 10 hours would cost about $23.

> Earlier versions of this document claimed ~$0.001 per block and ~3¢ per session for Gemini. That was wrong by roughly 27×, measured against the model the code actually calls.

## Setup — Azure Speech (~10 minutes)

### 1. Create a free Azure account

https://azure.microsoft.com/free — Azure asks for a card to verify identity, but the F0 tier never bills. This is not the same as Google Cloud's refundable $30 prepayment.

### 2. Create a Speech resource on the Free F0 tier

Portal: search **speech** in the top bar → **Speech services** → **+ Create**.

| Field | Value |
|---|---|
| Resource group | create one, e.g. `dnd` |
| Region | any that accepts new customers — `northeurope` works when `westeurope` refuses |
| Name | e.g. `dnd-tts` |
| **Pricing tier** | **`Free F0`** |

Or from the CLI:

```bash
az group create -n dnd -l northeurope
az cognitiveservices account create -n dnd-tts -g dnd \
  --kind SpeechServices --sku F0 -l northeurope --yes
```

Some regions reject new free-tier customers with `RequestDisallowedByAzure`. Try another region; the resource does not have to sit near you.

### 3. Save the key and region

```bash
mkdir -p ~/.config/claude-dnd && chmod 700 ~/.config/claude-dnd

az cognitiveservices account keys list -n dnd-tts -g dnd --query key1 -o tsv \
  > ~/.config/claude-dnd/azure-tts.key
echo northeurope > ~/.config/claude-dnd/azure-tts.region

chmod 600 ~/.config/claude-dnd/azure-tts.key
```

Environment variables take precedence if you prefer them: `DND_AZURE_TTS_KEY` (or `AZURE_SPEECH_KEY`) and `DND_AZURE_TTS_REGION` (or `AZURE_SPEECH_REGION`).

### 4. Verify

```bash
python3 display/tts.py --test
```

```
Provider:   azure
Key source: file:/home/you/.config/claude-dnd/azure-tts.key
Region:     northeurope
Voice:      tr-TR-Aydın:MAI-Voice-2
Text:       'Höyüğün ağzı önünüzde açılıyor…'
Synthesizing…
  OK — 264,000 bytes L16 PCM (24 kHz mono), 5.5s of audio in 2.7s
```

Add `--speak` to hear it (tries `paplay`, `afplay`, then `aplay`).

## Setup — Gemini fallback

Only needed if you can't or won't create an Azure resource.

1. Get a key at **https://aistudio.google.com/apikey**.
2. Save it to `~/.config/claude-dnd/tts.key` (`chmod 600`), or export `DND_TTS_KEY` / `GEMINI_API_KEY`.

Be aware of what you're accepting: the model is a preview model, and measured latency across three runs of the same 77-second block was **46s, 188s and 251s**, with two of five calls returning `503`. It is not reliable for live narration at the table.

## Voice catalog

The dropdown follows whichever provider is active. For Azure, the tr-TR catalog:

| Group | Voice id | Shown as | Notes |
|---|---|---|---|
| Male | **`tr-TR-Aydın:MAI-Voice-2`** *(default)* | Aydın HD | Best quality. ~13s for 80s of audio |
| Male | `tr-TR-Aydın:MAI-Voice-2-Flash` | Aydın Flash | Faster, slightly flatter |
| Male | `tr-TR-AhmetNeural` | Ahmet | Previous generation — noticeably robotic on long narration |
| Female | `tr-TR-Elif:MAI-Voice-2` | Elif HD | Best quality. ~6s for 79s of audio |
| Female | `tr-TR-Elif:MAI-Voice-2-Flash` | Elif Flash | Faster, slightly flatter |
| Female | `tr-TR-EmelNeural` | Emel | Previous generation |

A reasonable split for a table: HD for the narrator, Flash for NPC chatter.

To use a different locale, list what your resource offers and edit `AZURE_VOICES_MALE` / `AZURE_VOICES_FEMALE` in `display/tts.py`:

```bash
curl -s -H "Ocp-Apim-Subscription-Key: $(cat ~/.config/claude-dnd/azure-tts.key)" \
  "https://$(cat ~/.config/claude-dnd/azure-tts.region).tts.speech.microsoft.com/cognitiveservices/voices/list" \
  | python3 -c "import sys,json;[print(v['ShortName']) for v in json.load(sys.stdin) if v['Locale']=='tr-TR']"
```

The voice selection persists per-campaign in `state.md → ## Session Flags → tts_voice: <name>`.

## Watching the quota

Azure exposes **no character-count metric** for Speech resources — the portal shows call counts, not characters — so the skill keeps its own tally in `~/.config/claude-dnd/tts-usage.json`, keyed by calendar month and written atomically so it survives restarts.

```bash
python3 display/tts.py --usage
```

```
Month:    2026-09
Calls:    42
Chars:    48,120  (~58.2 min of audio)
  azure      48,120 chars    42 calls
Azure F0: 9.6% of 500,000 (451,880 left)
```

The same numbers are available at `GET /tts-usage` on the display, and every `/tts` response carries an `X-Quota-Used-Pct` header.

It counts the plain text actually sent — after the 2,000-character truncation, excluding the SSML envelope — because that is what Azure meters.

**The single biggest lever on the quota is Auto Narrate.** Left off (the default), you spend characters only on blocks someone deliberately clicks, which lands around 165k/month for a table playing 3–4 days a week. Turned on for every block, the same table spends 540k–810k and runs out. If you want it on, put it on the casting TV only — each player who clicks the same block makes a *separate* synthesis call, since nothing is cached by content hash.

## Using it during a session

- A speaker icon appears at the bottom-right of every narrator (`.dm-block`) and NPC (`.npc-block`) block. Click to play, click again to stop.
- Player input, dice-roll and tutor blocks intentionally **don't** get one — they're metadata, not narrative voice.
- The 2,000-character cap is the upper bound per request; longer blocks are truncated server-side. Azure F0 also allows only 20 requests per 60 seconds and the client backs off automatically on `429`.

## Multi-language sessions

Both backends detect the language from the text. Azure voices are locale-specific, though — a `tr-TR` voice reading Spanish will sound wrong, so switch the catalog in `display/tts.py` if you change table language. Gemini's voices are locale-neutral.

SFX trigger packs are configured separately, either via environment:

```bash
export DND_SFX_LANGUAGES=en,tr     # English first, then Turkish
```

…or per-campaign via `state.md → ## Session Flags`:

```
sfx_languages: tr,en
```

Packs ship for `ar`, `bn`, `de`, `en`, `es`, `fr`, `hi`, `id`, `it`, `ja`, `ko`, `mr`, `nl`, `pl`, `pt`, `ro`, `ru`, `ta`, `te`, `th`, `tr`, `uk`, `vi`, `zh`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `--test` says `Key source: unset` | No env var **and** no key file. Save the key to `~/.config/claude-dnd/azure-tts.key`. |
| `--test` reports provider `gemini` when you wanted Azure | The Azure key file is missing or empty; Gemini's key wins by fallback. Check with `python3 display/tts.py --voices`. |
| `TTS 401` | Azure key wrong, or the key belongs to a different region than `azure-tts.region`. |
| `TTS 429` | F0's 20-requests-per-60-seconds limit, or the 500k monthly quota is spent. Check `--usage`. |
| `RequestDisallowedByAzure` on create | That region isn't accepting new free-tier customers. Pick another. |
| `TTS 503` | Server reports TTS not configured — re-verify the key file and restart the display. |
| Audio doesn't play, no error label | Check device volume; on iOS Safari click the speaker once to grant the AudioContext gesture, then auto-narrate works for the rest of the session. |
| Long pause before audio on Gemini | Expected. The preview model has been measured at 46–251s per block. Switch to Azure. |

## How to disable

```bash
rm ~/.config/claude-dnd/azure-tts.key ~/.config/claude-dnd/tts.key
```

Speaker buttons disappear on next page load. Nothing else changes. The usage tally is left in place; delete `~/.config/claude-dnd/tts-usage.json` if you want it gone too.
