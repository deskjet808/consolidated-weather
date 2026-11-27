"""Reality Check: how often the app's forecasts, the Bureau's and four computer models came true.

For each place in scraper/reality.json this keeps data/reality/<id>.json:
{id, name, station, updated,
 days: {"2026-11-23": {"obs": {tmax, tmin, rain, w15: [km/h, direction]},
                       "fc": {source: {"0": [tmax, tmin, rain_mm, chance, w15_kmh, w15_dir], "1": [...], "2": [...]}}}}}
and data/reality/summary.json with the scores worked out from them.

Lead "0" is the same day (made before 6am), "1" the day before, "2" two days before.

What happened comes from the Bureau of Meteorology's Daily Weather Observations for the place's station
(www.bom.gov.au/climate/dwo/, the official daily record), so every source is checked against the same figures:
- max: highest from 9am that day to 9am the next; min: lowest in the 24 hours to 9am that day (the Bureau's rules)
- rain: the gauge's 24 hours to 9am the next morning (the Bureau's rain day), at least 1 mm counting as rain
- wind: the 3pm reading (speed, and direction when it is 6 km/h or more; calmer air has no real direction)

The forecasts:
- app: what the app shows, the middle figure (median) of every source's max and min that day, the Bureau's
  chance of rain (the median of the others when it has none), the median rain amount, and the Bureau's hourly
  wind for 3pm. From data/history/<id>.json, kept by scraper/history.py since 22 Nov 2026.
- bom and each site the app blends (willyweather, weatherzone, windy, ecmwf, icon, foreca): their own figures.
- m_ecmwf, m_gfs, m_ukmo, m_icon: four models by themselves, from Open-Meteo's archive of past model runs
  (previous-runs-api.open-meteo.com, "_previous_day1" and "_previous_day2"), so they reach back to backfill_from.
  Worked out over the same windows as the station's: max 9am to 9am, min to 9am, rain 9am to 9am, wind at 3pm.
  Lead 0 there is the latest run for each hour, which can be later than 6am, so it is a little kinder to them.

Nothing is estimated: a figure is kept only when its whole window is there. Run twice a day by
.github/workflows/reality.yml after scraper/history.py has run.
"""
import csv, io, json, math, os, sys, time, traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "scraper", "reality.json")
LOCATIONS = os.path.join(ROOT, "locations.json")
HISTORY = os.path.join(ROOT, "data", "history")
OUT = os.path.join(ROOT, "data", "reality")
LEADS = ("0", "1", "2")
SCORE_DAYS = 60  # the scores cover the last 60 days
RAIN_MM = 1.0  # a day with at least this much rain counted as wet (the Bureau's "rain day")
SAID_RAIN = 50  # a chance of rain at or above this counts as the forecast saying rain
CALM = 6  # km/h; below this a wind direction isn't checked
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
APP_SOURCES = ("bom", "willyweather", "weatherzone", "windy", "ecmwf", "icon", "foreca")
MODELS = {"m_ecmwf": "ecmwf_ifs", "m_gfs": "gfs_seamless", "m_ukmo": "ukmo_seamless", "m_icon": "icon_seamless"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9"}
DWO = "https://www.bom.gov.au/climate/dwo/{m}/text/{code}.{m}.csv"
PREVIOUS = "https://previous-runs-api.open-meteo.com/v1/forecast"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def r1(v):
    return None if v is None else (int(v) if float(v).is_integer() else round(v, 1))


def median(a):
    a = sorted(x for x in a if x is not None)
    if not a:
        return None
    m = len(a) // 2
    return a[m] if len(a) % 2 else (a[m - 1] + a[m]) / 2


def compass(deg):
    return None if deg is None else DIRS[int((deg % 360) / 22.5 + 0.5) % 16]


def get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45, **kw)
            if r.status_code == 429 and attempt < 2:
                time.sleep(20)
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(10)


