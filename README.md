# Jarvis for macOS — double clap → welcome home

A Python script that listens to your Mac's microphone. When you **clap twice**, it:

1. immediately opens the animated, full-screen **Jarvis interface** (and keeps it in front for the whole session),
2. starts your song in the **Spotify** app in the background — Spotify's window never appears — at full volume,
3. after the song has played for 7 s (while the interface boots), smoothly **ducks** the music to 35% — still audible — and speaks a welcome line in your **ElevenLabs** voice over it, while the interface shows **SPEAKING** and reacts to the voice,
4. when the voice finishes, **fades the music back up** over ~2 s (Spotify's own volume only — your Mac's volume and other sounds are untouched),
5. shows **AWAITING COMMAND**, then starts **listening** — you can talk to Jarvis naturally, in Italian or English (see *Talking to Jarvis*). The interface stays open until you close it.

Jarvis no longer opens any other apps or websites (Claude, Chrome, Cursor, Tasaradar) by itself.

**How it behaves:** start Jarvis → it measures your room for 1.5 s → waits for **one** valid double clap → **turns the microphone off** → runs the welcome once → keeps the interface open until you close it. Nothing you say or type afterwards can trigger it again. To use it again, run `./start_jarvis.sh` again.

## Requirements

- A Mac running macOS 12 (Monterey) or newer.
- **Python 3** from https://www.python.org/downloads/macos/ (3.10 or newer recommended).
- **Spotify** installed in your Applications folder (without it, the song opens in Spotify's web player instead, with no ducking).
- An **ElevenLabs** account (API key + voice ID) for the spoken welcome. Without it, everything else still works.

## Install (first time)

1. **Install Python**: download the macOS installer from https://www.python.org/downloads/macos/, open it and click through. When it finishes, a Finder window opens; double-click **Install Certificates.command** in it.
2. **Download Jarvis**: download the ZIP from GitHub (green **Code** button → **Download ZIP**), double-click it to unzip, rename the folder to `jarvis` and move it into your home folder (the one with your name, next to Desktop and Documents).
3. **Open Terminal** (press Cmd+Space, type `Terminal`, press Enter) and run:

   ```bash
   cd ~/jarvis
   chmod +x start_jarvis.sh
   ./start_jarvis.sh
   ```

   The first run sets everything up (about a minute), creates your `.env` settings file, and starts listening. Press **Ctrl+C** to stop it, then add your ElevenLabs details (next section).

## Settings (`.env`)

All settings live in the `.env` file in the `jarvis` folder. To edit it in TextEdit:

```bash
open -e ~/jarvis/.env
```

(Finder hides files whose names start with a dot. Press Cmd+Shift+. in Finder to show them.)

| Setting | What it does | Default |
| ------- | ------------ | ------- |
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key (ElevenLabs → profile → API Keys). | *(empty, no voice)* |
| `ELEVENLABS_VOICE_ID` | The voice to use (ElevenLabs → Voices → ⋯ → Copy voice ID). Only the ID itself: letters and numbers, no spaces, no quotes, e.g. `ELEVENLABS_VOICE_ID=AbCdEf1234567890xyz`. | *(empty, no voice)* |
| `JARVIS_WELCOME_PHRASE` | What Jarvis says. | `Welcome home, sir. All systems are online.` |
| `JARVIS_WELCOME_ENABLED` | `false` turns the voice off. | `true` |
| `JARVIS_AFTER_SONG_DELAY_S` | Seconds between the music starting and the voice starting — only for YouTube/web links, which can't be ducked. | `0.5` |
| `JARVIS_SONG_URI` | Spotify link (`https://open.spotify.com/track/…` or `spotify:track:…`) or a YouTube link. | the original track |
| `JARVIS_UI_ENABLED` | `false` = no full-screen interface (audio only, as before). | `true` |
| `JARVIS_MUSIC_LEAD_IN_SECONDS` | How long the song plays at full volume before ducking (the voice waits until then). | `7` |
| `JARVIS_SPOTIFY_DUCK_VOLUME` | Spotify's volume (0–100) while the voice speaks. | `35` |
| `JARVIS_SPOTIFY_DUCK_FADE_SECONDS` | How long the duck (full → duck volume) takes; the voice starts when it's done. | `0.5` |
| `JARVIS_SPOTIFY_NORMAL_VOLUME` | The song's full volume, at the start and after the voice (used when your previous Spotify volume is unknown or lower than the duck volume, or always if `JARVIS_SPOTIFY_RESTORE_PREVIOUS=false`). | `65` |
| `JARVIS_SPOTIFY_RESTORE_PREVIOUS` | `true` = full volume is the volume Spotify had before Jarvis started; `false` = always `JARVIS_SPOTIFY_NORMAL_VOLUME`. | `true` |
| `JARVIS_SPOTIFY_FADE_SECONDS` | How long the rise back to full volume after the voice takes. | `2.0` |
| `JARVIS_INPUT_DEVICE` | Force a microphone by name (e.g. `MacBook Pro Microphone`) or number. | your default mic |
| `JARVIS_MIN_CLAP_PEAK` | The quietest peak a clap may have, however quiet the room. | `0.15` |
| `JARVIS_MIN_CLAP_RMS` | The least energy (`rms`) a clap may have. This is what separates claps from clicks, taps and keys. Raise (e.g. `0.5`) if other sounds still trigger it; lower (e.g. `0.15`) if your claps show `too little energy`. | `0.3` |
| `JARVIS_SPIKE_RATIO` | How many times louder than the room a clap must be. Lower = more sensitive. | `5.0` |
| `JARVIS_MAX_DOUBLE_GAP_S` | Longest pause allowed between the two claps, in seconds. | `0.8` |
| `JARVIS_LEVEL_LOG_S` | How often the live sound-level line is printed (seconds, `0` = off). | `5` |
| `JARVIS_SAMPLE_RATE` | Try `48000` if your mic complains. | `44100` |
| `ELEVENLABS_MODEL_ID` / `ELEVENLABS_OUTPUT_FORMAT` / `ELEVENLABS_PCM_SAMPLE_RATE` | Advanced TTS options (format must be `pcm_…`). | `eleven_multilingual_v2` / `pcm_24000` |
| `JARVIS_WELCOME_CACHE_DIR` / `JARVIS_WELCOME_CACHE_ENABLED` | Where the spoken welcome is cached, so ElevenLabs is only called when the phrase or voice changes. | `.cache/jarvis_welcome/` / `true` |

Save the file, then restart Jarvis for changes to take effect.

## Update Jarvis

To get the latest version, without touching your `.env` settings:

```bash
cd ~/jarvis
./update_jarvis.sh
```

(If `update_jarvis.sh` isn't in your folder yet, download it once with:
`curl -fsSL https://raw.githubusercontent.com/francescapratowork/jarvis/main/update_jarvis.sh -o ~/jarvis/update_jarvis.sh && chmod +x ~/jarvis/update_jarvis.sh`.)

Every time Jarvis starts, its first lines show which version is running and from which folder, e.g.:

```
Jarvis version 2026-10-06.12 (interface ready handshake before Spotify, focus guard) — running /Users/you/jarvis/jarvis.py
Settings file: /Users/you/jarvis/.env (found)
```

If the version or folder isn't what you expect, you're running an old copy.

## Run

```bash
cd ~/jarvis
./start_jarvis.sh
```

Stay quiet for the first two seconds while Jarvis measures the room noise. When you see `Ready — clap twice.`, clap twice firmly (like "clap–clap", about a third of a second apart). Then Jarvis stops listening and runs the welcome:

```
DOUBLE CLAP DETECTED!
Microphone closed — Jarvis is no longer listening. Running the welcome sequence...
Jarvis interface: launching...
Jarvis interface: fullscreen and ready (1.4s).
Spotify: starting in background...
Spotify: asking it to play spotify:track:… at 70% (your previous Spotify volume; Spotify volume was 70%)...
Spotify: SUCCESS — playing "…" by … at 70%.
Spotify: ducking 70% → 35% over 0.5s for the voice...
Spotify: ducked to 35% (still audible under the voice).
Welcome voice: speaking (6.2s)...
Welcome voice: finished.
Spotify: fading music up 35% → 70% over 2.0s (your previous Spotify volume)...
…
Spotify: restored to 70%.
Jarvis interface is open and awaiting commands. Close it with Esc twice, the power button or Cmd+Q (or press Ctrl+C here).
```

Stop it at any time with **Ctrl+C**.

## The Jarvis interface

After the double clap, Jarvis opens a full-screen, animated interface (it runs locally, no internet needed) and keeps it in front:

- **STARTING** — boot animation with a countdown ring while the song plays at full volume.
- **ONLINE** — the core idles with slow rotating rings.
- **SPEAKING** — the core pulses and a ring of bars reacts to the voice in real time.
- **LISTENING** / **THINKING** — ready for the upcoming voice commands.
- **AWAITING COMMAND** — shown at the end; the interface stays open.

The panels show real information: the song playing on Spotify and its volume (including the duck and fade), CPU load, microphone state, voice output and a live event log.

**To exit:** press **Esc twice**, click the **power button** (top right), press **Cmd+Q**, or press **Ctrl+C** in Terminal.

**Preview it without clapping** (no microphone, music or voice — it cycles through all the states):

```bash
cd ~/jarvis
./start_jarvis.sh --ui-demo
```

## Test your claps (nothing opens)

```bash
cd ~/jarvis
./start_jarvis.sh --test
```

In test mode Jarvis keeps listening, so you can clap as often as you like and watch the Terminal:

```
Room noise: peak 0.0500 (rms 0.0150). A clap needs peak ≥ 0.250 and rms ≥ 0.300.
Ready — clap twice.
Ignored a sound: too little energy — a click, tap or key press? (peak 1.012, rms 0.079 (need 0.300), 98% high-pitched, 2 ms wide, threshold 0.250)
Clap 1 heard (peak 14.985, rms 1.401 (need 0.300), 85% high-pitched, 3 ms wide, threshold 0.252) — clap again...
Clap 2 heard after 0.30s (peak 14.985, rms 1.275 (need 0.301), 78% high-pitched, 3 ms wide, threshold 0.253) — checking it's not part of other noise...
DOUBLE CLAP DETECTED!
Level: room noise 0.0512 | loudest 0.4730 | clap threshold 0.252
```

- **Clap 1 / Clap 2 heard**: a clap was recognised, with its measured peak, energy (`rms`, and the minimum needed), pitch and width. On a MacBook, real claps measure about peak 10–18 and rms 1.1–2.3.
- **Ignored a sound**: something loud that is *not* a clap, and why: a click or key press (too little energy), a voice or thump (too low-pitched), music or talking (too long), or other noise right before or after.
- **Level** (every 5 seconds): room noise, the loudest recent sound, and how loud a clap must be.
- Jarvis only accepts two claps with silence around them, so typing, talking or music between or right after the claps cancels them.

## Talking to Jarvis (Phase 2A)

After the welcome, Jarvis listens. Just speak — no commands to memorise:

- "Jarvis, cosa ho domani?" · "Quando sono libera giovedì?" · "Che promemoria ho per oggi?"
- "Ricordati che devo ricontattare Giulia venerdì." → *"Annotato."* (saved in Jarvis's memory)
- "Aprimi Claude." · "Grazie, basta così." (ends the conversation)

The interface shows what is really happening: **LISTENING** (the core reacts to your voice) → **THINKING** → **SPEAKING** → **LISTENING**. Your words and Jarvis's actions appear in the event log. After about **60 seconds of silence** Jarvis goes back to **AWAITING COMMAND** — press **Space** (or click the microphone button) to talk again. While you talk with Jarvis, Spotify plays more quietly (`JARVIS_CONVERSATION_MUSIC_VOLUME`).

**What it can do in this version:** read your calendars and reminders (everything visible on this Mac), find free time, remember and recall important facts (stored only on your Mac in `data/`, never in the repository), forget a fact (only after you confirm), open apps when you ask. Creating or changing calendar events and reminders comes in the next phase — it will always ask for your confirmation first.

**What it needs (one-time setup):**
1. Calendar access — run `./start_jarvis.sh --check-calendar` and follow what it says.
2. An Anthropic API key in `.env` as `ANTHROPIC_API_KEY=…` (console.anthropic.com).
3. Your ElevenLabs key must allow **Speech to Text** (ElevenLabs → Developers → API Keys → edit the key → Speech to Text: Access).

Until the Anthropic key is added, Jarvis behaves exactly as before and stays on AWAITING COMMAND.

**Test the brain by typing** (no microphone or voice): `./start_jarvis.sh --chat`

**Privacy:** what you say is transcribed by ElevenLabs and answered by Anthropic's Claude (including any calendar details needed for the answer). Memory and calendar access stay on your Mac. Keys are never printed or logged.

| Setting | What it does | Default |
| ------- | ------------ | ------- |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (keep it secret). | *(empty — conversation off)* |
| `JARVIS_LLM_MODEL` / `JARVIS_LLM_EFFORT` | Claude model and how hard it thinks (`low` = fastest). | `claude-opus-5-5` / `low` |
| `JARVIS_STT_MODEL` | ElevenLabs speech-to-text model. | `scribe_v2_realtime` |
| `JARVIS_REPLY_TTS_MODEL` | ElevenLabs voice model for replies (same Bella voice ID; the welcome keeps `ELEVENLABS_MODEL_ID`). `eleven_flash_v2_5` is the fastest; `eleven_multilingual_v2` matches the welcome exactly. | `eleven_flash_v2_5` |
| `JARVIS_CONVERSATION_TIMEOUT_S` | Seconds of silence before returning to AWAITING COMMAND. | `60` |
| `JARVIS_CONVERSATION_MUSIC_VOLUME` | Spotify volume during a conversation (`off` = unchanged). | `25` |
| `JARVIS_USER_NAME` | How Jarvis addresses you. | `Miss Prato` |
| `JARVIS_CONVERSATION_ENABLED` / `JARVIS_CONVERSATION_AUTOSTART` | Turn conversation off / don't start listening right after the welcome. | `true` / `true` |
| `JARVIS_CALENDAR_BACKEND` | `auto`, `eventkit` (direct) or `applescript` (via the Calendar app). | `auto` |

## macOS permissions (first run)

macOS asks for permission the first time Jarvis does each thing. Click **Allow** / **OK** each time:

| Prompt | Why |
| ------ | --- |
| "Terminal" would like to access the microphone | Hearing your claps. |
| "Terminal" wants access to control "Spotify" | Playing the song and controlling its volume in the background. |
| "Terminal" would like to access your calendars / reminders (or to control "Calendar" / "Reminders") | Reading your schedule and reminders (`--check-calendar`). |

The full-screen interface needs no extra permission. If you click "Don't Allow" by mistake, turn it back on in **System Settings → Privacy & Security** under **Microphone** or **Automation**, then quit Terminal (Cmd+Q) and start again.

## Tuning

The threshold adapts to your room but never drops below `JARVIS_MIN_CLAP_PEAK` (0.15) and `JARVIS_MIN_CLAP_RMS` (0.3), so a quiet room can't make Jarvis jumpy. If you need to tune it, run `--test` and compare the `rms` of your claps and other sounds with the `need` value:

- Claps show up as `too little energy` → lower `JARVIS_MIN_CLAP_RMS` (keep it well above what your clicks and typing show).
- Other sounds show up as `Clap 1 heard` → raise `JARVIS_MIN_CLAP_RMS` (keep it well below what your claps show).

Advanced shape checks (`CLAP_RMS_RATIO`, `MIN_HF_RATIO`, `MAX_CLAP_LEN_S`, `QUIET_BEFORE_S`, `QUIET_AFTER_S`, …) are at the top of `jarvis.py`.

## Troubleshooting

- **"Every microphone sounds silent":** Microphone permission is off. Turn on Terminal in **System Settings → Privacy & Security → Microphone**, quit Terminal with Cmd+Q, and start again.
- **Wrong mic:** Jarvis prints the list of audio devices on startup. Put the name (or number) of the one you want in `JARVIS_INPUT_DEVICE`.
- **No reaction to claps:** Run `./start_jarvis.sh --test` and see the Tuning section. If `Clap 1` appears but never `Clap 2`, clap a bit faster (within 0.8 s) and keep quiet right after.
- **Triggers on other sounds:** Raise `JARVIS_MIN_CLAP_RMS` (e.g. `0.5`).
- **"The room (or mic gain) is very loud":** Lower the input volume in **System Settings → Sound → Input**.
- **No music:** Look for the `Spotify:` lines in Terminal. They say `SUCCESS` with the song name, or what went wrong:
  - *did not allow Jarvis to control Spotify*: turn on Spotify under **System Settings → Privacy & Security → Automation → Terminal**, quit Terminal (Cmd+Q), start again.
  - *did not start playing*: open Spotify, check you're logged in and the track plays when you click it, and that Spotify isn't playing on another device (the speaker icon at the bottom right).
  - *music too loud/quiet under the voice*: change `JARVIS_SPOTIFY_DUCK_VOLUME` (e.g. `25` or `45`); after the voice, `JARVIS_SPOTIFY_NORMAL_VOLUME`.
  - Also check your Mac isn't muted. Jarvis warns if it is.
- **The interface doesn't appear:** look for a `Jarvis interface:` line in Terminal. If it mentions `pywebview`, run `./start_jarvis.sh` again (it installs it). Try `./start_jarvis.sh --ui-demo` to test the interface on its own. Set `JARVIS_UI_ENABLED=false` to run without it.
- **Spotify's window shows up:** Jarvis only starts Spotify after the interface reports it is full screen and in front, launches it hidden, and while starting up immediately takes the front back from any app that grabs it (Terminal shows `… took focus — brought Jarvis back to the front`). If the interface reports `fullscreen=False` or `in front=False`, macOS refused it — send that line along when reporting the problem.
- **No welcome speech:** Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env`, and look for an `ElevenLabs TTS failed` line in Terminal. A `404 Not Found` whose address contains something other than your voice ID (e.g. `/v1/text-to-speech/open%20-e%20...`) means the `ELEVENLABS_VOICE_ID=` line holds the wrong text: put only the voice ID after the `=`.
- **"could not verify ElevenLabs' security certificate":** Jarvis checks ElevenLabs' certificate against your Mac's own trusted certificates (via `truststore`), and falls back to the `certifi` bundle. It never turns verification off. If you still see this, run `./start_jarvis.sh` once so it can install the packages. Then open **Applications → Python 3.x** and double-click **Install Certificates.command**, quit Terminal (Cmd+Q) and start again. A VPN, antivirus or company network that inspects HTTPS can also cause it.
- **A fix doesn't seem to apply:** Check the `Jarvis version … running …` line at startup, then run `./update_jarvis.sh`.
- **`zsh: permission denied: ./start_jarvis.sh`:** Run `chmod +x start_jarvis.sh` once.
