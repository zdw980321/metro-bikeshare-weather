#!/usr/bin/env python3
"""2025 Santander Cycles replication panel.

Same period cuts, rain rules and fixed-effect sample logic as the New York
panel. Member status is not in the public journey extract. Place type is not
assigned, because there is no lot-area layer comparable to MapPLUTO.
"""
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/london"
USAGE = RAW / "usage"
INTERIM = ROOT / "data/interim/london"
OUT = ROOT / "data/processed"
LOG = ROOT / "output"
for path in (RAW, USAGE, INTERIM, OUT, LOG):
    path.mkdir(parents=True, exist_ok=True)

S3 = "https://s3-eu-west-1.amazonaws.com/cycling.data.tfl.gov.uk/"
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
UA = {"User-Agent": "Mozilla/5.0 (research; bike-share weather replication)"}


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def haversine(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))


def list_2025_files():
    import xml.etree.ElementTree as ET
    marker = "usage-stats/407JourneyDataExtract01Nov2024-14Nov2024.csv"
    found = []
    while True:
        url = S3 + "?prefix=usage-stats/&max-keys=1000&marker=" + urllib.parse.quote(marker)
        root = ET.fromstring(fetch(url))
        items = []
        for node in root.findall("s:Contents", NS):
            key = node.find("s:Key", NS).text
            size = int(node.find("s:Size", NS).text)
            items.append((key, size))
        if not items:
            break
        stop = False
        for key, size in items:
            name = key.split("/", 1)[1]
            if not name.startswith("4") or name.startswith("4a") or name.startswith("4b"):
                stop = True
                break
            if "2025" in name and name.endswith(".csv"):
                found.append((key, size))
        marker = items[-1][0]
        truncated = root.find("s:IsTruncated", NS).text
        if stop or truncated != "true":
            break
    if len(found) < 20:
        raise SystemExit(f"expected the 2025 journey extracts, found {len(found)}")
    return found


def download_one(key, size):
    dest = USAGE / Path(key).name
    if dest.exists() and dest.stat().st_size == size:
        return dest.name, "cached", size
    data = fetch(S3 + urllib.parse.quote(key))
    if len(data) != size:
        raise RuntimeError(f"size mismatch for {key}: {len(data)} != {size}")
    dest.write_bytes(data)
    return dest.name, "downloaded", size