# ---- What happened: the Bureau's Daily Weather Observations ----
def dwo_month(code, month):
    """date -> {tmax, tmin, rain_to_9am, w15} from one month's file. The rain on a date fell in the 24 hours to
    9am that date, so it belongs to the day before."""
    text = get(DWO.format(m=month.strftime("%Y%m"), code=code)).content.decode("latin-1")
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 21:
            continue
        try:
            d = date.fromisoformat("-".join(p.zfill(2) for p in row[1].split("-")))
        except ValueError:
            continue
        spd = 0.0 if row[20].strip().lower() == "calm" else num(row[20])
        out[d] = {"tmin": num(row[2]), "tmax": num(row[3]), "rain9": num(row[4]),
                  "w15": None if spd is None else [r1(spd), (row[19].strip() or None) if spd else None]}
    return out


def observations(code, start, end, cache):
    """date -> obs for start..end, from the months covering them (one fetch per month and station per run)."""
    raw, m = {}, date(start.year, start.month, 1)
    while m <= end + timedelta(days=1):
        key = (code, m)
        if key not in cache:
            try:
                cache[key] = dwo_month(code, m)
            except Exception as e:
                print(f"  daily records {code} {m:%Y-%m}: FAILED ({type(e).__name__}: {e})"[:300], file=sys.stderr)
                cache[key] = {}
        raw.update(cache[key])
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    obs = {}
    d = start
    while d <= end:
        today, nxt = raw.get(d) or {}, raw.get(d + timedelta(days=1)) or {}
        o = {"tmax": r1(today.get("tmax")), "tmin": r1(today.get("tmin")), "rain": r1(nxt.get("rain9")),
             "w15": today.get("w15")}
        if any(v is not None for v in o.values()):
            obs[d.isoformat()] = {k: v for k, v in o.items() if v is not None}
        d += timedelta(days=1)
    return obs


# ---- The models by themselves: Open-Meteo's archive of past runs ----
def model_forecasts(loc, start, end):
    """source -> date -> lead -> row, over the station's windows."""
    suffix = {"0": "", "1": "_previous_day1", "2": "_previous_day2"}
    vars_ = [v + s for v in ("temperature_2m", "precipitation", "wind_speed_10m", "wind_direction_10m")
             for s in suffix.values()]
    j = get(PREVIOUS, params={"latitude": loc["lat"], "longitude": loc["lon"], "hourly": ",".join(vars_),
                              "models": ",".join(MODELS.values()), "timezone": loc["tz"],
                              "start_date": (start - timedelta(days=1)).isoformat(),
                              "end_date": (end + timedelta(days=1)).isoformat()}).json()
    h = j.get("hourly") or {}
    idx = {t: i for i, t in enumerate(h.get("time") or [])}
    out = {}
    for src, model in MODELS.items():
        for lead, sfx in suffix.items():
            col = lambda v: h.get(f"{v}{sfx}_{model}") or []
            T, P, W, D = col("temperature_2m"), col("precipitation"), col("wind_speed_10m"), col("wind_direction_10m")

            def span(series, a, b):
                """Values from hour a to hour b (local datetimes, inclusive), or None if any is missing."""
                vals, t = [], a
                while t <= b:
                    i = idx.get(t.strftime("%Y-%m-%dT%H:%M"))
                    v = series[i] if i is not None and i < len(series) else None
                    if v is None:
                        return None
                    vals.append(v)
                    t += timedelta(hours=1)
                return vals

            d = start
            while d <= end:
                nine = datetime.combine(d, datetime.min.time()) + timedelta(hours=9)
                hot = span(T, nine, nine + timedelta(hours=24))
                cold = span(T, nine - timedelta(hours=24), nine)
                wet = span(P, nine + timedelta(hours=1), nine + timedelta(hours=24))
                three = nine + timedelta(hours=6)
                ws, wd = span(W, three, three), span(D, three, three)
                row = [r1(max(hot)) if hot else None, r1(min(cold)) if cold else None,
                       r1(sum(wet)) if wet else None, None,
                       r1(round(ws[0])) if ws else None, compass(wd[0]) if wd else None]
                if any(v is not None for v in row):
                    out.setdefault(src, {}).setdefault(d.isoformat(), {})[lead] = row
                d += timedelta(days=1)
    return out


