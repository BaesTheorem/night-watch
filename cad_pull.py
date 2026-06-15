#!/usr/bin/env python3
"""
Night Watch — real-time CAD / calls-for-service layer (optional).

Unlike the crime layer (geocoded but published weeks late), many cities publish a
genuinely real-time "calls for service" / dispatch feed. Where one exists, this
pulls recent calls within your radius, writes them for the map, and (optionally)
alerts on new ones — true near-real-time awareness without the scanner.

Enable + point it at your city in config.json under `cad` (disabled by default;
see config.example.json for a working Seattle example). KC's own CAD lags, so
this is off for KC.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from lib.common import ROOT, haversine_miles, load_config, log

DEFAULT_FIELDS = {
    "id": "incident_number", "datetime": "datetime", "type": "type",
    "address": "address", "location": "report_location",
    "lat": "latitude", "lng": "longitude",
}


def soda_get(domain, dataset, params, app_token=""):
    host = domain if domain.startswith("http") else f"https://{domain}"
    url = f"{host}/resource/{dataset}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "night-watch/1.0"})
    if app_token:
        req.add_header("X-App-Token", app_token)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def pull(cfg):
    c = cfg.get("cad") or {}
    if not c.get("enabled") or not c.get("incidents_dataset"):
        return None
    f = {**DEFAULT_FIELDS, **c.get("fields", {})}
    home = cfg["home"]
    radius_m = cfg["radius_miles"] * 1609.344
    radius_mi = cfg["radius_miles"]
    cutoff = (datetime.now() - timedelta(hours=c.get("lookback_hours", 24))).strftime("%Y-%m-%dT%H:%M:%S")

    # Prefer a point column (server-side geo); else pull recent and filter client-side.
    if f.get("location"):
        where = (f"within_circle({f['location']}, {home['lat']}, {home['lng']}, {radius_m}) "
                 f"AND {f['datetime']} > '{cutoff}'")
    else:
        where = f"{f['datetime']} > '{cutoff}'"
    rows = soda_get(c.get("domain", ""), c["incidents_dataset"],
                    {"$where": where, "$order": f"{f['datetime']} DESC", "$limit": 2000},
                    c.get("app_token", ""))

    out = []
    for r in rows:
        lat = lng = None
        loc = r.get(f.get("location") or "", {})
        if isinstance(loc, dict) and loc.get("coordinates"):
            lng, lat = loc["coordinates"][0], loc["coordinates"][1]
        elif r.get(f.get("lat")) and r.get(f.get("lng")):
            lat, lng = float(r[f["lat"]]), float(r[f["lng"]])
        if lat is None:
            continue
        dist = haversine_miles(home["lat"], home["lng"], lat, lng)
        if dist > radius_mi:  # client-side filter for the no-point path
            continue
        out.append({
            "id": r.get(f["id"]),
            "time": (r.get(f["datetime"]) or "").replace("T", " ")[:16],
            "type": r.get(f["type"], "Unknown"),
            "address": (r.get(f["address"]) or "").strip(),
            "lat": lat, "lng": lng, "dist_mi": round(dist, 2),
        })
    return out


def main():
    cfg = load_config()
    items = pull(cfg)
    if items is None:
        log("cad: disabled (set cad.enabled + cad.incidents_dataset in config.json)")
        return
    log(f"cad: {len(items)} live calls within {cfg['radius_miles']} mi")

    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "cad.json"), "w") as fp:
        json.dump({"generated": datetime.now().isoformat(), "incidents": items}, fp, indent=2)

    # optional alerting on genuinely new calls (deduped by id)
    if cfg.get("cad", {}).get("alert"):
        seen_path = os.path.join(data_dir, "cad_seen.json")
        first_run = not os.path.exists(seen_path)
        seen = set(json.load(open(seen_path))) if not first_run else set()
        new = [i for i in items if i["id"] and i["id"] not in seen]
        seen.update(i["id"] for i in items if i["id"])
        with open(seen_path, "w") as fp:
            json.dump(sorted(seen), fp)
        if not first_run and new:
            from scanner_transcribe import match_alerts, notify
            for i in new:
                sev = "priority" if match_alerts(i["type"], cfg) else "nearby"
                notify(cfg, f"{i['type']} at {i['address']} ({i['dist_mi']} mi)", sev, quiet=False)
            log(f"cad: alerted on {len(new)} new call(s)")


if __name__ == "__main__":
    main()
