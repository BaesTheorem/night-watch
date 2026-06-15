#!/usr/bin/env python3
"""
Night Watch — desktop GUI.

Flask backend + pywebview window. Read-only monitoring (live transcript, alerts,
crime map, service health) plus control (edit config, switch feeds, start/stop
services). Runs on http://127.0.0.1:5017.
"""
import glob
import json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

from lib.common import CONFIG_PATH, ROOT, load_config

DATA = os.path.join(ROOT, "data")
SPOOL = os.path.join(ROOT, "spool")
PORT = 5018
SERVICES = {
    "capture": "com.nightwatch.capture",
    "transcribe": "com.nightwatch.transcribe",
    "crime": "com.nightwatch.crime",
}
# crime is a weekly StartCalendarInterval job, not a daemon — it's "scheduled",
# not "on/off". Don't bootout it (that would unschedule the weekly refresh).
SCHEDULED = {"crime"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_feeds_cache = {}

app = Flask(__name__, static_folder=os.path.join(ROOT, "static"))


# ---- helpers ---------------------------------------------------------------

def _vault_dir():
    try:
        return load_config().get("vault_dir", "")
    except SystemExit:
        return ""


def _launchctl_running():
    """Map service-key -> running bool from `launchctl list`."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {k: False for k in SERVICES}
    running = {}
    for key, label in SERVICES.items():
        running[key] = any(
            label in line and not line.startswith("-\t") and line.split("\t")[0].strip().lstrip("-").isdigit()
            for line in out.splitlines() if label in line
        )
    return running


# ---- static ----------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(os.path.join(ROOT, "static"), "index.html")


# ---- read APIs --------------------------------------------------------------

@app.get("/api/status")
def api_status():
    running = _launchctl_running()
    spool = len(glob.glob(os.path.join(SPOOL, "seg-*.wav")))
    tlog = os.path.join(DATA, f"transcript-{datetime.now():%Y%m%d}.log")
    lines_today = sum(1 for _ in open(tlog)) if os.path.exists(tlog) else 0
    try:
        cfg = load_config()
        feeds = cfg["scanner"].get("feeds") or [{"id": cfg["scanner"].get("feed_id", "")}]
    except SystemExit:
        feeds = []
    services = {k: {"running": running[k], "kind": "scheduled" if k in SCHEDULED else "daemon"}
                for k in SERVICES}
    crime_path = os.path.join(DATA, "crime.json")
    crime_last = None
    if os.path.exists(crime_path):
        try:
            crime_last = json.load(open(crime_path)).get("generated", "")[:16].replace("T", " ")
        except (ValueError, OSError):
            crime_last = None
    return jsonify({
        "services": services,
        "crime_last_run": crime_last,
        "spool_queue": spool,
        "transcribed_today": lines_today,
        "feeds": feeds,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/transcript")
def api_transcript():
    n = int(request.args.get("n", 60))
    tlog = os.path.join(DATA, f"transcript-{datetime.now():%Y%m%d}.log")
    rows = []
    if os.path.exists(tlog):
        for line in open(tlog).read().splitlines()[-n:]:
            parts = line.split("\t")
            if len(parts) == 3:
                rows.append({"time": parts[0], "feed": parts[1], "text": parts[2]})
            elif len(parts) == 2:
                rows.append({"time": parts[0], "feed": "?", "text": parts[1]})
    rows.reverse()
    return jsonify(rows)


@app.get("/api/alerts")
def api_alerts():
    note = os.path.join(_vault_dir(), "Scanner Alerts.md")
    rows = []
    if os.path.exists(note):
        for line in open(note).read().splitlines():
            m = re.match(r"- \*\*(.+?)\*\* \[(\w+)\] \*\*(.+?)\*\*(?: _\(feed (.+?)\)_)? — (.+)", line)
            if m:
                rows.append({"when": m.group(1), "severity": m.group(2),
                             "reason": m.group(3), "feed": m.group(4) or "?", "text": m.group(5)})
    rows.reverse()
    return jsonify(rows[:100])


@app.get("/api/crime")
def api_crime():
    path = os.path.join(DATA, "crime.json")
    if not os.path.exists(path):
        return jsonify({"generated": None, "incidents": []})
    return jsonify(json.load(open(path)))


@app.get("/api/config")
def api_config_get():
    if not os.path.exists(CONFIG_PATH):
        return jsonify({"error": "no config.json"}), 404
    cfg = json.load(open(CONFIG_PATH))
    cfg.setdefault("scanner", {})["password"] = "********" if cfg.get("scanner", {}).get("password") else ""
    return jsonify(cfg)


@app.get("/api/feeds")
def api_feeds():
    """Live KC Metro feed directory (cached 1h)."""
    now = datetime.now().timestamp()
    if _feeds_cache.get("at", 0) > now - 3600 and _feeds_cache.get("data"):
        return jsonify(_feeds_cache["data"])
    feeds = []
    try:
        cfg = load_config()
        directory = cfg["scanner"].get("directory_url", "https://www.broadcastify.com/listen/mid/63")
    except SystemExit:
        directory = "https://www.broadcastify.com/listen/mid/63"
    try:
        req = urllib.request.Request(directory, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        for m in re.finditer(r'/listen/feed/(\d+)"[^>]*>([^<]+)<', html):
            fid, name = m.group(1), m.group(2).strip()
            if fid not in {f["id"] for f in feeds}:
                feeds.append({"id": fid, "name": name})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e), "feeds": []})
    _feeds_cache.update(at=now, data=feeds)
    return jsonify(feeds)


# ---- write / control APIs ---------------------------------------------------

@app.post("/api/config")
def api_config_post():
    incoming = request.get_json(force=True)
    cur = json.load(open(CONFIG_PATH)) if os.path.exists(CONFIG_PATH) else {}
    # never overwrite the stored password with the masked placeholder
    pw = incoming.get("scanner", {}).get("password", "")
    if pw in ("", "********"):
        incoming.setdefault("scanner", {})["password"] = cur.get("scanner", {}).get("password", "")
    json.dump(incoming, open(CONFIG_PATH, "w"), indent=2)
    return jsonify({"ok": True})


@app.post("/api/geocode")
def api_geocode():
    addr = request.get_json(force=True).get("address", "")
    if not addr:
        return jsonify({"error": "no address"}), 400
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": addr, "format": "json", "limit": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "night-watch/1.0"})
    res = json.load(urllib.request.urlopen(req, timeout=20))
    if not res:
        return jsonify({"error": "not found"}), 404
    return jsonify({"lat": float(res[0]["lat"]), "lng": float(res[0]["lon"]),
                    "display": res[0]["display_name"]})


@app.post("/api/service/<key>/<action>")
def api_service(key, action):
    if key not in SERVICES or action not in {"start", "stop", "restart", "run"}:
        return jsonify({"error": "bad request"}), 400
    label = SERVICES[key]
    uid = os.getuid()
    # never bootout a scheduled job — that unschedules it. Any action on a
    # scheduled service just means "run now".
    if key in SCHEDULED:
        action = "run"
    try:
        if action == "stop":
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], timeout=10)
        elif action in ("start", "restart", "run"):
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], timeout=15)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.post("/api/run-crime")
def api_run_crime():
    py = os.path.join(ROOT, ".venv", "bin", "python")
    subprocess.Popen([py, os.path.join(ROOT, "crime_pull.py")], cwd=ROOT)
    return jsonify({"ok": True})


def _serve():
    app.run(host="127.0.0.1", port=PORT, threaded=True)


def main():
    threading.Thread(target=_serve, daemon=True).start()
    try:
        import webview
        webview.create_window("Night Watch", f"http://127.0.0.1:{PORT}",
                              width=1180, height=820, min_size=(900, 640))
        webview.start()
    except Exception:
        # headless / no display — just serve
        import time
        print(f"Night Watch serving at http://127.0.0.1:{PORT}")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
