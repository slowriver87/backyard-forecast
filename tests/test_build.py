"""Offline test: feeds the builder fake API responses shaped like the real ones
(NWS forecastHourly / forecast, Open-Meteo forecast / archive) and checks the output.
Run: python3 tests/test_build.py
"""
import json
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

tmp = Path(tempfile.mkdtemp())
os.environ["OUT_PATH"] = str(tmp / "data.json")
os.environ["NORMALS_PATH"] = str(tmp / "normals.json")
os.environ["LAT"] = "35.1234"
os.environ["LON"] = "-80.5678"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_forecast as bf  # noqa: E402

TZ = ZoneInfo("America/New_York")
random.seed(4)
NOW = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
calls = []


def temp_at(t):
    base = 84 - 2.0 * ((t - NOW).days)        # cooling trend
    return base - 9 + 9 * math.sin((t.hour - 9) / 24 * 2 * math.pi)


def fake_get_json(url, tries=4):
    calls.append(url)
    if "api.weather.gov/points" in url:
        return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/XXX/1,1/forecast/hourly",
                               "forecast": "https://api.weather.gov/gridpoints/XXX/1,1/forecast",
                               "cwa": "XXX"}}
    if url.endswith("/forecast/hourly"):
        periods = []
        for i in range(156):  # NWS hourly runs ~6.5 days
            t = NOW + timedelta(hours=i)
            periods.append({
                "startTime": t.isoformat(), "isDaytime": 6 <= t.hour < 18,
                "temperature": round(temp_at(t)), "temperatureUnit": "F",
                "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 70 if (t - NOW).days == 2 and t.hour > 13 else random.choice([0, 5, 10, 20])},
                "dewpoint": {"unitCode": "wmoUnit:degC", "value": round(bf.f_to_c(70 - 1.5 * (t - NOW).days), 2)},
                "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 70},
                "shortForecast": "Mostly Sunny",
            })
        return {"properties": {"periods": periods}}
    if url.endswith("/forecast"):
        out = []
        for k in range(14):
            d = NOW + timedelta(hours=12 * k)
            out.append({"startTime": d.isoformat(), "isDaytime": k % 2 == 0, "name": "x",
                        "shortForecast": "Chance Showers And Thunderstorms" if k == 4 else "Sunny"})
        return {"properties": {"periods": out}}
    if url.startswith("https://api.open-meteo.com"):
        start = NOW.replace(hour=0)
        times, T, D, RH, P, C = [], [], [], [], [], []
        for i in range(24 * 12):
            t = start + timedelta(hours=i)
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            T.append(round(temp_at(t) + 1, 1)); D.append(round(66 - 1.0 * (t - NOW).days, 1))
            RH.append(65); P.append(40 if (t - NOW).days == 8 else 10); C.append(2)
        return {"hourly": {"time": times, "temperature_2m": T, "dew_point_2m": D, "relative_humidity_2m": RH,
                           "precipitation_probability": P, "weather_code": C}}
    if "archive-api" in url:
        times, T, D = [], [], []
        t = datetime(NOW.year - 20, 1, 1)
        end = datetime(NOW.year - 1, 12, 31, 23)
        while t <= end:
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            doy = t.timetuple().tm_yday
            seasonal = math.sin((doy - 100) / 365 * 2 * math.pi)
            T.append(round(75 + 12 * seasonal + 6 * math.sin((t.hour - 9) / 24 * 2 * math.pi), 1))
            D.append(round(62 + 10 * seasonal, 1))
            t += timedelta(hours=1)
        return {"hourly": {"time": times, "temperature_2m": T, "dew_point_2m": D}}
    raise AssertionError("unexpected url")


bf.get_json = fake_get_json
assert bf.main() == 0
data = json.loads(Path(os.environ["OUT_PATH"]).read_text())
days = data["days"]
print(f"{len(days)} days")
for d in days:
    print(f"{d['date']} {d['label']:<10} score {d['score']:>3} {d['grade']:<6} rain {d['rain']['max_pop']:>3}% "
          f"feels {d['temp']['feels_high']}F dew {d['dew']['mean']} (normal {d['dew']['normal']}, {d['dew']['anomaly']:+}) "
          f"{d['source'][:20]:<20} {d['confidence']:<6} pick={d['top_pick']} nice={d['nice_hours']}/{d['total_hours']} stretch={d['best_window']['label'] if d['best_window'] else None}")
    print("    ", d["summary"], "|", d["sky"], "| sunset", d["sunset"])
    a = d["afternoon"]
    if a:
        print("     PM:", a["start"], a["flag"], "peak", a["peak_feels"], "vs summer", a["vs_summer"], a["rain_note"])

assert len(days) == 10
assert days[-1]["source"].startswith("Open-Meteo")
assert days[0]["source"] == "National Weather Service"
raw = json.dumps(data)
for secret in ("35.1", "-80.5", "80.56", "XXX", "gridpoints", '"lat', '"lon', "latitude", "longitude"):
    assert secret not in raw, f"leak: {secret}"

assert data["summer_ref"] and data["summer_ref"]["month"] == "July"
assert all(d["sunset"].endswith("PM") for d in days)
assert int(days[1]["sunset"].split(":")[1][:2]) % 15 == 0

# Heat index sanity vs NWS published table: 90F at 60% RH -> about 100F
hi = bf.heat_index_f(90, 60)
assert 98 <= hi <= 102, hi
assert bf.heat_index_f(70, 90) == 70

# Second run must use the normals cache (no archive call)
calls.clear()
assert bf.main() == 0
assert not any("archive-api" in c for c in calls)
print("all checks passed; sample at", os.environ["OUT_PATH"])
