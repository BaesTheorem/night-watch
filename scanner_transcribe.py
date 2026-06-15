#!/usr/bin/env python3
"""
Night Watch — transcribe + alert.

Watches spool/ for finished WAV segments, transcribes them locally with
mlx-whisper (Apple Silicon), then matches each transcript against:

  - alerts.priority_keywords  -> always alert (spoken via mist-notify)
  - alerts.near_streets       -> alert when a street near home is mentioned

Alerts are spoken (unless in quiet_hours, then silent) and appended to a
"Scanner Alerts" note in the vault. Raw transcripts are kept gitignored in
data/ for later review. Runs forever (launchd KeepAlive).
"""
import array
import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime

from lib.common import CONFIG_PATH, ROOT, load_config, log

SPOOL = os.path.join(ROOT, "spool")
DATA = os.path.join(ROOT, "data")

_recent = {}  # alert-key -> last fired epoch (rate limiting)
_model = None


# Whisper hallucinates phantom phrases ("you", "thank you") on silence, and a
# scanner feed is mostly dead air. Gate on RMS energy so we only transcribe
# segments that actually contain radio traffic.
SILENCE_RMS = 250  # 16-bit PCM; tune if real traffic gets skipped or silence leaks

# Free Broadcastify feeds inject a pre-roll ad at (re)connect. It can't trigger a
# false alert (ad copy won't match streets/keywords) but it pollutes transcripts,
# so drop lines that look like an ad read rather than dispatch.
AD_MARKERS = (
    "mistplay", "gift card", "download", "promo code", "broadcastify premium",
    "ad-free", "sign up", "use code", "terms apply", "this ad",
)


def looks_like_ad(text):
    low = text.lower()
    return sum(m in low for m in AD_MARKERS) >= 2


def likely_silent(wav_path):
    try:
        with wave.open(wav_path, "rb") as w:
            if w.getsampwidth() != 2:
                return False  # only know how to gate 16-bit; let it through
            frames = w.readframes(w.getnframes())
        if not frames:
            return True
        samples = array.array("h")
        samples.frombytes(frames)
        if not samples:
            return True
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return rms < SILENCE_RMS
    except (wave.Error, EOFError):
        return False


# Scanner audio is the worst case for Whisper: lossy radio codecs, clipped
# speech, ten-codes, and the exact tokens we alert on (street names) are rare
# words it loves to mangle. An initial_prompt biases the decoder toward the
# vocabulary we care about. We seed it with the config's streets + keywords plus
# a fixed dispatch phrasebook, so "prospect" stays "Prospect" not "prospectus".
_DISPATCH_PHRASEBOOK = (
    "Police and fire radio dispatch. Units, cross streets, and call types. "
    "Copy, dispatch, en route, on scene, code three, signal, "
    "shots fired, structure fire, EMS, ambulance, suspect, vehicle, "
    "northbound, southbound, eastbound, westbound, avenue, boulevard, terrace."
)


def build_prompt(cfg):
    """Domain prompt to bias the decoder. Explicit config override wins;
    otherwise auto-assemble from the streets/keywords we alert on."""
    override = (cfg["scanner"].get("whisper_prompt") or "").strip()
    if override:
        return override
    alerts = cfg.get("alerts", {})
    terms = list(alerts.get("near_streets", [])) + list(alerts.get("priority_keywords", []))
    seen, vocab = set(), []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            vocab.append(t.title() if t.islower() else t)
    prompt = _DISPATCH_PHRASEBOOK
    if vocab:
        prompt += " Streets and incidents near here: " + ", ".join(vocab) + "."
    return prompt


def transcribe(wav_path, model, prompt=None):
    import mlx_whisper
    res = mlx_whisper.transcribe(
        wav_path, path_or_hf_repo=model, language="en",
        initial_prompt=prompt or None,
        # Each segment is an independent radio burst — don't let one transcript
        # seed the next, which is how Whisper spirals into repeated phantoms.
        condition_on_previous_text=False,
        # Greedy, no temperature fallback: deterministic and avoids the
        # higher-temperature re-decodes that invent text on marginal audio.
        temperature=0.0,
        # Hallucination guards: drop output that's too repetitive (high
        # compression ratio), too low-confidence, or reads as silence.
        compression_ratio_threshold=2.4,
        logprob_threshold=-1.0,
        no_speech_threshold=0.6,
    )
    return (res.get("text") or "").strip()


