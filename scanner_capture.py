#!/usr/bin/env python3
"""
Night Watch — scanner capture.

Connects to one or more Broadcastify live feeds and writes rolling 16 kHz mono
WAV segments into spool/ for the transcriber. One ffmpeg process per feed;
segment filenames are tagged with the feed id (seg-<feed>-<timestamp>.wav).
Runs forever (launchd KeepAlive); dead ffmpegs are relaunched.

Broadcastify free feeds are account-gated and the stream URL is session-tokened.
Supply EITHER:
  - scanner.feeds: [{"id": "36219", "name": "KC Fire"}, ...]  (preferred, multi)
  - scanner.feed_id: "23630"                                   (single, legacy)
plus scanner.username / scanner.password (free account), or a per-feed
"stream_url" override grabbed from the logged-in web player's Network tab.
"""
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

from lib.common import ROOT, load_config, log

SPOOL = os.path.join(ROOT, "spool")
LOGIN_URL = "https://www.broadcastify.com/login/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# launchd does not inherit the user's PATH, so resolve ffmpeg absolutely.
FFMPEG = next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)),
    "ffmpeg",
)


def feeds_for(cfg):
    """Normalize config into a list of {id, name, stream_url}."""
    sc = cfg["scanner"]
    feeds = sc.get("feeds")
    if not feeds:
        feeds = [{"id": sc.get("feed_id", ""), "name": sc.get("feed_id", "")}]
    out = []
    for f in feeds:
        if isinstance(f, str):
            f = {"id": f, "name": f}
        if f.get("id"):
            out.append({"id": str(f["id"]), "name": f.get("name") or str(f["id"]),
                        "stream_url": f.get("stream_url", "")})
    return out


def login(cfg):
    """Return an authenticated urllib opener, or None if no creds."""
    sc = cfg["scanner"]
    user, pw = sc.get("username"), sc.get("password")
    if not (user and pw):
        return None
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    html = op.open(LOGIN_URL, timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'name="__token"\s+value="([^"]+)"', html)
    token = m.group(1) if m else ""
    data = urllib.parse.urlencode(
        {"username": user, "password": pw, "action": "auth", "__token": token}
    ).encode()
    op.open(LOGIN_URL, data=data, timeout=30).read()
    return op


def resolve_stream(op, feed):
    """Resolve a feed's live stream URL (override wins; else scrape player)."""
    if feed.get("stream_url"):
        return feed["stream_url"]
    if op is None:
        sys.exit(
            "No Broadcastify session and no stream_url override. Set scanner."
            "username/password (free account) or a per-feed stream_url. See README."
        )
    page = op.open(
        f"https://www.broadcastify.com/webPlayer/{feed['id']}", timeout=30
    ).read().decode("utf-8", "replace")
    m = re.search(r'https?://[\w.-]*(?:cdnstream\d*|audio\d*\.broadcastify)[\w./-]+', page)
    if not m:
        sys.exit(
            f"Logged in but couldn't find the stream URL for feed {feed['id']}. "
            "Use a per-feed stream_url override. See README."
        )
    return m.group(0)


def spawn(feed, stream_url, seg):
    out = os.path.join(SPOOL, f"seg-{feed['id']}-%Y%m%d-%H%M%S.wav")
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
        "-user_agent", UA, "-i", stream_url,
        "-ar", "16000", "-ac", "1",
        "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
        "-strftime", "1", out,
    ]
    log(f"capture: starting feed {feed['id']} ({feed['name']})")
    return subprocess.Popen(cmd)


def main():
    cfg = load_config()
    os.makedirs(SPOOL, exist_ok=True)
    seg = int(cfg["scanner"].get("segment_seconds", 30))
    feeds = feeds_for(cfg)
    if not feeds:
        sys.exit("No feeds configured. Set scanner.feeds or scanner.feed_id. See README.")

    op = login(cfg)
    procs = {}  # feed_id -> (feed, Popen)
    for f in feeds:
        try:
            procs[f["id"]] = (f, spawn(f, resolve_stream(op, f), seg))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"capture: feed {f['id']} failed to start: {e!r}")

    # supervise: relaunch any ffmpeg that dies (re-login if the session went stale)
    while True:
        time.sleep(10)
        for fid, (f, p) in list(procs.items()):
            if p.poll() is not None:
                log(f"capture: feed {fid} exited ({p.returncode}); relaunching")
                try:
                    if op is None or not f.get("stream_url"):
                        op = login(cfg)
                    procs[fid] = (f, spawn(f, resolve_stream(op, f), seg))
                except Exception as e:  # noqa: BLE001
                    log(f"capture: feed {fid} relaunch failed: {e!r}; retry in 10s")


if __name__ == "__main__":
    main()
