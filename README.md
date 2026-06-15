# Night Watch

A two-layer neighborhood safety feed for Kansas City, built because no clean,
legal, real-time, geocoded "what's happening near me" feed exists off the shelf
(Citizen is a closed ToS-violating scrape; PulsePoint locked its API behind a
login in June 2026; KC's open crime data is geocoded but published weeks late).

So Night Watch combines the two halves that *are* available legitimately:

| Layer | Source | Nature |
|---|---|---|
| **Context** | KC Open Data (KCPD), Socrata API | Official, geocoded, but **multi-week lag**. Weekly digest of crime near home. |
| **Real-time** | Broadcastify live scanner feed + local transcription | Live dispatch audio, transcribed on-device with mlx-whisper, matched for near-home streets and priority incidents. |

The context layer keeps it honest and grounded; the scanner layer is the actual
"alert me now" capability. Neither depends on a service that can revoke access
or a ToS we're violating.

## How it works

```
crime_pull.py     KC Socrata  ──►  "Neighborhood Watch.md"  (weekly)
scanner_capture.py   Broadcastify ──► spool/*.wav  (rolling 30s segments)
scanner_transcribe.py  spool/ ──► mlx-whisper ──► match ──► mist-notify + "Scanner Alerts.md"
```

- Capture holds one ffmpeg connection to the feed and writes 16 kHz mono WAV
  segments. It auto-reconnects on drops.
- Transcribe gates out dead air (RMS energy) so Whisper doesn't hallucinate on
  silence, transcribes real traffic locally, and matches each line against your
  `near_streets` and `priority_keywords`.
- Matches are spoken via MIST (`mist-notify`) and appended to a vault note.
  Rate-limited to one alert per reason per 10 minutes; silent during quiet hours.

## Setup

You need two things, both kept out of git:

1. **`config.json`** — copy `config.example.json` to `config.json` and fill in:
   - `home.lat` / `home.lng` — your home coordinates (look up your address on
     any map, right-click → coordinates). Drives the radius filter. **Never
     committed.**
   - `radius_miles` — how close counts as "near home" (0.75 is a good start).
   - `alerts.near_streets` — lowercased street names in/around your radius. These
     are what get matched against spoken dispatch audio. The more complete, the
     better the geo-filtering.
   - `scanner.username` / `scanner.password` — a **free** Broadcastify account.
     Needed because free live feeds are account-gated. *Or* paste a
     `scanner.stream_url_override` grabbed from the logged-in web player's
     Network tab (more reliable; URLs can rotate).

2. **Python venv** (transcriber only):
   ```sh
   python3 -m venv .venv && .venv/bin/pip install mlx-whisper
   ```
   The model downloads on first run and caches.

### Run it

```sh
# context layer — try it immediately, no account needed:
python3 crime_pull.py

# scanner layer (after config.json has Broadcastify creds):
python3 scanner_capture.py        # terminal 1
.venv/bin/python scanner_transcribe.py   # terminal 2
```

### Desktop GUI

A full-featured control panel (`app.py`, Flask + pywebview, http://127.0.0.1:5018):

- **Dashboard** — service health pills (click to start/stop), live transcript
  stream (feed-tagged), scanner alerts, counters.
- **Map** — Leaflet map with your home marker, radius ring, and color-coded crime
  pins (red = violent, amber = property), labeled with the data's publish lag.
- **Settings** — geocode your address, set radius, add/remove feeds from the live
  KC directory, edit near-streets / priority keywords / quiet hours, manage the
  Broadcastify login. Save and optionally restart capture in one click.

Launch from `~/Desktop/Apps/Night Watch.app`, or `.venv/bin/python app.py`.

### Run it as services (launchd)

```sh
./install.sh
```

This fills the `launchd/*.plist.template` files in for your checkout, writes real
plists to `~/Library/LaunchAgents`, and loads them. `com.nightwatch.crime` runs
weekly; `com.nightwatch.capture` and `com.nightwatch.transcribe` are KeepAlive
and run continuously. (The templates use placeholders rather than hardcoded
paths, so nothing personal is committed.)

## Privacy

Your home coordinates, street list, Broadcastify credentials, captured audio, and
transcripts all live in gitignored paths (`config.json`, `spool/`, `data/`,
`logs/`, `notify-*.sh`). Only `config.example.json` (generic placeholder coords)
is committed. The launchd plists are placeholder templates filled in at install
time, so no machine-specific paths are committed either.

## Responsible use

This is built for **personal, local** situational awareness. It captures a free
public Broadcastify stream to your own machine and transcribes it on-device. Use
it within Broadcastify's Terms of Service: don't rebroadcast or redistribute the
audio, and don't republish transcripts. It reads only **public** open-data and
public-safety radio. The transcription is imperfect; never treat an alert as a
verified fact or act on it as if it were dispatched to you.

## Cost

$0 as configured. Free Broadcastify account, on-device transcription. Broadcastify
Premium ($30/yr) would only add ad-free streaming and 365-day archive backfill
for overnight gaps — optional, add it only if the feed proves its worth.

## Honest limitations

- **Context layer lags weeks.** KC publishes crime data late. It's for patterns,
  not "right now." The note self-labels how far behind it is.
- **Scanner geo-filtering is good, not perfect.** Dispatchers speak addresses;
  matching spoken street names from noisy, jargon-heavy audio will miss some and
  false-positive on others. Tune `near_streets` and `SILENCE_RMS` over time.
- **Capture needs the Mac awake.** Overnight sleep = gaps (Premium archive could
  backfill, not wired up).