def in_quiet_hours(cfg):
    qh = cfg["alerts"].get("quiet_hours") or []
    if not qh:
        return False
    hour = datetime.now().hour
    for span in qh:  # e.g. ["23-6"] meaning 11pm..6am
        try:
            a, b = (int(x) for x in span.split("-"))
        except (ValueError, AttributeError):
            continue
        if a <= b:
            if a <= hour < b:
                return True
        else:  # wraps midnight
            if hour >= a or hour < b:
                return True
    return False


def match_alerts(text, cfg):
    """Return list of (severity, reason) for a transcript line."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    hits = []
    for kw in cfg["alerts"].get("priority_keywords", []):
        if f" {kw.lower()} " in low or kw.lower() in low:
            hits.append(("priority", kw))
    for st in cfg["alerts"].get("near_streets", []):
        if st.lower() in low:
            hits.append(("nearby", st))
    return hits


def feed_of(wav_path):
    """Extract the feed id from a seg-<feed>-<timestamp>.wav filename."""
    m = re.match(r"seg-([^-]+)-\d", os.path.basename(wav_path))
    return m.group(1) if m else "?"


LIVE_PATH = os.path.join(DATA, "live.json")
_geocache = {}


def locate(text, cfg):
    """Best-effort approximate geocode of a place named in a transcript, bounded
    to near home (a mentioned near-street, enriched with a house number or cross
    street when present). Returns (lat, lng) or None. Approximate by nature —
    scanner audio is spoken and noisy."""
    home = cfg["home"]
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    streets = [s.lower() for s in cfg["alerts"].get("near_streets", [])]
    hit = next((s for s in streets if s in low), None)
    if not hit:
        return None
    q = hit
    m = re.search(r"(\d{2,5})\s+(?:[a-z]+\s+){0,2}" + re.escape(hit), low)
    if m:
        q = f"{m.group(1)} {hit}"
    else:
        m2 = re.search(re.escape(hit) + r"\s+(?:and|at|&)\s+([a-z0-9]+(?:\s+[a-z]+)?)", low)
        if m2:
            q = f"{hit} and {m2.group(1).strip()}"
    if q in _geocache:
        return _geocache[q]
    d = 0.06  # ~4 mi box around home so results stay local
    viewbox = f"{home['lng']-d},{home['lat']+d},{home['lng']+d},{home['lat']-d}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "viewbox": viewbox, "bounded": 1})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "night-watch/1.0"})
        res = json.load(urllib.request.urlopen(req, timeout=15))
        out = (float(res[0]["lat"]), float(res[0]["lon"])) if res else None
    except Exception:  # noqa: BLE001
        out = None
    _geocache[q] = out
    return out


def append_live(cfg, rec):
    """Append a located incident to the rolling live feed (last 6h, max 80)."""
    try:
        items = json.load(open(LIVE_PATH)) if os.path.exists(LIVE_PATH) else []
    except (ValueError, OSError):
        items = []
    items.append(rec)
    cutoff = datetime.now().timestamp() - 6 * 3600

    def ts(r):
        try:
            return datetime.strptime(r["time"], "%Y-%m-%d %H:%M").timestamp()
        except (ValueError, KeyError):
            return 0
    items = [r for r in items if ts(r) >= cutoff][-80:]
    os.makedirs(DATA, exist_ok=True)
    with open(LIVE_PATH, "w") as f:
        json.dump(items, f)


def fire(cfg, severity, reason, text, feed="?"):
    key = f"{severity}:{reason.lower()}"
    now = time.time()
    if now - _recent.get(key, 0) < 600:  # 10-min rate limit per key
        return
    _recent[key] = now

    stamp = datetime.now().strftime("%H:%M")
    headline = f"Scanner {severity}: {reason}"
    snippet = text.strip()[:200]

    # vault log
    vault_dir = cfg["vault_dir"]
    os.makedirs(vault_dir, exist_ok=True)
    note = os.path.join(vault_dir, "Scanner Alerts.md")
    new = not os.path.exists(note)
    with open(note, "a") as f:
        if new:
            f.write("### Scanner Alerts\nReal-time matches from the Broadcastify feed. Newest at bottom.\n\n")
        f.write(f"- **{datetime.now().strftime('%Y-%m-%d %H:%M')}** [{severity}] **{reason}** _(feed {feed})_ — {snippet}\n")

    # plot it on the live map if we can approximately locate it
    loc = locate(text, cfg)
    if loc:
        append_live(cfg, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "lat": loc[0], "lng": loc[1],
            "severity": severity, "reason": reason, "text": snippet, "feed": feed,
        })

    quiet = in_quiet_hours(cfg)
    msg = f"{reason} near you at {stamp}" if severity == "nearby" else f"{reason} on the scanner at {stamp}"
    notify(cfg, msg, severity, quiet)
    log(f"alert[{severity}]: {reason} :: {snippet}")


APP_NAME = "Night Watch"
APP_BUNDLE_ID = "com.nightwatch.app"


def voice_enabled():
    """Read alerts.voice fresh from config each call so the GUI toggle takes
    effect on the next alert without restarting the service. Defaults to on."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("alerts", {}).get("voice", True) is not False
    except (ValueError, OSError):
        return True


