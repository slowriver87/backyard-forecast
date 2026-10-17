#!/usr/bin/env python3
"""
Builds site/data.json: a 10-day "backyard score" forecast for a dog who loves
spending the day outside when it is cool and dry, and dislikes rain and above-normal humidity.

Where the data comes from
  Days 1-7  : National Weather Service (api.weather.gov), the official US forecast.
              NWS only publishes 7 days, so days 8-10 come from the next source.
  Days 8-10 : Open-Meteo (open-meteo.com), which serves the NOAA and European
              (ECMWF) computer weather models.
  "Normal"  : Open-Meteo historical archive (ERA5 reanalysis), the past 20 years
              for the same calendar dates.

Privacy
  The location is read from the LAT and LON environment variables (GitHub
  secrets). It is only sent to the weather services. It is never written to
  data.json or printed in logs.

Standard library only, so the GitHub Action needs no installs.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
TZ_NAME = os.environ.get("TZ_NAME", "America/New_York")
TZ = ZoneInfo(TZ_NAME)
DOG_NAME = os.environ.get("DOG_NAME", "Maximus")
DAYS = 10
DAY_START = 7                         # first hour that counts (7 AM)
SUNSET_BUFFER_MIN = 30                # day ends this many minutes before sunset (he comes in before dark)
AFTERNOON_START = 13                  # "afternoon" runs from 1 PM to the end of the day
NORMALS_END_HOUR = 19                 # fixed daylight window (7 AM to 7 PM) used for humidity normals
SUMMER_MONTH = 7                      # comparison month for the afternoon check (July)
# Afternoon check labels, by hottest afternoon feels-like (rule of thumb, not a vet standard)
AFTERNOON_EASY_F, AFTERNOON_WATCH_F = 80.0, 90.0
STRETCH_HOURS = 4                     # length of the "best stretch" shown each day
# A "nice hour": feels like at or below this, and rain chance below this
NICE_FEELS_F, NICE_POP = 80.0, 30.0
NORMAL_YEARS = 20                     # years of history used for "normal"
NORMAL_WINDOW = 7                     # +/- days around each date when averaging
NORMALS_MAX_AGE_DAYS = 45             # rebuild the cached normals after this

# Score weights (must add to 1.0)
W_RAIN, W_TEMP, W_HUMID = 0.40, 0.35, 0.25
# Feels-like temperature range for the temperature score
TEMP_BEST_F, TEMP_WORST_F = 65.0, 95.0
# Dew point difference from normal that maps to a 0 or 100 humidity score
DEW_SWING_F = 8.0

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = Path(os.environ.get("OUT_PATH", ROOT / "site" / "data.json"))
NORMALS_PATH = Path(os.environ.get("NORMALS_PATH", ROOT / ".cache" / "normals.json"))

UA = "backyard-forecast/1.0 (github.com/{})".format(
    os.environ.get("GITHUB_REPOSITORY", "personal-project"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- http
def get_json(url: str, tries: int = 4) -> dict:
    """GET a JSON document with retries. Location is in the URL, so never log it."""
    host = urllib.parse.urlparse(url).netloc
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/geo+json, application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            code = getattr(e, "code", "")
            log(f"  {host}: attempt {attempt + 1} failed {code}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{host} unavailable ({type(last).__name__})")


# ---------------------------------------------------------------- weather math
def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def rh_from_dew(t_f: float, dew_f: float) -> float:
    """Relative humidity (%) from temperature and dew point (Magnus formula)."""
    t, d = f_to_c(t_f), f_to_c(dew_f)
    a, b = 17.625, 243.04
    rh = 100 * math.exp(a * d / (b + d)) / math.exp(a * t / (b + t))
    return max(1.0, min(100.0, rh))


def heat_index_f(t: float, rh: float) -> float:
    """NWS heat index ("feels like") using the Rothfusz method the NWS publishes."""
    simple = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if (simple + t) / 2 < 80:
        return t  # heat index only matters in warm weather; below that, feels like = air temp
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
          - 0.00683783 * t * t - 0.05481717 * rh * rh + 0.00122874 * t * t * rh
          + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh)
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return hi


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def sun_times(d: date, lat: float, lon: float):
    """Sunrise and sunset for a date, as local times.

    Uses the NOAA "General Solar Position Calculations" equations (accurate to
    about a minute, which is plenty here). Longitude is negative west.
    """
    n = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))
              - math.tan(la) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight_utc = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("UTC"))
    rise = midnight_utc + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sset = midnight_utc + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise.astimezone(TZ), sset.astimezone(TZ)


def fmt_clock_rounded(t: datetime, step: int = 15) -> str:
    """Clock time rounded to the nearest `step` minutes, e.g. "6:45 PM".
    Rounding keeps an exact sunset time from hinting at the exact location."""
    mins = t.hour * 60 + t.minute + t.second / 60
    mins = int(round(mins / step) * step) % (24 * 60)
    h, m = divmod(mins, 60)
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


# WMO weather codes used by Open-Meteo, as short plain-English text
WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
    81: "Rain showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Thunderstorms, hail", 99: "Thunderstorms, hail",
}
WMO_RANK = {c: i for i, c in enumerate(sorted(WMO))}  # higher code = worse weather, roughly


# ---------------------------------------------------------------- sources
def fetch_nws(lat: float, lon: float):
    """Hourly forecast and daily text from the National Weather Service."""
    pts = get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")["properties"]
    hourly_doc = get_json(pts["forecastHourly"])
    hours = {}
    for p in hourly_doc["properties"]["periods"]:
        start = datetime.fromisoformat(p["startTime"]).astimezone(TZ)
        t = p["temperature"]
        t_f = float(t["value"] if isinstance(t, dict) else t)
        unit = (t.get("unitCode", "") if isinstance(t, dict) else p.get("temperatureUnit", "F"))
        if unit.endswith("degC") or unit == "C":
            t_f = c_to_f(t_f)
        dew = p.get("dewpoint") or {}
        dew_f = None
        if dew.get("value") is not None:
            dew_f = float(dew["value"])
            if str(dew.get("unitCode", "degC")).endswith("degC"):
                dew_f = c_to_f(dew_f)
        rh = (p.get("relativeHumidity") or {}).get("value")
        if dew_f is None and rh is None:
            continue  # no humidity info we can use
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        key = start.replace(minute=0, second=0, microsecond=0)
        hours[key] = {
            "t": t_f, "dew": dew_f, "pop": float(pop),
            "rh": float(rh) if rh is not None else rh_from_dew(t_f, dew_f),
            "text": p.get("shortForecast", ""), "src": "nws",
        }

    text = {}
    try:
        daily_doc = get_json(pts["forecast"])
        for p in daily_doc["properties"]["periods"]:
            d = datetime.fromisoformat(p["startTime"]).astimezone(TZ).date()
            entry = text.setdefault(d, {})
            entry["day" if p.get("isDaytime") else "night"] = p.get("shortForecast", "")
    except Exception as e:  # daily text is nice to have, not essential
        log(f"  NWS daily text skipped: {e}")
    return hours, text


def fetch_open_meteo(lat: float, lon: float):
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "hourly": "temperature_2m,dew_point_2m,relative_humidity_2m,"
                  "precipitation_probability,weather_code",
        "temperature_unit": "fahrenheit", "timezone": TZ_NAME, "forecast_days": 12,
    })
    doc = get_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    h = doc["hourly"]
    hours = {}
    for i, ts in enumerate(h["time"]):
        t, dew = h["temperature_2m"][i], h["dew_point_2m"][i]
        if t is None or dew is None:
            continue
        key = datetime.fromisoformat(ts).replace(tzinfo=TZ)
        rh = h["relative_humidity_2m"][i]
        code = h["weather_code"][i]
        hours[key] = {
            "t": float(t), "dew": float(dew),
            "pop": float(h["precipitation_probability"][i] or 0),
            "rh": float(rh) if rh is not None else rh_from_dew(t, dew),
            "code": code, "text": WMO.get(code, ""), "src": "open-meteo",
        }
    return hours


def load_normals(lat: float, lon: float, today: date):
    """Normal dew point and high temperature for each calendar date (MM-DD).

    Uses the past NORMAL_YEARS full years from the ERA5 reanalysis archive and
    averages a +/- NORMAL_WINDOW day window around each date. Cached because
    normals barely move and the download is large.
    """
    if NORMALS_PATH.exists():
        try:
            cached = json.loads(NORMALS_PATH.read_text())
            built = date.fromisoformat(cached["built"])
            if ((today - built).days < NORMALS_MAX_AGE_DAYS and cached.get("by_date")
                    and cached.get("version") == 2):
                log("  normals: using cache")
                return cached
        except Exception:
            pass

    end_year = today.year - 1
    start_year = end_year - NORMAL_YEARS + 1
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "start_date": f"{start_year}-01-01", "end_date": f"{end_year}-12-31",
        "hourly": "dew_point_2m,temperature_2m",
        "temperature_unit": "fahrenheit", "timezone": TZ_NAME,
    })
    log("  normals: downloading archive")
    doc = get_json(f"https://archive-api.open-meteo.com/v1/archive?{q}")
    h = doc["hourly"]

    # Per calendar day of each year: mean dew point during daylight hours, and daily high.
    per_day: dict[date, dict] = {}
    for i, ts in enumerate(h["time"]):
        dt = datetime.fromisoformat(ts)
        d = dt.date()
        rec = per_day.setdefault(d, {"dew": [], "t": []})
        t, dew = h["temperature_2m"][i], h["dew_point_2m"][i]
        if t is not None:
            rec["t"].append(t)
        if dew is not None and DAY_START <= dt.hour < NORMALS_END_HOUR:
            rec["dew"].append(dew)
        # Typical summer afternoon: hottest feels-like from 1 PM to 7 PM, July days only.
        if (dt.month == SUMMER_MONTH and AFTERNOON_START <= dt.hour < NORMALS_END_HOUR
                and t is not None and dew is not None):
            fl = heat_index_f(t, rh_from_dew(t, dew))
            rec["sfl"] = max(rec.get("sfl", -999), fl)
            rec.setdefault("sdew", []).append(dew)

    daily = {}
    for d, rec in per_day.items():
        if rec["dew"] and rec["t"]:
            daily[d] = (sum(rec["dew"]) / len(rec["dew"]), max(rec["t"]))

    # Average by day-of-year with a moving window (2001 is a non-leap reference year).
    by_date = {}
    ref = date(2001, 1, 1)
    for k in range(365):
        center = ref + timedelta(days=k)
        dews, highs = [], []
        for y in range(start_year, end_year + 1):
            for off in range(-NORMAL_WINDOW, NORMAL_WINDOW + 1):
                try:
                    dd = date(y, center.month, center.day) + timedelta(days=off)
                except ValueError:
                    continue
                if dd in daily:
                    dews.append(daily[dd][0])
                    highs.append(daily[dd][1])
        if dews:
            by_date[center.strftime("%m-%d")] = {
                "dew": round(sum(dews) / len(dews), 1),
                "high": round(sum(highs) / len(highs), 1),
            }
    summer_feels = [r["sfl"] for r in per_day.values() if "sfl" in r]
    summer_dews = [x for r in per_day.values() for x in r.get("sdew", [])]
    summer_ref = None
    if summer_feels and summer_dews:
        summer_ref = {"month": date(2001, SUMMER_MONTH, 1).strftime("%B"),
                      "feels": round(sum(summer_feels) / len(summer_feels)),
                      "dew": round(sum(summer_dews) / len(summer_dews))}

    if len(by_date) < 300:
        raise RuntimeError("archive returned too little history to build normals")
    by_date["02-29"] = by_date.get("02-28")

    result = {"built": today.isoformat(), "years": f"{start_year}-{end_year}",
              "by_date": by_date, "summer_ref": summer_ref, "version": 2}
    NORMALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    NORMALS_PATH.write_text(json.dumps(result))
    return result


# ---------------------------------------------------------------- scoring
def temp_score(feels_f: float) -> float:
    return clamp(100 * (TEMP_WORST_F - feels_f) / (TEMP_WORST_F - TEMP_BEST_F))


def humid_score(dew_anomaly: float) -> float:
    return clamp(50 - dew_anomaly * (50 / DEW_SWING_F))


def humid_score_absolute(dew_f: float) -> float:
    """Fallback if normals are unavailable: 55F dew point or lower = 100, 75F or higher = 0."""
    return clamp(100 * (75 - dew_f) / 20)


def grade(score: float) -> str:
    if score >= 75:
        return "Great"
    if score >= 60:
        return "Good"
    if score >= 45:
        return "Fair"
    return "Rough"


def fmt_hour(h: int) -> str:
    suffix = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh} {suffix}"


def is_nice(x: dict) -> bool:
    return x["feels"] <= NICE_FEELS_F and x["pop"] < NICE_POP


def best_window(hours: list[dict], n: int = STRETCH_HOURS) -> dict | None:
    """Best n-hour stretch outside: low rain chance and cool feels-like temp."""
    if len(hours) < n:
        n = len(hours)
    if n < 1:
        return None
    best = None
    for i in range(len(hours) - n + 1):
        run = hours[i:i + n]
        if run[-1]["hour"] - run[0]["hour"] != n - 1:
            continue
        s = sum(0.5 * (100 - x["pop"]) + 0.5 * temp_score(x["feels"]) for x in run) / n
        if best is None or s > best["s"] + 0.01:
            best = {"s": s, "start": run[0]["hour"], "end": run[-1]["hour"] + 1,
                    "feels": round(max(x["feels"] for x in run)),
                    "pop": round(max(x["pop"] for x in run))}
    if not best:
        return None
    best["label"] = f"{fmt_hour(best['start'])} to {fmt_hour(best['end'] % 24)}"
    best.pop("s")
    return best


def afternoon_check(hours: list[dict], summer_ref: dict | None) -> dict | None:
    """How the afternoon (1 PM to the end of the day) compares with a typical July afternoon."""
    pm = [x for x in hours if x["hour"] >= AFTERNOON_START]
    if not pm:
        return None
    peak = max(x["feels"] for x in pm)
    max_pop = max(x["pop"] for x in pm)
    dews = [x["dew"] for x in pm if x["dew"] is not None]
    dew_mean = sum(dews) / len(dews) if dews else None
    if peak <= AFTERNOON_EASY_F:
        flag, level = "Easy afternoon", "easy"
    elif peak <= AFTERNOON_WATCH_F:
        flag, level = "Keep an eye on him", "watch"
    else:
        flag, level = "Short visits only", "hot"
    out = {
        "start": fmt_hour(pm[0]["hour"]),
        "peak_feels": round(peak),
        "max_pop": round(max_pop),
        "dew": round(dew_mean) if dew_mean is not None else None,
        "nice_hours": sum(1 for x in pm if is_nice(x)),
        "total_hours": len(pm),
        "flag": flag, "level": level,
        "rain_note": "Showers or storms likely this afternoon" if max_pop >= 50 else
                     ("Some chance of an afternoon shower" if max_pop >= 30 else ""),
        "vs_summer": None,
    }
    if summer_ref:
        out["vs_summer"] = {
            "summer_feels": summer_ref["feels"],
            "feels_better_by": round(summer_ref["feels"] - peak),
            "summer_dew": summer_ref["dew"],
            "dew_lower_by": round(summer_ref["dew"] - dew_mean) if dew_mean is not None else None,
        }
    return out


def confidence_for(day_index: int) -> str:
    # General rule: forecast skill drops with lead time. Not a measured number.
    if day_index < 3:
        return "High"
    if day_index < 7:
        return "Medium"
    return "Low"


def summarize_text(texts: list[str]) -> str:
    """Pick the most common hourly description during daylight hours."""
    counts: dict[str, int] = {}
    for t in texts:
        if t:
            counts[t] = counts.get(t, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def describe(day: dict, normals_ok: bool) -> str:
    """One plain-English sentence explaining the score."""
    bits = []
    pop = day["rain"]["max_pop"]
    if pop >= 60:
        bits.append(f"rain likely ({pop}% at worst)")
    elif pop >= 30:
        bits.append(f"some rain risk ({pop}% at worst)")
    else:
        bits.append("mostly dry")
    fh = day["temp"]["feels_high"]
    if fh >= 95:
        bits.append(f"very hot, feels like {fh}°F")
    elif fh >= 85:
        bits.append(f"hot, feels like {fh}°F at the warmest")
    elif fh >= 75:
        bits.append(f"warm, feels like {fh}°F")
    elif fh >= 68:
        bits.append(f"mild, feels like {fh}°F at most")
    else:
        bits.append(f"cool, feels like {fh}°F at most")
    if normals_ok and day["dew"]["anomaly"] is not None:
        a = day["dew"]["anomaly"]
        if a <= -3:
            bits.append("drier air than normal")
        elif a >= 3:
            bits.append("muggier than normal")
        else:
            bits.append("normal humidity")
    s = ", ".join(bits)
    return s[0].upper() + s[1:] + "."


def build_days(nws_hours, nws_text, om_hours, normals, now, lat, lon):
    merged = dict(om_hours)
    merged.update(nws_hours)  # NWS wins wherever it has data
    today = now.date()
    normals_ok = bool(normals and normals.get("by_date"))

    # Skip today if less than an hour of usable daylight is left.
    _, sset_today = sun_times(today, lat, lon)
    cutoff_today = sset_today - timedelta(minutes=SUNSET_BUFFER_MIN)
    start_day = today if now <= cutoff_today - timedelta(hours=1) else today + timedelta(days=1)
    summer_ref = (normals or {}).get("summer_ref")
    out = []
    for k in range(DAYS):
        d = start_day + timedelta(days=k)
        all_h = sorted((t, v) for t, v in merged.items() if t.date() == d)
        _, sset = sun_times(d, lat, lon)
        cutoff = sset - timedelta(minutes=SUNSET_BUFFER_MIN)   # an hour counts if it starts by this time
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        daylight = [(t, v) for t, v in all_h
                    if t.hour >= DAY_START and t <= cutoff and (d != today or t >= this_hour)]
        if not daylight:
            continue
        hours = []
        for t, v in daylight:
            rh = v["rh"] if v["rh"] is not None else rh_from_dew(v["t"], v["dew"])
            dew = v["dew"] if v["dew"] is not None else None
            hours.append({"hour": t.hour, "t": v["t"], "dew": dew, "pop": v["pop"],
                          "feels": heat_index_f(v["t"], rh), "src": v["src"],
                          "text": v.get("text", "")})

        max_pop = max(x["pop"] for x in hours)
        feels_high = max(x["feels"] for x in hours)
        dews = [x["dew"] for x in hours if x["dew"] is not None]
        dew_mean = sum(dews) / len(dews) if dews else None
        temps_all = [v["t"] for _, v in all_h]
        nws_share = sum(1 for x in hours if x["src"] == "nws") / len(hours)

        normal = (normals or {}).get("by_date", {}).get(d.strftime("%m-%d")) if normals_ok else None
        anomaly = round(dew_mean - normal["dew"], 1) if (normal and dew_mean is not None) else None

        s_rain = 100 - max_pop
        s_temp = temp_score(feels_high)
        if anomaly is not None:
            s_hum = humid_score(anomaly)
        elif dew_mean is not None:
            s_hum = humid_score_absolute(dew_mean)
        else:
            s_hum = 50.0
        score = round(W_RAIN * s_rain + W_TEMP * s_temp + W_HUMID * s_hum)

        if nws_share >= 0.99:
            source = "National Weather Service"
        elif nws_share <= 0.01:
            source = "Open-Meteo (NOAA and European models)"
        else:
            source = "National Weather Service + Open-Meteo"

        sky = ""
        if d in nws_text and nws_share > 0.5:
            sky = nws_text[d].get("day") or nws_text[d].get("night", "")
        if not sky:
            sky = summarize_text([x["text"] for x in hours])

        day = {
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "label": "Today" if d == today else ("Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A")),
            "partial": d == today,
            "score": score,
            "grade": grade(score),
            "parts": {"rain": round(s_rain), "temp": round(s_temp), "humidity": round(s_hum)},
            "sky": sky,
            "rain": {"max_pop": round(max_pop),
                     "mean_pop": round(sum(x["pop"] for x in hours) / len(hours))},
            "temp": {"high": round(max(temps_all)), "low": round(min(temps_all)),
                     "feels_high": round(feels_high),
                     "normal_high": round(normal["high"]) if normal else None},
            "dew": {"mean": round(dew_mean) if dew_mean is not None else None,
                    "normal": round(normal["dew"]) if normal else None,
                    "anomaly": anomaly},
            "best_window": best_window(hours),
            "nice_hours": sum(1 for x in hours if is_nice(x)),
            "sunset": fmt_clock_rounded(sset),
            "afternoon": afternoon_check(hours, summer_ref),
            "total_hours": len(hours),
            "source": source,
            "confidence": confidence_for(k),
            "hourly": [{"h": x["hour"], "t": round(x["t"]), "f": round(x["feels"]),
                        "p": round(x["pop"])} for x in hours],
        }
        day["summary"] = describe(day, normals_ok)
        out.append(day)

    # Mark the top picks (up to 3, only if they are at least "Good").
    ranked = sorted(out, key=lambda x: (-x["score"], x["date"]))
    picks = [x["date"] for x in ranked[:3] if x["score"] >= 60]
    for x in out:
        x["top_pick"] = x["date"] in picks
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    try:
        lat = float(os.environ["LAT"])
        lon = float(os.environ["LON"])
    except (KeyError, ValueError):
        log("LAT and LON must be set (add them as GitHub repository secrets).")
        return 2

    now = datetime.now(TZ)
    notes = []

    log("Fetching National Weather Service forecast")
    try:
        nws_hours, nws_text = fetch_nws(lat, lon)
    except Exception as e:
        log(f"  NWS failed: {e}")
        nws_hours, nws_text = {}, {}
        notes.append("The National Weather Service could not be reached on this update, "
                     "so every day uses Open-Meteo model data.")

    log("Fetching Open-Meteo forecast")
    try:
        om_hours = fetch_open_meteo(lat, lon)
    except Exception as e:
        log(f"  Open-Meteo failed: {e}")
        om_hours = {}
        notes.append("Open-Meteo could not be reached on this update, so days beyond "
                     "the 7-day National Weather Service forecast are missing.")

    if not nws_hours and not om_hours:
        log("No forecast data at all. Keeping the previous data.json.")
        return 1

    log("Loading humidity normals")
    try:
        normals = load_normals(lat, lon, now.date())
    except Exception as e:
        log(f"  normals failed: {e}")
        normals = None
        notes.append("Historical normals were unavailable, so humidity is scored on a "
                     "fixed scale (55°F dew point good, 75°F bad) instead of against normal.")

    days = build_days(nws_hours, nws_text, om_hours, normals, now, lat, lon)
    if not days:
        log("Forecast data had no usable days. Keeping the previous data.json.")
        return 1

    data = {
        "dog": DOG_NAME,
        "generated_at": now.isoformat(timespec="minutes"),
        "timezone": now.tzname(),
        "day_hours": f"{fmt_hour(DAY_START)} until {SUNSET_BUFFER_MIN} minutes before sunset",
        "summer_ref": (normals or {}).get("summer_ref"),
        "afternoon_rule": [AFTERNOON_EASY_F, AFTERNOON_WATCH_F],
        "stretch_hours": STRETCH_HOURS,
        "nice_rule": [NICE_FEELS_F, NICE_POP],
        "weights": {"rain": W_RAIN, "temp": W_TEMP, "humidity": W_HUMID},
        "temp_range": [TEMP_BEST_F, TEMP_WORST_F],
        "dew_swing": DEW_SWING_F,
        "normals_years": normals.get("years") if normals else None,
        "notes": notes,
        "days": days,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1))
    log(f"Wrote {len(days)} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
