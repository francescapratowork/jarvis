# Jarvis for macOS — double clap → welcome home

A Python script that listens to your Mac's microphone. When you **clap twice**, it:

1. plays your song in the **Spotify** app (and tells you in Terminal whether it worked),
2. opens **Claude** and **Tasaradar** in new **Google Chrome** windows (fullscreen, one per display if you have several),
3. speaks a welcome line in your **ElevenLabs** voice through your Mac speakers,
4. brings **Cursor** to the front (fullscreen), launching it if needed.

**How it behaves:** start Jarvis → it measures your room for 1.5 s → waits for **one** valid double clap → **turns the microphone off** → runs the welcome once → stops. Nothing you say or type afterwards can trigger it again. To use it again, run `./start_jarvis.sh` again.

## Requirements

- A Mac running macOS 12 (Monterey) or newer.
- **Python 3** from https://www.python.org/downloads/macos/ (3.10 or newer recommended).
- **Spotify**, **Google Chrome** and **Cursor** installed in your Applications folder. Any of them can be missing: Jarvis skips Cursor, and opens Spotify/Chrome links in your default browser instead.
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
| `ELEVENLABS_VOICE_ID` | The voice to use (ElevenLabs → Voices → ⋯ → Copy voice ID). | *(empty, no voice)* |
| `JARVIS_WELCOME_PHRASE` | What Jarvis says. | `Welcome home, sir. All systems are online.` |
| `JARVIS_WELCOME_ENABLED` | `false` turns the voice off. | `true` |
| `JARVIS_AFTER_SONG_DELAY_S` | Seconds between starting the song and speaking. | `1.0` |
| `JARVIS_SONG_URI` | Spotify link (`https://open.spotify.com/track/…` or `spotify:track:…`) or a YouTube link. | the original track |
| `SPOTIFY_VOLUME` | Spotify volume 0–100 set before playing. | unchanged |
| `CLAUDE_CODE_URL` | First Chrome window. | `https://claude.ai/new` |
| `TASARADAR_URL` | Second Chrome window. `BINANCE_BTC_URL` from older setups is still read if this is empty. | `https://tasaradar.com` |
| `OPEN_CLAUDE_CODE_IN_CHROME` / `OPEN_TASARADAR_IN_CHROME` | `false` skips that window. | `true` |
| `CLAUDE_CHROME_MONITOR` / `TASARADAR_CHROME_MONITOR` | Display number (1 = leftmost). If you have fewer displays, the last one is used. | `1` / `3` |
| `OPEN_CHROME_FULLSCREEN` | `true` = macOS fullscreen, `false` = normal window. | `true` |
| `CHROME_WINDOW_WIDTH` / `CHROME_WINDOW_HEIGHT` | Window size when not fullscreen. | `1400` / `900` |
| `CHROME_SEPARATE_SITE_PROFILES` | `true` = each site opens in its own temporary Chrome profile (no logins). | `false` |
| `CURSOR_OPEN_FULLSCREEN` | `true` puts Cursor in fullscreen. | `true` |
| `FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP` / `OPEN_NEW_CURSOR_ON_DOUBLE_CLAP` | Bring Cursor forward / also open a new Cursor window. | `true` / `false` |
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

## Run

```bash
cd ~/jarvis
./start_jarvis.sh
```

Stay quiet for the first two seconds while Jarvis measures the room noise. When you see `Ready — clap twice.`, clap twice firmly (like "clap–clap", about a third of a second apart). Then Jarvis stops listening and runs the welcome:

```
DOUBLE CLAP DETECTED!
Microphone closed — Jarvis is no longer listening. Running the welcome sequence...
Spotify: SUCCESS — playing "…" by … (Spotify volume 70%).
…
Welcome sequence finished. Jarvis has stopped. Run ./start_jarvis.sh to use it again.
```

Stop it at any time with **Ctrl+C**.

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

## macOS permissions (first run)

macOS asks for permission the first time Jarvis does each thing. Click **Allow** / **OK** each time:

| Prompt | Why |
| ------ | --- |
| "Terminal" would like to access the microphone | Hearing your claps. |
| "Terminal" wants access to control "Spotify" / "Google Chrome" / "System Events" | Playing the song, opening windows. |
| Accessibility access | Putting Chrome and Cursor into fullscreen. Open **System Settings → Privacy & Security → Accessibility** and switch on **Terminal**. |

If you click "Don't Allow" by mistake, turn it back on in **System Settings → Privacy & Security** under **Microphone**, **Automation** or **Accessibility**, then quit Terminal (Cmd+Q) and start again. Without Accessibility access, everything still opens, but windows only fill the screen instead of going fullscreen.

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
  - *volume is almost 0*: set `SPOTIFY_VOLUME=60` in `.env`.
  - Also check your Mac isn't muted. Jarvis warns if it is.
- **Windows don't go fullscreen:** Give Terminal Accessibility access (see above).
- **No welcome speech:** Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env`, and look for an `ElevenLabs TTS failed` line in Terminal.
- **`zsh: permission denied: ./start_jarvis.sh`:** Run `chmod +x start_jarvis.sh` once.
