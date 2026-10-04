#!/usr/bin/env python3
"""Download NYC MapPLUTO lot centroids, land use and lot area."""
import csv
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "gis" / "pluto_lots.csv"
URL = "https://services5.arcgis.com/GfwWNkhOj9bNBqoJ/ArcGIS/rest/services/MAPPLUTO/FeatureServer/0/query"
PAGE = 2000

def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "research-ejtir/1.0"
    offset = 0
    if OUT.exists() and OUT.stat().st_size > 0:
        # resume from existing rows
        with OUT.open() as f:
            offset = max(sum(1 for _ in f) - 1, 0)
        print("resume offset", offset, flush=True)
        mode = "a"
    else:
        mode = "w"
    with OUT.open(mode, newline="") as f:
        writer = csv.writer(f)
        if mode == "w":
            writer.writerow(["landuse", "lotarea", "lat", "lon"])
        while True:
            params = {
                "where": "Latitude>0 AND Longitude<-73",
                "outFields": "LandUse,LotArea,Latitude,Longitude",
                "orderByFields": "OBJECTID",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": PAGE,
                "f": "json",
            }
            for attempt in range(6):
                try:
                    response = session.get(URL, params=params, timeout=90)
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:
                    print("retry", offset, attempt, exc, flush=True)
                    time.sleep(2 + attempt * 2)
            else:
                raise SystemExit(f"failed at offset {offset}")
            features = payload.get("features", [])
            if not features:
                print("done", offset, flush=True)
                break
            for feature in features:
                attrs = feature["attributes"]
                writer.writerow([
                    attrs.get("LandUse") or "",
                    attrs.get("LotArea") or 0,
                    attrs.get("Latitude"),
                    attrs.get("Longitude"),
                ])
            f.flush()
            offset += len(features)
            if offset % 20000 == 0:
                print("rows", offset, flush=True)
            if not payload.get("exceededTransferLimit") and len(features) < PAGE:
                print("finished", offset, flush=True)
                break

if __name__ == "__main__":
    main()