def notify(cfg, msg, severity, quiet):
    """Deliver an alert. If alerts.notify_command is set, run it as
    `<command> <message> <severity> <voice>` (voice = "1"/"0"; point it at any
    notifier you like). Otherwise post a clickable macOS notification that opens
    the app: prefer terminal-notifier (click activates the app), fall back to
    osascript. The voice flag (alerts.voice) gates spoken voice and alert sound."""
    voice = voice_enabled()
    cmd = cfg["alerts"].get("notify_command")
    if cmd and not quiet:
        try:
            args = ([cmd] if isinstance(cmd, str) else list(cmd)) + [msg, severity, "1" if voice else "0"]
            subprocess.run(args, timeout=20, check=False)
            return
        except Exception as e:  # noqa: BLE001
            log(f"alert: notify_command failed {e!r}")

    tn = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
    if os.path.exists(tn):
        args = [tn, "-title", APP_NAME, "-message", msg,
                "-sender", APP_BUNDLE_ID,
                "-execute", f"open -b {APP_BUNDLE_ID}"]
        if severity == "priority" and not quiet and voice:
            args += ["-sound", "Basso"]
        subprocess.run(args, timeout=20, check=False)
        return

    # fallback: native notification (click opens the posting tool, not the app)
    script = f'display notification "{msg}" with title "{APP_NAME}"'
    if severity == "priority" and not quiet and voice:
        script += ' sound name "Basso"'
    subprocess.run(["osascript", "-e", script], check=False)


def ready_segments(cfg):
    """WAV files that ffmpeg has finished writing (older than segment+5s)."""
    grace = int(cfg["scanner"].get("segment_seconds", 30)) + 5
    now = time.time()
    files = sorted(glob.glob(os.path.join(SPOOL, "seg-*.wav")))
    return [f for f in files if now - os.path.getmtime(f) > grace]


def main():
    cfg = load_config()
    model = cfg["scanner"].get("whisper_model", "mlx-community/whisper-large-v3-turbo")
    prompt = build_prompt(cfg)
    os.makedirs(DATA, exist_ok=True)
    log(f"transcribe: watching spool/, model={model}, prompt={len(prompt)} chars")
    while True:
        segs = ready_segments(cfg)
        if not segs:
            time.sleep(3)
            continue
        for wav in segs:
            if likely_silent(wav):
                os.remove(wav)
                continue
            try:
                text = transcribe(wav, model, prompt)
            except Exception as e:  # noqa: BLE001
                log(f"transcribe: failed on {os.path.basename(wav)}: {e!r}")
                os.remove(wav)
                continue
            if text and not looks_like_ad(text):
                feed = feed_of(wav)
                tlog = os.path.join(DATA, f"transcript-{datetime.now():%Y%m%d}.log")
                with open(tlog, "a") as f:
                    f.write(f"{datetime.now():%H:%M:%S}\t{feed}\t{text}\n")
                for severity, reason in match_alerts(text, cfg):
                    fire(cfg, severity, reason, text, feed)
            os.remove(wav)


if __name__ == "__main__":
    main()
