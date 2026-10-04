#!/usr/bin/env python3
"""Aggregate 2025 Citi Bike monthly zips to station-hour starts and ends."""
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "citibike"
OUT = ROOT / "data" / "interim"
MONTHLY = OUT / "monthly"
SCRATCH = OUT / "scratch"
MONTHLY.mkdir(parents=True, exist_ok=True)

def zip_complete(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1_000_000:
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except Exception:
        return False

def process(month_zip: Path) -> None:
    tag = month_zip.name[:6]
    starts_out = MONTHLY / f"starts_{tag}.parquet"
    ends_out = MONTHLY / f"ends_{tag}.parquet"
    qa_out = MONTHLY / f"qa_{tag}.json"
    if starts_out.exists() and ends_out.exists() and qa_out.exists():
        print("skip", month_zip.name, flush=True)
        return
    print("unzip", month_zip.name, flush=True)
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    subprocess.check_call(["unzip", "-q", str(month_zip), "-d", str(SCRATCH)])
    csvs = list(SCRATCH.rglob("*.csv"))
    if not csvs:
        raise SystemExit(f"no csv in {month_zip}")
    for csv_path in csvs:
        if csv_path.parent != SCRATCH:
            target = SCRATCH / csv_path.name
            if target.exists():
                target = SCRATCH / f"{csv_path.parent.name}_{csv_path.name}"
            csv_path.rename(target)
    glob = str(SCRATCH / "*.csv").replace("'", "''")
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    con.execute("PRAGMA memory_limit='8GB'")
    con.execute(f"""
        CREATE TABLE trips AS
        SELECT
          CAST(start_station_id AS VARCHAR) AS start_station_id,
          CAST(end_station_id AS VARCHAR) AS end_station_id,
          lower(CAST(rideable_type AS VARCHAR)) AS rideable_type,
          lower(CAST(member_casual AS VARCHAR)) AS member_casual,
          started_at,
          ended_at,
          start_lat, start_lng, end_lat, end_lng,
          start_station_name, end_station_name
        FROM read_csv('{glob}', union_by_name=true, header=true,
          types={{'start_station_id':'VARCHAR','end_station_id':'VARCHAR',
                  'rideable_type':'VARCHAR','member_casual':'VARCHAR'}})
    """)
    qa_types = con.execute("""
        SELECT rideable_type, member_casual, count(*) AS n
        FROM trips GROUP BY 1, 2 ORDER BY n DESC
    """).fetchall()
    con.execute("""
        CREATE TABLE kept AS
        SELECT * FROM trips
        WHERE start_station_id IS NOT NULL AND start_station_id <> ''
          AND end_station_id IS NOT NULL AND end_station_id <> ''
          AND start_lat BETWEEN 40.4 AND 41.0 AND start_lng BETWEEN -74.3 AND -73.6
          AND end_lat BETWEEN 40.4 AND 41.0 AND end_lng BETWEEN -74.3 AND -73.6
          AND started_at IS NOT NULL AND ended_at IS NOT NULL
          AND date_diff('minute', started_at, ended_at) BETWEEN 1 AND 480
          AND started_at >= TIMESTAMP '2025-01-01' AND started_at < TIMESTAMP '2026-01-01'
    """)
    raw_n = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    kept_n = con.execute("SELECT count(*) FROM kept").fetchone()[0]
    con.execute(f"""
        COPY (
          SELECT start_station_id AS station_id,
                 any_value(start_station_name) AS station_name,
                 median(start_lat) AS lat,
                 median(start_lng) AS lng,
                 date_trunc('hour', started_at) AS hour_ts,
                 count(*) AS trips,
                 count(*) FILTER (WHERE member_casual='member') AS trips_member,
                 count(*) FILTER (WHERE member_casual='casual') AS trips_casual,
                 count(*) FILTER (WHERE rideable_type='classic_bike') AS trips_classic,
                 count(*) FILTER (WHERE rideable_type='electric_bike') AS trips_electric
          FROM kept
          GROUP BY start_station_id, date_trunc('hour', started_at)
        ) TO '{starts_out}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (
          SELECT end_station_id AS station_id,
                 any_value(end_station_name) AS station_name,
                 median(end_lat) AS lat,
                 median(end_lng) AS lng,
                 date_trunc('hour', ended_at) AS hour_ts,
                 count(*) AS trips,
                 count(*) FILTER (WHERE member_casual='member') AS trips_member,
                 count(*) FILTER (WHERE member_casual='casual') AS trips_casual,
                 count(*) FILTER (WHERE rideable_type='classic_bike') AS trips_classic,
                 count(*) FILTER (WHERE rideable_type='electric_bike') AS trips_electric
          FROM kept
          WHERE ended_at >= TIMESTAMP '2025-01-01' AND ended_at < TIMESTAMP '2026-01-01'
          GROUP BY end_station_id, date_trunc('hour', ended_at)
        ) TO '{ends_out}' (FORMAT PARQUET)
    """)
    starts_sum = con.execute(f"SELECT coalesce(sum(trips),0) FROM read_parquet('{starts_out}')").fetchone()[0]
    qa = {
        "file": month_zip.name,
        "raw_rows": int(raw_n),
        "kept_rows": int(kept_n),
        "starts": int(starts_sum),
        "types": [{"rideable_type": a, "member_casual": b, "n": int(n)} for a, b, n in qa_types],
    }
    qa_out.write_text(json.dumps(qa, ensure_ascii=False, indent=2))
    print(month_zip.name, "kept", kept_n, "starts", starts_sum, flush=True)
    con.close()
    shutil.rmtree(SCRATCH)

def combine() -> None:
    con = duckdb.connect()
    for kind in ("starts", "ends"):
        files = sorted(MONTHLY.glob(f"{kind}_2025*.parquet"))
        if not files:
            continue
        dest = OUT / f"{kind}_hour.parquet"
        file_list = ", ".join("'" + str(p).replace("'", "''") + "'" for p in files)
        tmp = dest.with_suffix(".parquet.tmp")
        con.execute(f"COPY (SELECT * FROM read_parquet([{file_list}])) TO '{tmp}' (FORMAT PARQUET)")
        tmp.replace(dest)
        n = con.execute(f"SELECT count(*), sum(trips) FROM read_parquet('{dest}')").fetchone()
        print("combined", kind, "files", len(files), n, flush=True)
    con.close()

def main() -> None:
    for month in range(1, 13):
        path = RAW / f"2025{month:02d}-citibike-tripdata.zip"
        if zip_complete(path):
            process(path)
        else:
            print("not ready", path.name, path.stat().st_size if path.exists() else 0, flush=True)
    combine()

if __name__ == "__main__":
    main()