def download_usage():
    files = list_2025_files()
    print(f"2025 files {len(files)} bytes {sum(s for _, s in files)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download_one, key, size) for key, size in files]
        for future in as_completed(futures):
            name, status, size = future.result()
            print(f"{status:12} {size/1e6:6.1f}MB {name}", flush=True)
    return files


def save_bikepoints():
    payload = json.loads(fetch("https://api.tfl.gov.uk/BikePoint", timeout=90))
    (RAW / "bikepoints.json").write_text(json.dumps(payload))
    stations = []
    for item in payload:
        terminal = None
        for prop in item.get("additionalProperties", []):
            if prop.get("key") == "TerminalName":
                terminal = prop.get("value")
        if terminal is None:
            continue
        try:
            station_id = str(int(terminal))
        except ValueError:
            continue
        stations.append({
            "station_id": station_id,
            "station_name": item.get("commonName"),
            "lat": float(item["lat"]),
            "lng": float(item["lon"]),
        })
    print("bikepoints", len(stations), flush=True)
    return stations


def save_tube():
    payload = json.loads(fetch("https://api.tfl.gov.uk/StopPoint/Mode/tube", timeout=120))
    (RAW / "tube_stoppoints.json").write_text(json.dumps(payload))
    stations = []
    for item in payload["stopPoints"]:
        if item.get("stopType") != "NaptanMetroStation":
            continue
        lines = []
        for group in item.get("lineModeGroups") or []:
            if group.get("modeName") == "tube":
                lines.extend(group.get("lineIdentifier") or [])
        if not lines:
            for line in item.get("lines") or []:
                if line.get("id"):
                    lines.append(line["id"])
        if item.get("lat") is None or item.get("lon") is None or not lines:
            continue
        stations.append({
            "stop_id": item.get("stationNaptan") or item.get("naptanId"),
            "stop_name": item.get("commonName"),
            "lat": float(item["lat"]),
            "lng": float(item["lon"]),
            "lines": sorted(set(lines)),
        })
    print("tube stations", len(stations), flush=True)
    return stations


def assign_rail(stations, tube):
    for station in stations:
        nearest = None
        nearest_d = 1e18
        lines = set()
        for stop in tube:
            dlat = abs(stop["lat"] - station["lat"])
            if dlat > 0.02 and nearest_d < 2500:
                continue
            dist = haversine(station["lat"], station["lng"], stop["lat"], stop["lng"])
            if dist < nearest_d:
                nearest_d = dist
                nearest = stop
            if dist <= 400:
                lines.update(stop["lines"])
        station["dist_tube_m"] = nearest_d
        station["nearest_tube"] = nearest["stop_name"] if nearest else None
        station["tube_routes_nearest"] = len(nearest["lines"]) if nearest else 0
        station["tube_routes_400"] = len(lines)
        station["access_300"] = int(nearest_d <= 300)
        station["access_800"] = int(nearest_d <= 800)
        station["is_hub"] = int(len(lines) >= 3)
    return stations


def aggregate():
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    con.execute("PRAGMA memory_limit='10GB'")
    pattern = str(USAGE / "*.csv")
    con.execute(f"""
        CREATE TABLE trips AS
        SELECT "Number" AS rental_id,
               TRY_CAST("Start date" AS TIMESTAMP) AS start_ts,
               TRY_CAST(regexp_replace("Start station number", '[^0-9]', '', 'g') AS INTEGER) AS station_num,
               "Start station" AS station_name,
               upper(trim("Bike model")) AS bike_model,
               TRY_CAST("Total duration (ms)" AS BIGINT) AS duration_ms
        FROM read_csv('{pattern}', header=true, all_varchar=true, union_by_name=true, strict_mode=false)
    """)
    qa = con.execute("""
        SELECT count(*) AS rows,
               count(*) FILTER (WHERE start_ts IS NULL) AS bad_time,
               count(*) FILTER (WHERE station_num IS NULL) AS bad_station,
               count(DISTINCT bike_model) AS models
        FROM trips
    """).fetchone()
    print("raw rows", qa, flush=True)
    models = con.execute("""
        SELECT bike_model, count(*) AS n
        FROM trips
        WHERE start_ts >= TIMESTAMP '2025-01-01' AND start_ts < TIMESTAMP '2026-01-01'
        GROUP BY 1
        ORDER BY n DESC
    """).fetchall()
    print("models", models, flush=True)
    con.execute("""
        CREATE TABLE kept AS
        SELECT rental_id,
               any_value(start_ts) AS start_ts,
               any_value(station_num) AS station_num,
               any_value(station_name) AS station_name,
               any_value(bike_model) AS bike_model,
               any_value(duration_ms) AS duration_ms
        FROM trips
        WHERE start_ts >= TIMESTAMP '2025-01-01' AND start_ts < TIMESTAMP '2026-01-01'
          AND station_num IS NOT NULL
          AND duration_ms BETWEEN 60000 AND 28800000
        GROUP BY rental_id
    """)
    kept_n = con.execute("SELECT count(*) FROM kept").fetchone()[0]
    print("kept unique trips", kept_n, flush=True)
    con.execute(f"""
        COPY (
            SELECT CAST(station_num AS VARCHAR) AS station_id,
                   date_trunc('hour', start_ts) AS hour_ts,
                   any_value(station_name) AS station_name,
                   count(*) AS trips,
                   count(*) FILTER (WHERE bike_model = 'CLASSIC') AS trips_classic,
                   count(*) FILTER (WHERE bike_model = 'PBSC_EBIKE') AS trips_electric
            FROM kept
            GROUP BY 1, 2
        ) TO '{INTERIM / "starts_hour.parquet"}' (FORMAT PARQUET)
    """)
    return con


def weather_grid(stations):
    lats = [s["lat"] for s in stations]
    lons = [s["lng"] for s in stations]
    lat0, lat1 = min(lats), max(lats)
    lon0, lon1 = min(lons), max(lons)
    points = []
    lat = lat0
    while lat <= lat1 + 1e-9:
        lon = lon0
        while lon <= lon1 + 1e-9:
            points.append((round(lat, 4), round(lon, 4)))
            lon += 0.12
        lat += 0.07
    # Keep the grid small. A compact service area does not need every node.
    if len(points) > 8:
        points = points[::2][:8] if len(points) > 12 else points[:8]
    print("weather points", points, flush=True)
    return points


def get_json(url):
    return json.loads(fetch(url, timeout=180))


def fetch_weather(points):
    weather = {}
    aq = []
    for lat, lon in points:
        label = f"{lat:.4f}_{lon:.4f}"
        wurl = (
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            "&start_date=2024-12-31&end_date=2026-01-02"
            "&hourly=temperature_2m,precipitation,snowfall,relative_humidity_2m,wind_speed_10m"
            "&timezone=GMT&wind_speed_unit=kmh&precipitation_unit=mm"
        )
        aurl = (
            "https://air-quality-api.open-meteo.com/v1/air-quality"
            f"?latitude={lat}&longitude={lon}"
            "&start_date=2024-12-31&end_date=2026-01-02"
            "&hourly=pm2_5&timezone=GMT"
        )
        print("weather", label, flush=True)
        weather[label] = get_json(wurl)
        print("aq", label, flush=True)
        aq.append(get_json(aurl))
    (RAW / "weather_utc_2025.json").write_text(json.dumps(weather))
    (RAW / "aq_utc_2025.json").write_text(json.dumps(aq))
    return weather, aq


def nearest(lat, lng, points):
    best_i, best_d = 0, 1e18
    for i, point in enumerate(points):
        dist = haversine(lat, lng, point["lat"], point["lon"])
        if dist < best_d:
            best_i, best_d = i, dist
    return best_i, best_d


def build_panel(con, stations, weather, aq):
    import csv
    w_points = []
    for label, item in weather.items():
        w_points.append({"id": label, "lat": item["latitude"], "lon": item["longitude"], "item": item})
    a_points = []
    for item in aq:
        label = f"{item['latitude']:.4f}_{item['longitude']:.4f}"
        a_points.append({"id": label, "lat": item["latitude"], "lon": item["longitude"], "item": item})
    located = []
    for station in stations:
        wi, wd = nearest(station["lat"], station["lng"], w_points)
        ai, ad = nearest(station["lat"], station["lng"], a_points)
        station["weather_point"] = w_points[wi]["id"]
        station["aq_point"] = a_points[ai]["id"]
        station["dist_weather_m"] = wd
        station["dist_aq_m"] = ad
        located.append(station)
    fields = ["station_id", "station_name", "lat", "lng", "annual_starts",
              "annual_classic", "annual_electric", "dist_tube_m", "nearest_tube",
              "tube_routes_nearest", "tube_routes_400", "access_300", "access_800",
              "is_hub", "weather_point", "aq_point", "dist_weather_m", "dist_aq_m"]
    with (OUT / "london_stations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for station in located:
            writer.writerow({key: station.get(key) for key in fields})
    wx_path = OUT / "london_weather_utc.csv"
    with wx_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["point", "utc_ts", "temp", "precip", "snowfall", "rh", "wind"])
        for point in w_points:
            hourly = point["item"]["hourly"]
            for i, stamp in enumerate(hourly["time"]):
                writer.writerow([
                    point["id"], stamp.replace("T", " ") + ":00",
                    hourly["temperature_2m"][i], hourly["precipitation"][i],
                    hourly["snowfall"][i], hourly["relative_humidity_2m"][i],
                    hourly["wind_speed_10m"][i],
                ])
    aq_path = OUT / "london_aq_utc.csv"
    with aq_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["point", "utc_ts", "pm25"])
        for point in a_points:
            hourly = point["item"]["hourly"]
            for i, stamp in enumerate(hourly["time"]):
                writer.writerow([point["id"], stamp.replace("T", " ") + ":00", hourly["pm2_5"][i]])

    con.execute(f"""
        CREATE OR REPLACE TABLE st AS
        SELECT * FROM read_csv('{OUT / "london_stations.csv"}', header=true)
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE wx AS
        SELECT point,
               timezone('Europe/London', CAST(utc_ts AS TIMESTAMP) AT TIME ZONE 'UTC') AS local_ts,
               temp, precip, snowfall, rh, wind
        FROM read_csv('{wx_path}', header=true)
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE aq AS
        SELECT point,
               timezone('Europe/London', CAST(utc_ts AS TIMESTAMP) AT TIME ZONE 'UTC') AS local_ts,
               pm25
        FROM read_csv('{aq_path}', header=true)
    """)
    con.execute("""
        CREATE OR REPLACE TABLE wx_period AS
        SELECT point,
               CAST(local_ts AS DATE) AS date,
               CASE WHEN hour(local_ts) < 6 THEN 'night'
                    WHEN hour(local_ts) < 10 THEN 'morning'
                    WHEN hour(local_ts) < 16 THEN 'midday'
                    WHEN hour(local_ts) < 20 THEN 'evening'
                    ELSE 'late' END AS period,
               avg(temp) AS temp,
               sum(precip) AS precip,
               sum(snowfall) AS snowfall,
               avg(rh) AS rh,
               avg(wind) AS wind,
               count(*) AS wx_hours
        FROM wx
        WHERE local_ts >= TIMESTAMP '2025-01-01' AND local_ts < TIMESTAMP '2026-01-01'
        GROUP BY 1, 2, 3
    """)
    con.execute("""
        CREATE OR REPLACE TABLE aq_period AS
        SELECT point,
               CAST(local_ts AS DATE) AS date,
               CASE WHEN hour(local_ts) < 6 THEN 'night'
                    WHEN hour(local_ts) < 10 THEN 'morning'
                    WHEN hour(local_ts) < 16 THEN 'midday'
                    WHEN hour(local_ts) < 20 THEN 'evening'
                    ELSE 'late' END AS period,
               avg(pm25) AS pm25,
               count(*) AS aq_hours
        FROM aq
        WHERE local_ts >= TIMESTAMP '2025-01-01' AND local_ts < TIMESTAMP '2026-01-01'
        GROUP BY 1, 2, 3
    """)
    starts = INTERIM / "starts_hour.parquet"
    con.execute(f"""
        CREATE OR REPLACE TABLE period AS
        SELECT station_id,
               CAST(hour_ts AS DATE) AS date,
               CASE WHEN hour(hour_ts) < 6 THEN 'night'
                    WHEN hour(hour_ts) < 10 THEN 'morning'
                    WHEN hour(hour_ts) < 16 THEN 'midday'
                    WHEN hour(hour_ts) < 20 THEN 'evening'
                    ELSE 'late' END AS period,
               sum(trips) AS starts,
               sum(trips_classic) AS starts_classic,
               sum(trips_electric) AS starts_electric
        FROM read_parquet('{starts}')
        GROUP BY 1, 2, 3
    """)
    out_parquet = OUT / "london_panel.parquet"
    con.execute(f"""
        COPY (
          SELECT sk.station_id, sk.date, sk.period, sk.is_hub,
                 sk.access_300, sk.access_800, sk.dist_tube_m,
                 COALESCE(pe.starts, 0) AS starts,
                 COALESCE(pe.starts_classic, 0) AS starts_classic,
                 COALESCE(pe.starts_electric, 0) AS starts_electric,
                 wx.temp, wx.precip, wx.snowfall, wx.wind, wx.rh, aq.pm25,
                 CASE WHEN wx.precip > 0.2 AND wx.snowfall <= 0.1 THEN 1 ELSE 0 END AS rain,
                 CASE WHEN wx.precip >= 1.0 AND wx.snowfall <= 0.1 THEN 1 ELSE 0 END AS rain1,
                 CASE WHEN wx.snowfall > 0.1 THEN 1 ELSE 0 END AS snow,
                 CASE WHEN wx.temp >= 28 THEN 1 ELSE 0 END AS hot,
                 CASE WHEN wx.temp < 0 THEN 1 ELSE 0 END AS freezing,
                 dayofweek(sk.date) AS dow,
                 month(sk.date) AS month,
                 wx.wx_hours
          FROM (
            SELECT s.station_id, d.date, p.period, s.is_hub, s.access_300,
                   s.access_800, s.dist_tube_m, s.weather_point, s.aq_point
            FROM st s
            JOIN (
                SELECT station_id, min(CAST(hour_ts AS DATE)) AS d0, max(CAST(hour_ts AS DATE)) AS d1,
                       sum(trips) AS annual_starts
                FROM read_parquet('{starts}')
                GROUP BY 1
                HAVING sum(trips) >= 200
            ) a USING (station_id)
            CROSS JOIN (SELECT DISTINCT date FROM wx_period) d
            CROSS JOIN (SELECT UNNEST(['night','morning','midday','evening','late']) AS period) p
            WHERE d.date >= a.d0 AND d.date <= a.d1
          ) sk
          JOIN wx_period wx
            ON wx.point = sk.weather_point AND wx.date = sk.date AND wx.period = sk.period
          JOIN aq_period aq
            ON aq.point = sk.aq_point AND aq.date = sk.date AND aq.period = sk.period
          LEFT JOIN period pe
            ON pe.station_id = sk.station_id AND pe.date = sk.date AND pe.period = sk.period
          WHERE wx.wx_hours >= 3 AND wx.temp IS NOT NULL AND aq.pm25 IS NOT NULL
        ) TO '{out_parquet}' (FORMAT PARQUET)
    """)
    summary = con.execute(f"""
        SELECT count(*) AS rows,
               count(DISTINCT station_id) AS stations,
               count(DISTINCT date) AS days,
               sum(starts) AS starts,
               sum(starts_classic) AS classic,
               sum(starts_electric) AS electric,
               avg(rain) AS rain_share,
               avg(rain1) AS rain1_share,
               avg(snow) AS snow_share,
               avg(hot) AS hot_share,
               avg(CASE WHEN access_800 = 1 THEN 1 ELSE 0 END) AS share_rows_800,
               count(DISTINCT CASE WHEN access_800 = 1 THEN station_id END) AS stations_800,
               count(DISTINCT CASE WHEN access_800 = 0 THEN station_id END) AS stations_outside,
               count(DISTINCT CASE WHEN is_hub = 1 THEN station_id END) AS hubs
        FROM read_parquet('{out_parquet}')
    """).fetchone()
    text = "london panel " + " ".join(str(x) for x in summary)
    print(text, flush=True)
    (LOG / "london_panel_summary.txt").write_text(text + "\n")