# ---- The app and the sources it blends, from the hourly collector ----
def app_forecasts(hist):
    """source -> date -> lead -> row, for "app" and each source the app blends."""
    out = {}
    for d, day in (hist.get("days") or {}).items():
        fc, w15 = day.get("fc") or {}, (day.get("w15") or {}).get("bom") or {}
        for lead in LEADS:
            rows = {s: f[lead] for s, f in fc.items() if isinstance(f, dict) and isinstance(f.get(lead), list)}
            wind = w15.get(lead) or [None, None]
            for s, p in rows.items():
                amt = p[3] if p[3] is not None else p[2]
                row = [p[0], p[1], amt, p[4], *(wind if s == "bom" else [None, None])]
                out.setdefault(s, {}).setdefault(d, {})[lead] = row
            if not rows:
                continue
            b = rows.get("bom")
            chance = b[4] if b and b[4] is not None else median([p[4] for p in rows.values()])
            app = [r1(median([p[0] for p in rows.values()])), r1(median([p[1] for p in rows.values()])),
                   r1(median([p[3] if p[3] is not None else p[2] for p in rows.values()])), r1(chance), *wind]
            if app[0] is not None or app[1] is not None:
                out.setdefault("app", {}).setdefault(d, {})[lead] = app
    return out


# ---- Scores ----
def said_rain(row):
    """True, False or None: whether a forecast row said it would rain."""
    if row[3] is not None:
        return row[3] >= SAID_RAIN
    if row[2] is not None:
        return row[2] >= RAIN_MM
    return None


def dir_gap(a, b):
    if a not in DIRS or b not in DIRS:
        return None
    g = abs(DIRS.index(a) - DIRS.index(b)) % 16
    return min(g, 16 - g)


