#!/usr/bin/env python3
"""
Night Watch — KC crime/CAD context layer.

Pulls geocoded KCPD incident data from the city's open-data portal (Socrata),
filters to a radius around home, and writes a "Neighborhood Watch" digest note
to the Obsidian vault.

This data is OFFICIAL and geocoded, but KC publishes it with a multi-week lag,
so it is the situational-context layer, not the live-alert layer. The scanner
pipeline (scanner_*.py) provides real-time alerting.
"""
import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from lib.common import ROOT, haversine_miles, load_config, log

SODA_HOST = "https://data.kcmo.org"


def soda_get(dataset, params, app_token=""):
    url = f"{SODA_HOST}/resource/{dataset}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "night-watch/1.0"})
    if app_token:
        req.add_header("X-App-Token", app_token)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def pull_incidents(cfg):
    c = cfg["crime"]
    home = cfg["home"]
    radius_m = cfg["radius_miles"] * 1609.344
    cutoff = (datetime.now() - timedelta(days=c["lookback_days"])).strftime("%Y-%m-%dT00:00:00")
    where = (
        f"within_circle(location, {home['lat']}, {home['lng']}, {radius_m}) "
        f"AND report_date > '{cutoff}'"
    )
    rows = soda_get(
        c["incidents_dataset"],
        {"$where": where, "$order": "report_date DESC", "$limit": 5000},
        c.get("app_token", ""),
    )
    out = []
    for r in rows:
        loc = r.get("location", {})
        coords = loc.get("coordinates") if isinstance(loc, dict) else None
        dist = None
        if coords:
            dist = haversine_miles(home["lat"], home["lng"], coords[1], coords[0])
        out.append({
            "report": r.get("report"),
            "date": r.get("report_date", "")[:10],
            "offense": r.get("offense", "Unknown"),
            "description": r.get("description", ""),
            "address": (r.get("address") or "").strip(),
            "zip": r.get("zipcode", ""),
            "lat": coords[1] if coords else None,
            "lng": coords[0] if coords else None,
            "dist_mi": round(dist, 2) if dist is not None else None,
        })
    out.sort(key=lambda x: (x["date"]), reverse=True)
    return out


def render_note(cfg, incidents):
    home = cfg["home"]
    radius = cfg["radius_miles"]
    now = datetime.now()
    by_type = Counter(i["offense"] for i in incidents)
    newest = incidents[0]["date"] if incidents else "n/a"
    lag_days = ""
    if incidents:
        try:
            lag_days = f" (~{(now - datetime.strptime(newest, '%Y-%m-%d')).days} days behind)"
        except ValueError:
            pass

    lines = [
        "---",
        "type: neighborhood_watch",
        f"generated: {now.isoformat(timespec='seconds')}",
        f"radius_miles: {radius}",
        "source: KC Open Data (KCPD), official + geocoded, multi-week publishing lag",
        "---",
        f"### Neighborhood Watch — crime within {radius} mi of {home['label']}",
        "",
        f"> [!info] Context layer, not live alerts. KCPD publishes this data with a lag. "
        f"Most recent incident here is **{newest}**{lag_days}. Real-time awareness comes "
        f"from the scanner feed, not this note.",
        "",
        f"**{len(incidents)} incidents** in the last {cfg['crime']['lookback_days']} days of available data.",
        "",
        "#### By type",
    ]
    for offense, n in by_type.most_common():
        lines.append(f"- {offense}: {n}")
    lines += ["", "#### Most recent (closest first within each day)", ""]
    lines.append("| Date | Offense | Distance | Address |")
    lines.append("|---|---|---|---|")
    for i in sorted(incidents, key=lambda x: (x["date"], x["dist_mi"] if x["dist_mi"] is not None else 9), reverse=True)[:40]:
        d = f"{i['dist_mi']} mi" if i["dist_mi"] is not None else "?"
        lines.append(f"| {i['date']} | {i['offense']} | {d} | {i['address']} |")
    lines.append("")
    return "\n".join(lines)


def main():
    cfg = load_config()
    log("crime_pull: querying KC open data within radius")
    incidents = pull_incidents(cfg)
    log(f"crime_pull: {len(incidents)} incidents within {cfg['radius_miles']} mi")

    # persist raw
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "crime.json"), "w") as f:
        json.dump({"generated": datetime.now().isoformat(), "incidents": incidents}, f, indent=2)

    # write vault note
    vault_dir = cfg["vault_dir"]
    os.makedirs(vault_dir, exist_ok=True)
    note_path = os.path.join(vault_dir, "Neighborhood Watch.md")
    with open(note_path, "w") as f:
        f.write(render_note(cfg, incidents))
    log(f"crime_pull: wrote {note_path}")


if __name__ == "__main__":
    main()