def annual_counts(con):
    starts = INTERIM / "starts_hour.parquet"
    rows = con.execute(f"""
        SELECT station_id, any_value(station_name) AS station_name,
               sum(trips) AS annual_starts,
               sum(trips_classic) AS annual_classic,
               sum(trips_electric) AS annual_electric
        FROM read_parquet('{starts}')
        GROUP BY 1
    """).fetchall()
    return {row[0]: row for row in rows}


def main():
    download_usage()
    docks = save_bikepoints()
    tube = save_tube()
    by_id = {s["station_id"]: s for s in docks}
    con = aggregate()
    counts = annual_counts(con)
    matched = []
    unmatched_trips = 0
    matched_trips = 0
    for station_id, row in counts.items():
        if station_id not in by_id:
            unmatched_trips += row[2]
            continue
        station = dict(by_id[station_id])
        station["annual_starts"] = int(row[2])
        station["annual_classic"] = int(row[3])
        station["annual_electric"] = int(row[4])
        matched.append(station)
        matched_trips += int(row[2])
    total_trips = matched_trips + unmatched_trips
    share = matched_trips / total_trips if total_trips else 0
    print(f"coordinate match trips {matched_trips}/{total_trips} ({share:.3f}) stations {len(matched)}", flush=True)
    if share < 0.90:
        raise SystemExit("too many 2025 trips lack a current BikePoint coordinate; stop before estimation")
    matched = [s for s in matched if s["annual_starts"] >= 200]
    assign_rail(matched, tube)
    weather, aq = fetch_weather(weather_grid(matched))
    build_panel(con, matched, weather, aq)
    print("london pipeline complete", flush=True)


if __name__ == "__main__":
    main()
