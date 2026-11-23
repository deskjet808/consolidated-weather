"""Keep each source's forecasts and what actually happened, for the app's accuracy scoreboard.

Reads the data/<id>.json files scrape.py writes and keeps data/history/<id>.json:
{id, updated, station: {bom_id, name, distance_km, state, wmo}, samples: [[local time, temp, rain since 9am]],
 days: {"2026-11-23": {"fc": {source: {"1": [tmax, tmin, rain_min, rain_max, chance], "2": [...], "3": [...]}},
                       "actual": {tmax, tmin, rain, via}}}}

Forecasts: each source's figures for a day from its last fetch 1, 2 and 3 days before, by the location's own
calendar (a fetch at 11pm on the 22nd is 1 day ahead for the 23rd). A later fetch on the same day replaces an
earlier one, so each lead keeps the last forecast made before that day began. Nothing is ever estimated.

What happened: the Bureau of Meteorology station nearest the location (the one its app reports), from
- the station's observations page data (www.bom.gov.au/fwo/...): every half-hour reading of the last 72 hours.
  Its WMO number comes from the Bureau's list of stations (stations.txt), looked up once and kept; and
- each run's reading from the Bureau app's feed, with the max and min it reports and their times. These are
  kept in "samples" for 3 days, so they also cover a day the observations page couldn't be read, and catch
  peaks between half-hour readings.
Following the Bureau's own climate rules (its Daily Weather Observations use the same), the top temperature for
a day is the highest reading from 9am that day to 9am the next, and the bottom temperature the lowest from 9am
the day before to 9am that day. Rain is midnight to midnight, the day the forecast sites give rain for, worked
out from the station's rain-since-9am gauge. A figure is kept only once its whole window has passed and the
readings cover it with no gap over MAX_GAP.

Runs hourly from its own workflow (.github/workflows/history.yml), so a failure here never stops the scrape.
"""
import json, math, os, sys, time, traceback
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATIONS = os.path.join(ROOT, "locations.json")
DATA = os.path.join(ROOT, "data")
HISTORY = os.path.join(DATA, "history")
KEEP_DAYS = 120  # days of forecasts and results kept per location (under 1 KB a day)
LEADS = (1, 2, 3)  # how many days ahead a forecast was made
# A result is worked out only while its window began within the last 71 hours, when the observations page's
# 72 hours still hold all of it; after that it is left as it is, so a later run with less to go on can't spoil it
RECENT = timedelta(hours=71)
SAMPLES_KEPT = timedelta(hours=72)  # the app readings needed for that
MAX_GAP = timedelta(hours=3)  # longest stretch without a reading that still counts as covered
TIME_BUDGET_MINUTES = 10  # stop starting new locations after this (job timeout 20)
STATIONS = "https://www.bom.gov.au/climate/data/lists_by_element/stations.txt"
# The observations page for each state ("Latest Weather Observations"); every station in the state is in it
OBS_PRODUCT = {"QLD": "IDQ60801", "NSW": "IDN60801", "ACT": "IDN60801", "VIC": "IDV60801", "SA": "IDS60801",
               "WA": "IDW60801", "TAS": "IDT60801", "NT": "IDD60801"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
GIVE_UP = 3  # after this many failures in a row from one feed, stop asking it this run (it is probably blocking us)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    """Number or None (also for "-", "nan" and the like); whole floats become ints so the JSON reads cleanly."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace("%", "").strip())
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return int(f) if f.is_integer() else f


def temp(v):
    """A temperature, or None if it is missing or can't be real (a unit slip or a broken sensor)."""
    v = num(v)
    return v if v is not None and -15 <= v <= 55 else None


def utc(s):
    """An ISO time with its zone ('2026-11-23T08:11:54Z', '2026-11-23T18:20+10:00') as an aware time, or None."""
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r


def masked(e):
    return f"{type(e).__name__}: {e}"[:300]


# ---- Forecasts: what each source said, 1 to 3 days ahead ----
def snapshot(hist, scraped, tz):
    """Copy each source's forecasts for the next few days out of a data/<id>.json file. Returns how many."""
    n = 0
    for s in scraped.get("sources") or []:
        if not isinstance(s, dict):
            continue
        src, fetched = s.get("source"), utc(s.get("fetched"))
        if not src or not fetched:
            continue
        # A source that failed keeps its last good entry, with the time it was really fetched
        made = fetched.astimezone(tz).date()
        for d in s.get("days") or []:
            try:
                target = date.fromisoformat(d.get("date"))
            except (AttributeError, TypeError, ValueError):
                continue
            lead = (target - made).days
            if lead not in LEADS:
                continue
            row = [temp(d.get("tmax")), temp(d.get("tmin")), num(d.get("rain_min")), num(d.get("rain_max")),
                   num(d.get("chance"))]
            if all(v is None for v in row):
                continue
            day = hist["days"].setdefault(target.isoformat(), {})
            day.setdefault("fc", {}).setdefault(src, {})[str(lead)] = row
            n += 1
    return n


# ---- What happened: the Bureau station's readings ----
_stations, _stations_error = None, None


def station_list():
    """Every Bureau station's number -> (state, WMO number or None), from its 2.6 MB list. Fetched at most once a
    run, and only when a location's station is new to its history file."""
    global _stations, _stations_error
    if _stations_error:
        raise RuntimeError(f"station list unavailable ({_stations_error})")
    if _stations is None:
        try:
            text = get(STATIONS).text
        except Exception as e:
            _stations_error = masked(e)
            raise
        _stations = {}
        for line in text.splitlines():
            # Columns: Site, Dist, Site name (has spaces), Start, End, Lat, Lon, Source, STA, Height, Bar_ht, WMO;
            # counted from the right, as the name can be any number of words
            p = line.split()
            if len(p) < 9 or not p[0].isdigit():
                continue
            _stations[p[0]] = (p[-4], int(p[-1]) if p[-1].isdigit() else None)
    return _stations


def local_iso(when, tz):
    """A time as the location's local clock with its offset, to the minute (e.g. 2026-11-23T21:20+10:00)."""
    return when.astimezone(tz).isoformat(timespec="minutes")


def app_readings(loc, tz):
    """The Bureau app feed's latest reading for the location: (station, [[time, temp, rain since 9am], ...]).
    Besides the reading itself, its max and min so far (with the times they happened) are real readings too,
    and catch peaks that fall between half-hour readings."""
    j = get(f"https://api.weather.bom.gov.au/v1/locations/{loc['bom']}/observations").json()
    d = j.get("data") or {}
    st = d.get("station") or {}
    out = []
    when = utc((j.get("metadata") or {}).get("observation_time"))
    if when:
        out.append([local_iso(when, tz), temp(d.get("temp")), num(d.get("rain_since_9am"))])
    for k in ("max_temp", "min_temp"):
        x = d.get(k) or {}
        w, v = utc(x.get("time")), temp(x.get("value"))
        if w and v is not None:
            out.append([local_iso(w, tz), v, None])
    return st, out


def fwo_readings(state, wmo, tz):
    """The last 72 hours of the station's half-hour readings from its observations page: [[time, temp, rain]]."""
    product = OBS_PRODUCT.get(state)
    if not product or not wmo:
        return []
    j = get(f"https://www.bom.gov.au/fwo/{product}/{product}.{wmo}.json").json()
    out = []
    for r in (j.get("observations") or {}).get("data") or []:
        if not isinstance(r, dict):
            continue
        try:
            when = datetime.strptime(str(r.get("aifstime_utc")), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append([local_iso(when, tz), temp(r.get("air_temp")), num(r.get("rain_trace"))])
    return out


def merge_readings(*lists):
    """Readings from any feed as one sorted list of (time, temp, rain), one per time; where two feeds give the
    same time, the first given fills in and the later one only adds what it lacked."""
    seen = {}
    for lst in lists:
        for s in lst or []:
            if not isinstance(s, (list, tuple)) or len(s) < 3:
                continue
            t = utc(s[0])
            if not t:
                continue
            tv, rv = temp(s[1]), num(s[2])
            if t in seen:
                _, t0, r0 = seen[t]
                tv, rv = (t0 if t0 is not None else tv), (r0 if r0 is not None else rv)
            seen[t] = (t, tv, rv)
    return sorted(seen.values(), key=lambda x: x[0])


def at(d, hour, tz):
    """A clock time on a local date (9am or midnight), as an aware time."""
    return datetime.combine(d, dtime(hour), tzinfo=tz)


def covered(times, start, end):
    """Whether readings at these sorted times run from start to end with no gap over MAX_GAP."""
    pts = [start] + [t for t in times if start <= t <= end] + [end]
    return len(pts) > 2 and all(b - a <= MAX_GAP for a, b in zip(pts, pts[1:]))


def extreme(rows, start, end, pick):
    """The highest (pick=max) or lowest reading from start to end, if the readings cover it."""
    pts = [(t, v) for t, v, _ in rows if v is not None and start <= t <= end]
    if not covered([t for t, _ in pts], start, end):
        return None
    return pick(v for _, v in pts)


def resets_between(a, b, tz):
    """Whether the rain gauge's 9am reset falls after reading a and before reading b. The 9am reading itself
    still holds the 24 hours to 9am; the next one starts again from zero."""
    nine = at(a.astimezone(tz).date(), 9, tz)
    if nine < a:
        nine = at(a.astimezone(tz).date() + timedelta(days=1), 9, tz)
    return nine < b


def rain_total(rows, start, end, tz):
    """Rain from start to end, adding up the rise in the rain-since-9am gauge between readings."""
    pts = [(t, r) for t, _, r in rows if r is not None and r >= 0]
    before = [p for p in pts if p[0] <= start]
    inside = [p for p in pts if start < p[0] <= end]
    if not before or not inside:
        return None
    chain = [before[-1]] + inside
    if start - chain[0][0] > MAX_GAP or end - chain[-1][0] > MAX_GAP:
        return None
    if any(b[0] - a[0] > MAX_GAP for a, b in zip(chain, chain[1:])):
        return None
    total = 0.0
    for (ta, ra), (tb, rb) in zip(chain, chain[1:]):
        # After a reset the gauge shows only what fell since 9am; a fall in the gauge otherwise is a correction
        total += rb if resets_between(ta, tb, tz) else max(0.0, rb - ra)
    return num(round(total, 1))


def actual_for(rows, d, tz, now):
    """What happened on local date d, for each figure whose whole window has passed and is covered."""
    wins = {"tmax": (at(d, 9, tz), at(d + timedelta(days=1), 9, tz)),
            "tmin": (at(d - timedelta(days=1), 9, tz), at(d, 9, tz)),
            "rain": (at(d, 0, tz), at(d + timedelta(days=1), 0, tz))}
    out = {}
    for k, (start, end) in wins.items():
        if end > now or now - start > RECENT:
            continue  # still going, or settled by earlier runs
        if k == "rain":
            out[k] = rain_total(rows, start, end, tz)
        else:
            out[k] = extreme(rows, start, end, max if k == "tmax" else min)
    return {k: v for k, v in out.items() if v is not None}


# ---- One location ----
def load_history(path, loc):
    """The location's history so far, or a fresh one. A file that can't be read is left alone (raises), so a
    bad write can never wipe months of results."""
    if not os.path.exists(path):
        return {"id": loc["id"], "days": {}}
    with open(path, encoding="utf-8") as f:
        h = json.load(f)
    if not isinstance(h, dict) or not isinstance(h.get("days"), dict):
        raise ValueError("history file has an unexpected shape")
    return h


def write_history(path, hist):
    """Compact JSON with one line per day, so each run's change is a small diff."""
    c = lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    days = hist.get("days") or {}
    head = ",\n".join(f"{c(k)}:{c(v)}" for k, v in hist.items() if k != "days")
    body = ",\n".join(f"{c(k)}:{c(days[k])}" for k in sorted(days))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("{" + head + ',\n"days":{\n' + body + "\n}}\n")
    os.replace(tmp, path)


def update_location(loc, now, feeds):
    """Add the latest forecasts and results to one location's history. feeds holds this run's shared state:
    observations pages already fetched (several locations share a station) and failure counts."""
    tz = ZoneInfo(loc["tz"])
    path = os.path.join(HISTORY, f"{loc['id']}.json")
    hist = load_history(path, loc)
    hist["id"] = loc["id"]
    notes = []

    try:
        with open(os.path.join(DATA, f"{loc['id']}.json"), encoding="utf-8") as f:
            n = snapshot(hist, json.load(f), tz)
        notes.append(f"{n} forecasts")
    except Exception as e:  # results can still be kept without today's forecasts
        notes.append(f"no forecasts ({masked(e)})")

    # The app feed names the station and gives this run's reading
    new = []
    if feeds["app_fail"] < GIVE_UP:
        try:
            st, new = app_readings(loc, tz)
            feeds["app_fail"] = 0
            bom_id = str(st.get("bom_id") or "")
            old = hist.get("station") or {}
            if bom_id and old.get("bom_id") != bom_id:
                # A new station (or the first run): earlier readings were from another one, so they go
                hist["station"], hist["samples"] = {"bom_id": bom_id}, []
            if bom_id:
                metres = num(st.get("distance"))
                hist["station"].update({"name": st.get("name"),
                                        "distance_km": num(round(metres / 1000, 1)) if metres else None})
        except Exception as e:
            feeds["app_fail"] += 1
            notes.append(f"app feed FAILED ({masked(e)})")
    station = hist.get("station") or {}
    if station.get("bom_id") and "wmo" not in station:
        try:
            sta, wmo = station_list().get(station["bom_id"], (loc.get("state"), None))
            station.update({"state": sta, "wmo": wmo})
        except Exception as e:
            notes.append(f"station list FAILED ({masked(e)})")  # tried again next run

    # The station's last 72 hours, fetched once a run however many locations share it
    half = []
    key = (station.get("state"), station.get("wmo"))
    if station.get("wmo") and key in feeds["fwo"]:
        half = feeds["fwo"][key]
    elif station.get("wmo") and feeds["fwo_fail"] < GIVE_UP:
        try:
            half = fwo_readings(key[0], key[1], tz)
            feeds["fwo_fail"] = 0
        except Exception as e:
            feeds["fwo_fail"] += 1
            notes.append(f"observations page FAILED ({masked(e)})")
        feeds["fwo"][key] = half

    keep = now - SAMPLES_KEPT
    hist["samples"] = [[local_iso(t, tz), v, r] for t, v, r in merge_readings(hist.get("samples"), new)
                       if t >= keep and (v is not None or r is not None)]
    rows = merge_readings(half, hist["samples"])
    today = now.astimezone(tz).date()
    filled = 0
    for back in range(3, -1, -1):  # windows that began in the last 71 hours belong to these days
        d = today - timedelta(days=back)
        got = actual_for(rows, d, tz, now)
        if not got:
            continue
        day = hist["days"].setdefault(d.isoformat(), {})
        act = day.setdefault("actual", {})
        if any(act.get(k) != v for k, v in got.items()):
            act.update(got)
            act["via"] = "half-hourly" if half else "app"
            filled += 1
    notes.append(f"{filled} days of results updated" if filled else "no new results")

    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    hist["days"] = {k: v for k, v in hist["days"].items() if k >= cutoff}
    hist["updated"] = now_iso()
    # Keep the file's keys in a fixed order (id, updated, station, samples, days) for tidy diffs
    order = ["id", "updated", "station", "samples", "days"]
    hist = {k: hist[k] for k in order if k in hist}
    write_history(path, hist)
    return notes


def main():
    locs = json.load(open(LOCATIONS, encoding="utf-8"))
    if len(sys.argv) > 1:
        locs = [l for l in locs if l["id"] in sys.argv[1:]]  # for trying one or two by hand
    feeds = {"fwo": {}, "fwo_fail": 0, "app_fail": 0}
    ok, start = 0, time.monotonic()
    for i, loc in enumerate(locs):
        if time.monotonic() - start > TIME_BUDGET_MINUTES * 60:
            print(f"Time budget used; {len(locs) - i} left for the next run")
            break
        try:
            notes = update_location(loc, datetime.now(timezone.utc), feeds)
            ok += 1
            print(f"{loc['id']}: " + "; ".join(notes))
        except Exception as e:
            traceback.print_exc()
            print(f"{loc['id']}: FAILED ({masked(e)})", file=sys.stderr)
    # Fail only if no location could be updated, so partial results still get committed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
