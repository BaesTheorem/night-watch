"""Shared helpers for Night Watch: config loading, geo math, logging."""
import json
import math
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

# launchd starts services with a minimal PATH that omits Homebrew. Several pieces
# (ffmpeg directly, and mlx-whisper which shells out to ffmpeg to decode audio)
# need it, so put the usual bin dirs back on PATH for any process that imports us.
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")


def load_config():
    """Load the gitignored config.json. Fail loudly with a pointer if missing."""
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "config.json not found. Copy config.example.json to config.json and "
            "fill in your home coordinates (and Broadcastify login for the scanner). "
            "See README.md."
        )
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if cfg.get("vault_dir"):
        cfg["vault_dir"] = os.path.expanduser(cfg["vault_dir"])
    return cfg


def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points in statute miles."""
    r = 3958.7613  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def log(msg):
    """Timestamped line to stderr (captured by launchd into logs/)."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr, flush=True)