def score(pairs):
    """pairs: [(date, fc row, obs)]. Plain counts and averages, rounded for reading."""
    s = {"days": len(pairs), "first": min((d for d, _, _ in pairs), default=None),
         "last": max((d for d, _, _ in pairs), default=None)}
    for k, i in (("max", 0), ("min", 1)):
        e = [f[i] - o[f"t{k}"] for _, f, o in pairs if f[i] is not None and o.get(f"t{k}") is not None]
        if e:
            s[k] = {"n": len(e), "off": round(sum(map(abs, e)) / len(e), 2), "lean": round(sum(e) / len(e), 2),
                    "within2": sum(abs(x) <= 2 for x in e), "worst": r1(max(e, key=abs))}
    r = [(said_rain(f), o["rain"] >= RAIN_MM) for _, f, o in pairs if said_rain(f) is not None and o.get("rain") is not None]
    if r:
        s["rain"] = {"n": len(r), "hit": sum(a and b for a, b in r), "false_alarm": sum(a and not b for a, b in r),
                     "missed": sum(b and not a for a, b in r), "dry_right": sum(not a and not b for a, b in r)}
    cal = {}
    for _, f, o in pairs:
        if f[3] is not None and o.get("rain") is not None:
            band = min(int(f[3] // 20), 4)
            c = cal.setdefault(band, [0, 0])
            c[0] += 1
            c[1] += o["rain"] >= RAIN_MM
    if cal:
        s["chance"] = [[b * 20, b * 20 + 19 if b < 4 else 100, n, wet] for b, (n, wet) in sorted(cal.items())]
    w = [(f[4] - o["w15"][0], dir_gap(f[5], o["w15"][1]) if o["w15"][0] >= CALM else None)
         for _, f, o in pairs if f[4] is not None and o.get("w15") and o["w15"][0] is not None]
    if w:
        g = [x for _, x in w if x is not None]
        s["wind"] = {"n": len(w), "off": round(sum(abs(e) for e, _ in w) / len(w), 1),
                     "lean": round(sum(e for e, _ in w) / len(w), 1), "dir_n": len(g), "dir_ok": sum(x <= 2 for x in g)}
    return s


def place_summary(rec, today):
    since = (today - timedelta(days=SCORE_DAYS)).isoformat()
    days = {d: v for d, v in rec["days"].items() if since <= d < today.isoformat() and v.get("obs")}
    by_src = {}
    for d, v in days.items():
        for src, leads in (v.get("fc") or {}).items():
            for lead, row in leads.items():
                by_src.setdefault(src, {}).setdefault(lead, []).append((d, row, v["obs"]))
    scores = {src: {lead: score(p) for lead, p in leads.items()} for src, leads in by_src.items()}
    # Fair comparisons: each source against the app on only the days both have
    vs = {}
    app = by_src.get("app", {})
    for src, leads in by_src.items():
        if src == "app":
            continue
        for lead, pairs in leads.items():
            mine = {d: row for d, row, _ in app.get(lead, [])}
            both = [(d, row, o) for d, row, o in pairs if d in mine]
            if both:
                vs.setdefault(src, {})[lead] = {"them": score(both),
                                                "app": score([(d, mine[d], o) for d, _, o in both])}
    return {"id": rec["id"], "name": rec["name"], "station": rec["station"], "scores": scores, "vs_app": vs,
            "obs_days": len(days)}


def update_place(p, loc, today, cache):
    path = os.path.join(OUT, f"{p['id']}.json")
    try:
        rec = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        rec = {"days": {}}
    start = date.fromisoformat(CONFIG_DATA["backfill_from"])
    have = sorted(rec.get("days") or {})
    # Once the backfill is in, only the last 10 days are worked out again (later records are final)
    recent = max(start, today - timedelta(days=10)) if have and have[0] <= start.isoformat() else start
    end = today - timedelta(days=1)
    notes = []
    obs = observations(p["dwo"], recent, end, cache)
    try:
        models = model_forecasts(loc, recent, today + timedelta(days=0))
    except Exception as e:
        models = {}
        notes.append(f"models FAILED ({type(e).__name__}: {e})"[:200])
    try:
        hist = json.load(open(os.path.join(HISTORY, f"{p['id']}.json"), encoding="utf-8"))
    except (OSError, ValueError):
        hist = {}
    fcs = {**app_forecasts(hist), **models}
    days = rec.get("days") or {}
    for d, o in obs.items():
        days.setdefault(d, {})["obs"] = o
    for src, ds in fcs.items():
        if src not in APP_SOURCES and src != "app" and src not in MODELS:
            continue
        for d, leads in ds.items():
            if src in MODELS and d < recent.isoformat():
                continue  # the archive is only asked for recent days; keep what's stored
            days.setdefault(d, {}).setdefault("fc", {})[src] = leads
    rec = {"id": p["id"], "name": loc["name"],
           "station": {k: p[k] for k in ("station", "station_name", "dwo", "km", "setting", "note") if k in p}
                      | ({"far": True} if p.get("far") else {}),
           "updated": now_iso(), "days": dict(sorted(days.items()))}
    write_json(path, rec)
    notes.append(f"{len(obs)} days of records, models for {len(models.get('m_ecmwf', {}))} days")
    return rec, notes


def write_json(path, obj):
    """One day per line, so the files stay readable and their changes small."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if "days" in obj:
            head = {k: v for k, v in obj.items() if k != "days"}
            f.write(json.dumps(head, ensure_ascii=False)[:-1] + ',\n"days":{\n')
            f.write(",\n".join(f"{json.dumps(d)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                               for d, v in obj["days"].items()))
            f.write("\n}}\n")
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.write("\n")
    os.replace(tmp, path)


def main():
    global CONFIG_DATA
    CONFIG_DATA = json.load(open(CONFIG, encoding="utf-8"))
    locs = {l["id"]: l for l in json.load(open(LOCATIONS, encoding="utf-8"))}
    places = CONFIG_DATA["places"]
    if len(sys.argv) > 1:
        places = [p for p in places if p["id"] in sys.argv[1:]]
    today = datetime.now(ZoneInfo("Australia/Brisbane")).date()
    cache, summaries, ok = {}, [], 0
    for p in places:
        try:
            rec, notes = update_place(p, locs[p["id"]], today, cache)
            summaries.append(place_summary(rec, today))
            ok += 1
            print(f"{p['id']}: " + "; ".join(notes))
        except Exception as e:
            traceback.print_exc()
            print(f"{p['id']}: FAILED ({type(e).__name__}: {e})"[:300], file=sys.stderr)
    if len(sys.argv) == 1 and summaries:
        write_json(os.path.join(OUT, "summary.json"),
                   {"updated": now_iso(), "score_days": SCORE_DAYS, "rain_mm": RAIN_MM, "said_rain": SAID_RAIN,
                    "places": summaries})
    return 0 if ok else 1


CONFIG_DATA = {}

if __name__ == "__main__":
    sys.exit(main())
