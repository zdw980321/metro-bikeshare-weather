#!/usr/bin/env python3
"""Build the 2025 station-period panel."""
import json
import math
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data/interim"
RAW = ROOT / "data/raw"
OUT = ROOT / "data/processed"
OUT.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "output"
LOG.mkdir(parents=True, exist_ok=True)

def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))

def classify(acc):
    residential = acc.get("01", 0) + acc.get("02", 0) + acc.get("03", 0)
    mixed = acc.get("04", 0)
    commercial = acc.get("05", 0)
    other = sum(acc.get(code, 0) for code in ("06", "07", "08", "09", "10", "11"))
    total = residential + mixed + commercial + other
    if total <= 0:
        return "unclassified", None, None, None
    rs, ms, cs = residential / total, mixed / total, commercial / total
    if cs >= 0.35 and cs >= rs:
        label = "commercial"
    elif rs >= 0.45:
        label = "residential"
    elif ms >= 0.25 or (rs >= 0.20 and cs >= 0.20):
        label = "mixed"
    else:
        label = "other"
    return label, rs, cs, ms

def nearest(lat, lng, points):
    best_i, best_d = 0, 1e18
    for i, p in enumerate(points):
        d = haversine(lat, lng, p["lat"], p["lon"])
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d

def main():
    monthly = sorted((INTERIM / "monthly").glob("starts_2025*.parquet"))
    if len(monthly) < 12:
        raise SystemExit(f"need 12 monthly start files, found {len(monthly)}")
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    con.execute("PRAGMA memory_limit='10GB'")
    starts = INTERIM / "starts_hour.parquet"
    ends = INTERIM / "ends_hour.parquet"
    stations = con.execute(f"""
        SELECT station_id,
               any_value(station_name) AS station_name,
               median(lat) AS lat,
               median(lng) AS lng,
               sum(trips) AS annual_starts,
               sum(trips_member) AS annual_member,
               sum(trips_casual) AS annual_casual,
               sum(trips_classic) AS annual_classic,
               sum(trips_electric) AS annual_electric
        FROM read_parquet('{starts}')
        WHERE lat BETWEEN 40.4 AND 41.0 AND lng BETWEEN -74.3 AND -73.6
          AND hour_ts >= TIMESTAMP '2025-01-01' AND hour_ts < TIMESTAMP '2026-01-01'
        GROUP BY station_id
        HAVING sum(trips) >= 200
    """).fetchall()
    cols = ["station_id", "station_name", "lat", "lng", "annual_starts",
            "annual_member", "annual_casual", "annual_classic", "annual_electric"]
    stations = [dict(zip(cols, row)) for row in stations]
    print("stations", len(stations), flush=True)

    gtfs = INTERIM / "gtfs"
    subway = con.execute(f"""
        WITH stops AS (
            SELECT * FROM read_csv('{gtfs}/stops.txt', header=true, all_varchar=true)
        ),
        parents AS (
            SELECT stop_id, stop_name,
                   TRY_CAST(stop_lat AS DOUBLE) AS lat,
                   TRY_CAST(stop_lon AS DOUBLE) AS lng
            FROM stops
            WHERE TRY_CAST(location_type AS INTEGER) = 1
        ),
        served AS (
            SELECT COALESCE(NULLIF(s.parent_station, ''), s.stop_id) AS parent_id,
                   COUNT(DISTINCT t.route_id) AS n_routes
            FROM read_csv('{gtfs}/stop_times.txt', header=true, all_varchar=true) st
            JOIN stops s ON st.stop_id = s.stop_id
            JOIN read_csv('{gtfs}/trips.txt', header=true, all_varchar=true) t
              ON st.trip_id = t.trip_id
            GROUP BY 1
        )
        SELECT p.stop_id, p.stop_name, p.lat, p.lng, COALESCE(v.n_routes, 1) AS n_routes
        FROM parents p
        LEFT JOIN served v ON p.stop_id = v.parent_id
        WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL
    """).fetchall()
    subway = [{"stop_id": a, "stop_name": b, "lat": c, "lng": d, "n_routes": int(e)} for a, b, c, d, e in subway]
    print("subway parents", len(subway), flush=True)

    for s in stations:
        best, best_i = 1e18, 0
        for i, sub in enumerate(subway):
            if abs(sub["lat"] - s["lat"]) > 0.02:
                continue
            d = haversine(s["lat"], s["lng"], sub["lat"], sub["lng"])
            if d < best:
                best, best_i = d, i
        if best > 1e17:
            for i, sub in enumerate(subway):
                d = haversine(s["lat"], s["lng"], sub["lat"], sub["lng"])
                if d < best:
                    best, best_i = d, i
        sub = subway[best_i]
        s["dist_subway_m"] = best
        s["subway_routes"] = sub["n_routes"]
        s["nearest_subway"] = sub["stop_name"]
        s["access_300"] = int(best <= 300)
        s["access_800"] = int(best <= 800)
        s["is_hub"] = int(best <= 400 and sub["n_routes"] >= 3)

    cells = con.execute(f"""
        SELECT CAST(round(TRY_CAST(lat AS DOUBLE) / 0.0018) AS INTEGER) AS gy,
               CAST(round(TRY_CAST(lon AS DOUBLE) / 0.0022) AS INTEGER) AS gx,
               lpad(regexp_replace(CAST(landuse AS VARCHAR), '\\.0$', ''), 2, '0') AS code,
               sum(TRY_CAST(lotarea AS DOUBLE)) AS area
        FROM read_csv('{RAW}/gis/pluto_lots.csv', header=true, all_varchar=true)
        WHERE TRY_CAST(lat AS DOUBLE) BETWEEN 40.45 AND 40.95
          AND TRY_CAST(lon AS DOUBLE) BETWEEN -74.25 AND -73.65
          AND TRY_CAST(lotarea AS DOUBLE) > 0
          AND landuse IS NOT NULL AND landuse <> ''
        GROUP BY 1, 2, 3
    """).fetchall()
    cell = {}
    for gy, gx, code, area in cells:
        cell.setdefault((gy, gx), {})[code] = float(area)
    print("land-use cells", len(cell), flush=True)
    for s in stations:
        acc = {}
        cy, cx = round(s["lat"] / 0.0018), round(s["lng"] / 0.0022)
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                clat = (cy + dy) * 0.0018
                clon = (cx + dx) * 0.0022
                if haversine(s["lat"], s["lng"], clat, clon) > 320:
                    continue
                for code, area in cell.get((cy + dy, cx + dx), {}).items():
                    acc[code] = acc.get(code, 0.0) + area
        label, rs, cs, ms = classify(acc)
        s["place_type"] = label
        s["res_share"] = rs
        s["com_share"] = cs
        s["mix_share"] = ms

    weather = json.loads((RAW / "weather/nyc_weather_utc_2025.json").read_text())
    aq = json.loads((RAW / "weather/nyc_aq_utc_2025.json").read_text())
    w_points = []
    for label, item in weather.items():
        w_points.append({"id": label, "lat": item["latitude"], "lon": item["longitude"], "item": item})
    a_points = []
    for item in aq:
        label = f"{item['latitude']:.4f}_{item['longitude']:.4f}"
        a_points.append({"id": label, "lat": item["latitude"], "lon": item["longitude"], "item": item})
    for s in stations:
        wi, wd = nearest(s["lat"], s["lng"], w_points)
        ai, ad = nearest(s["lat"], s["lng"], a_points)
        s["weather_point"] = w_points[wi]["id"]
        s["aq_point"] = a_points[ai]["id"]
        s["dist_weather_m"] = wd
        s["dist_aq_m"] = ad

    import csv
    station_path = OUT / "stations.csv"
    fields = ["station_id", "station_name", "lat", "lng", "annual_starts", "annual_member",
              "annual_casual", "annual_classic", "annual_electric", "dist_subway_m",
              "subway_routes", "nearest_subway", "access_300", "access_800", "is_hub",
              "place_type", "res_share", "com_share", "mix_share", "weather_point",
              "aq_point", "dist_weather_m", "dist_aq_m"]
    with station_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for s in stations:
            writer.writerow({k: s.get(k) for k in fields})

    wx_path = OUT / "weather_utc.csv"
    with wx_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["point", "utc_ts", "temp", "precip", "snowfall", "rh", "wind"])
        for p in w_points:
            h = p["item"]["hourly"]
            for i, t in enumerate(h["time"]):
                writer.writerow([p["id"], t.replace("T", " ") + ":00", h["temperature_2m"][i],
                                 h["precipitation"][i], h["snowfall"][i], h["relative_humidity_2m"][i],
                                 h["wind_speed_10m"][i]])
    aq_path = OUT / "aq_utc.csv"
    with aq_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["point", "utc_ts", "pm25"])
        for p in a_points:
            h = p["item"]["hourly"]
            for i, t in enumerate(h["time"]):
                writer.writerow([p["id"], t.replace("T", " ") + ":00", h["pm2_5"][i]])

    con.execute(f"""
        CREATE TABLE st AS
        SELECT station_id, place_type, is_hub, access_300, access_800, weather_point, aq_point,
               lat, lng, dist_subway_m
        FROM read_csv('{station_path}', header=true,
             types={{'station_id':'VARCHAR','is_hub':'INTEGER','access_300':'INTEGER','access_800':'INTEGER'}})
    """)
    con.execute(f"""
        CREATE TABLE wx AS
        SELECT point,
               timezone('America/New_York', CAST(utc_ts AS TIMESTAMP) AT TIME ZONE 'UTC') AS local_ts,
               temp, precip, snowfall, rh, wind
        FROM read_csv('{wx_path}', header=true)
    """)
    con.execute(f"""
        CREATE TABLE aq AS
        SELECT point,
               timezone('America/New_York', CAST(utc_ts AS TIMESTAMP) AT TIME ZONE 'UTC') AS local_ts,
               pm25
        FROM read_csv('{aq_path}', header=true)
    """)
    con.execute("""
        CREATE TABLE wx_period AS
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
        CREATE TABLE aq_period AS
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
    con.execute(f"""
        CREATE TABLE starts AS
        SELECT station_id, hour_ts,
               sum(trips) AS starts,
               sum(trips_member) AS starts_member,
               sum(trips_casual) AS starts_casual,
               sum(trips_classic) AS starts_classic,
               sum(trips_electric) AS starts_electric
        FROM read_parquet('{starts}')
        WHERE hour_ts >= TIMESTAMP '2025-01-01' AND hour_ts < TIMESTAMP '2026-01-01'
        GROUP BY 1, 2
    """)
    con.execute(f"""
        CREATE TABLE ends AS
        SELECT station_id, hour_ts, sum(trips) AS ends
        FROM read_parquet('{ends}')
        WHERE hour_ts >= TIMESTAMP '2025-01-01' AND hour_ts < TIMESTAMP '2026-01-01'
        GROUP BY 1, 2
    """)
    con.execute("""
        CREATE TABLE hourly AS
        SELECT COALESCE(s.station_id, e.station_id) AS station_id,
               COALESCE(s.hour_ts, e.hour_ts) AS hour_ts,
               COALESCE(s.starts, 0) AS starts,
               COALESCE(s.starts_member, 0) AS starts_member,
               COALESCE(s.starts_casual, 0) AS starts_casual,
               COALESCE(s.starts_classic, 0) AS starts_classic,
               COALESCE(s.starts_electric, 0) AS starts_electric,
               COALESCE(e.ends, 0) AS ends
        FROM starts s
        FULL OUTER JOIN ends e USING (station_id, hour_ts)
    """)
    con.execute("""
        CREATE TABLE period AS
        SELECT station_id,
               CAST(hour_ts AS DATE) AS date,
               CASE WHEN hour(hour_ts) < 6 THEN 'night'
                    WHEN hour(hour_ts) < 10 THEN 'morning'
                    WHEN hour(hour_ts) < 16 THEN 'midday'
                    WHEN hour(hour_ts) < 20 THEN 'evening'
                    ELSE 'late' END AS period,
               sum(starts) AS starts,
               sum(starts_member) AS starts_member,
               sum(starts_casual) AS starts_casual,
               sum(starts_classic) AS starts_classic,
               sum(starts_electric) AS starts_electric,
               sum(ends) AS ends
        FROM hourly
        GROUP BY 1, 2, 3
    """)
    con.execute("""
        CREATE TABLE active AS
        SELECT station_id,
               min(CAST(hour_ts AS DATE)) AS d0,
               max(CAST(hour_ts AS DATE)) AS d1
        FROM hourly
        WHERE starts > 0 OR ends > 0
        GROUP BY 1
    """)
    out_parquet = OUT / "panel_period.parquet"
    con.execute(f"""
        COPY (
          SELECT sk.station_id, sk.date, sk.period, sk.place_type, sk.is_hub,
                 sk.access_300, sk.access_800, sk.dist_subway_m,
                 COALESCE(pe.starts, 0) AS starts,
                 COALESCE(pe.starts_member, 0) AS starts_member,
                 COALESCE(pe.starts_casual, 0) AS starts_casual,
                 COALESCE(pe.starts_classic, 0) AS starts_classic,
                 COALESCE(pe.starts_electric, 0) AS starts_electric,
                 COALESCE(pe.ends, 0) AS ends,
                 wx.temp, wx.precip, wx.snowfall, wx.wind, wx.rh, aq.pm25,
                 CASE WHEN wx.precip > 0.2 AND wx.snowfall <= 0.1 THEN 1 ELSE 0 END AS rain,
                 CASE WHEN wx.precip >= 1.0 AND wx.snowfall <= 0.1 THEN 1 ELSE 0 END AS rain1,
                 CASE WHEN wx.snowfall > 0.1 THEN 1 ELSE 0 END AS snow,
                 CASE WHEN wx.temp >= 28 THEN 1 ELSE 0 END AS hot,
                 CASE WHEN wx.temp >= 30 THEN 1 ELSE 0 END AS hot30,
                 CASE WHEN wx.temp < 0 THEN 1 ELSE 0 END AS freezing,
                 CASE WHEN aq.pm25 >= 25 THEN 1 ELSE 0 END AS high_pm,
                 dayofweek(sk.date) AS dow,
                 month(sk.date) AS month,
                 wx.wx_hours
          FROM (
            SELECT s.station_id, d.date, p.period, s.place_type, s.is_hub, s.access_300,
                   s.access_800, s.dist_subway_m, s.weather_point, s.aq_point
            FROM st s
            JOIN active a USING (station_id)
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
               avg(temp) AS temp,
               avg(rain) AS rain_share,
               avg(snow) AS snow_share,
               avg(hot) AS hot_share,
               avg(CASE WHEN access_800=1 THEN 1 ELSE 0 END) AS access_share
        FROM read_parquet('{out_parquet}')
    """).fetchone()
    places = con.execute(f"""
        SELECT place_type, access_800, count(DISTINCT station_id) AS stations, sum(starts) AS starts
        FROM read_parquet('{out_parquet}')
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).fetchall()
    dist = con.execute(f"""
        SELECT median(dist_weather_m), median(dist_aq_m), median(dist_subway_m)
        FROM read_csv('{station_path}')
        WHERE access_800=1
    """).fetchone()
    print("panel", summary, flush=True)
    print("places", places, flush=True)
    print("median distances weather/aq/subway among access stations", dist, flush=True)
    (LOG / "panel_summary.txt").write_text(
        "panel " + repr(summary) + "\nplaces " + repr(places) + "\ndistances " + repr(dist) + "\n"
    )

if __name__ == "__main__":
    main()
