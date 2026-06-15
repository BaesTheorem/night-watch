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
import os
import re
import subprocess
import time
import wave
from datetime import datetime

from lib.common import ROOT, load_config, log

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


def transcribe(wav_path, model):
    import mlx_whisper
    res = mlx_whisper.transcribe(wav_path, path_or_hf_repo=model, language="en")
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

    quiet = in_quiet_hours(cfg)
    msg = f"{reason} near you at {stamp}" if severity == "nearby" else f"{reason} on the scanner at {stamp}"
    notify(cfg, msg, severity, quiet)
    log(f"alert[{severity}]: {reason} :: {snippet}")


def notify(cfg, msg, severity, quiet):
    """Deliver an alert. If alerts.notify_command is set, run it as
    `<command> <message> <severity>` (point it at any notifier you like).
    Otherwise fall back to a native macOS notification."""
    cmd = cfg["alerts"].get("notify_command")
    if cmd and not quiet:
        try:
            args = ([cmd] if isinstance(cmd, str) else list(cmd)) + [msg, severity]
            subprocess.run(args, timeout=20, check=False)
            return
        except Exception as e:  # noqa: BLE001
            log(f"alert: notify_command failed {e!r}")
    script = f'display notification "{msg}" with title "Night Watch"'
    if severity == "priority" and not quiet:
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
    model = cfg["scanner"].get("whisper_model", "mlx-community/whisper-small.en-mlx")
    os.makedirs(DATA, exist_ok=True)
    log(f"transcribe: watching spool/, model={model}")
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
                text = transcribe(wav, model)
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
