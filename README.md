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
| `JARVIS_SPIKE_RATIO` | Clap sensitivity: higher = needs louder claps. | `7.0` |
| `JARVIS_SAMPLE_RATE` | Try `48000` if your mic complains. | `44100` |
| `ELEVENLABS_MODEL_ID` / `ELEVENLABS_OUTPUT_FORMAT` / `ELEVENLABS_PCM_SAMPLE_RATE` | Advanced TTS options (format must be `pcm_…`). | `eleven_multilingual_v2` / `pcm_24000` |
| `JARVIS_WELCOME_CACHE_DIR` / `JARVIS_WELCOME_CACHE_ENABLED` | Where the spoken welcome is cached, so ElevenLabs is only called when the phrase or voice changes. | `.cache/jarvis_welcome/` / `true` |

Save the file, then restart Jarvis for changes to take effect.

## Run

```bash
cd ~/jarvis
./start_jarvis.sh
```

Wait for `Ready — clap twice.`, then clap twice quickly. Stop with **Ctrl+C**.

## macOS permissions (first run)

macOS asks for permission the first time Jarvis does each thing. Click **Allow** / **OK** each time:

| Prompt | Why |
| ------ | --- |
| "Terminal" would like to access the microphone | Hearing your claps. |
| "Terminal" wants access to control "Spotify" / "Google Chrome" / "System Events" | Playing the song, opening windows. |
| Accessibility access | Putting Chrome and Cursor into fullscreen. Open **System Settings → Privacy & Security → Accessibility** and switch on **Terminal**. |

If you click "Don't Allow" by mistake, turn it back on in **System Settings → Privacy & Security** under **Microphone**, **Automation** or **Accessibility**, then quit Terminal (Cmd+Q) and start again. Without Accessibility access, everything still opens, but windows only fill the screen instead of going fullscreen.

## Tuning

Clap detection constants are at the top of `jarvis.py`:

| Constant      | Effect                                                            |
| ------------- | ----------------------------------------------------------------- |
| `SPIKE_RATIO` | Increase if you get false triggers; decrease if claps are missed. |
| `COOLDOWN_S`  | Minimum time between two logged claps.                            |
| `BLOCK_MS`    | Larger = slightly less CPU, a bit less precise timing.            |
| `MIN_RMS`     | Floor on how loud a block must be (helps in very quiet rooms).    |
| `SAMPLE_RATE` | Try `48000` if your device does not like `44100`.                 |

## Troubleshooting

- **"Every microphone sounds silent":** Microphone permission is off. Turn on Terminal in **System Settings → Privacy & Security → Microphone**, quit Terminal with Cmd+Q, and start again.
- **Wrong mic:** Jarvis prints the list of audio devices on startup. Put the name (or number) of the one you want in `JARVIS_INPUT_DEVICE`.
- **No reaction to claps:** Lower `JARVIS_SPIKE_RATIO` (e.g. `5`) or clap closer to the Mac.
- **Triggers on random noise:** Raise `JARVIS_SPIKE_RATIO` (e.g. `10`).
- **Spotify opens but doesn't play:** Make sure you're logged in to the Spotify app, and that Terminal is allowed to control Spotify under **Privacy & Security → Automation**.
- **Windows don't go fullscreen:** Give Terminal Accessibility access (see above).
- **No welcome speech:** Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in `.env`, and look for an `ElevenLabs TTS failed` line in Terminal.
- **`zsh: permission denied: ./start_jarvis.sh`:** Run `chmod +x start_jarvis.sh` once.
