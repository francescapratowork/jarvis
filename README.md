# Jarvis for macOS — double clap → welcome home

A Python script that listens to your Mac's microphone. When you **clap twice**, it:

1. plays your song in the **Spotify** app,
2. opens **Claude** and **Tasaradar** in new **Google Chrome** windows (fullscreen, one per display if you have several),
3. speaks a welcome line in your **ElevenLabs** voice through your Mac speakers,
4. brings **Cursor** to the front (fullscreen), launching it if needed.

The welcome runs once per start. To run it again, stop Jarvis (Ctrl+C) and start it again.

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
| `JARVIS_SPIKE_RATIO` | Clap sensitivity: how many times louder than the room a clap must be. Lower = more sensitive. | `4.0` |
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

Stay quiet for the first two seconds while Jarvis measures the room noise. When you see `Ready — clap twice.`, clap twice quickly (a short pause, like "clap–clap"). Stop with **Ctrl+C**.

## Test your claps (nothing opens)

```bash
cd ~/jarvis
./start_jarvis.sh --test
```

Clap as often as you like and watch the Terminal:

```
Room noise: peak 0.0506 (rms 0.0149). Clap threshold set to peak 0.2025.
Ready — clap twice.
Clap 1 heard (peak 0.812, rms 0.1102, threshold 0.206) — clap again within 0.8s...
Clap 2 heard (peak 0.934, rms 0.1240) — DOUBLE CLAP DETECTED!
Level: room noise 0.0512 | loudest 0.9340 (rms 0.1240) | clap threshold 0.2049
```

- **Clap 1 / Clap 2 heard** — a clap was detected, with its measured peak and RMS.
- **Level** (every 5 seconds) — the room noise, the loudest sound since the last line, and how loud a clap must be. Jarvis keeps re-measuring the room, so the threshold adapts on its own.
- If you clap and only see `a sound reached 70% of the threshold`, your claps are too soft for the current setting: lower `JARVIS_SPIKE_RATIO` (e.g. to `3`) in `.env`.

## macOS permissions (first run)

macOS asks for permission the first time Jarvis does each thing. Click **Allow** / **OK** each time:

| Prompt | Why |
| ------ | --- |
| "Terminal" would like to access the microphone | Hearing your claps. |
| "Terminal" wants access to control "Spotify" / "Google Chrome" / "System Events" | Playing the song, opening windows. |
| Accessibility access | Putting Chrome and Cursor into fullscreen. Open **System Settings → Privacy & Security → Accessibility** and switch on **Terminal**. |

If you click "Don't Allow" by mistake, turn it back on in **System Settings → Privacy & Security** under **Microphone**, **Automation** or **Accessibility**, then quit Terminal (Cmd+Q) and start again. Without Accessibility access, everything still opens, but windows only fill the screen instead of going fullscreen.

## Tuning

The clap threshold calibrates itself to your room. Usually the only knob you need is `JARVIS_SPIKE_RATIO` in `.env` (lower = more sensitive, higher = fewer false triggers). Advanced constants are at the top of `jarvis.py` (`BLOCK_MS`, `MIN_DOUBLE_GAP_S`, `MIN_CLAP_PEAK`, `MAX_CLAP_THRESHOLD`, `NOISE_WINDOW_S`, `CALIBRATION_S`).

## Troubleshooting

- **"Every microphone sounds silent":** Microphone permission is off. Turn on Terminal in **System Settings → Privacy & Security → Microphone**, quit Terminal with Cmd+Q, and start again.
- **Wrong mic:** Jarvis prints the list of audio devices on startup. Put the name (or number) of the one you want in `JARVIS_INPUT_DEVICE`.
- **No reaction to claps:** Run `./start_jarvis.sh --test`. If no `Clap heard` lines appear, lower `JARVIS_SPIKE_RATIO` (e.g. `3`) or clap closer to the Mac. If `Clap 1` appears but never `Clap 2`, clap a bit faster (within 0.8 s).
- **Triggers on random noise:** Raise `JARVIS_SPIKE_RATIO` (e.g. `6`).
- **"The room (or mic gain) is very loud":** Lower the input volume in **System Settings → Sound → Input**.
- **Spotify opens but doesn't play:** Make sure you're logged in to the Spotify app, and that Terminal is allowed to control Spotify under **Privacy & Security → Automation**.
- **Windows don't go fullscreen:** Give Terminal Accessibility access (see above).
- **No welcome speech:** Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env`, and look for an `ElevenLabs TTS failed` line in Terminal.
- **`zsh: permission denied: ./start_jarvis.sh`:** Run `chmod +x start_jarvis.sh` once.
