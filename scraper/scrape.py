"""Scrape each forecast site for the locations in locations.json and write data/<id>.json.

Shape per file: {id, generated, sources: [{source, name, url, fetched, point?, days: [{date, tmax, tmin,
rain_min, rain_max, rain_text, chance, text}]}], observations: {...}, errors: {source: message}}.
A source that fails keeps its last good entry (its "fetched" time shows how old it is).

Runs are staggered: each run updates only the locations that are due (last updated DUE_MINUTES ago
or more), oldest first, and at most its share of them (a sixth of all locations, as there are six runs
in 3 hours). So every location refreshes about every 3 hours without all the sites being hit at once,
and after a run that updates everything (--all) the locations spread back out into separate
runs within a cycle. GitHub often starts scheduled runs hours late, so locations not updated for
OVERDUE_MINUTES are taken as well, within TIME_BUDGET_MINUTES.
--all updates every location.
"""
import argparse, json, math, os, re, sys, time, traceback
from html import unescape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATIONS = os.path.join(ROOT, "locations.json")
DATA = os.path.join(ROOT, "data")
DUE_MINUTES = 165  # a little under 3 hours, so a late run doesn't push a location past its slot
RUNS_PER_CYCLE = 6  # a run every 30 minutes, each location every 3 hours
OVERDUE_MINUTES = 330  # past this, a location is updated even beyond the run's share (GitHub ran late)
TIME_BUDGET_MINUTES = 30  # stop starting new locations after this, leaving time to commit (job timeout 45)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    """Number or None; whole floats become ints so the JSON reads cleanly."""
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace("%", "").strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def rain_range(text):
    """'1-5mm', '1–5 mm', '5 - 10 mm' -> (1, 5); '< 1mm' -> (0, 1)."""
    if not text:
        return None, None
    t = text.replace("–", "-").replace("\xa0", " ")
    vals = [num(n) for n in re.findall(r"\d+(?:\.\d+)?", t)]
    if "<" in t and vals:
        return 0, vals[0]
    if len(vals) >= 2:
        return vals[0], vals[1]
    if len(vals) == 1:
        return vals[0], vals[0]
    return None, None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def day(date, tmax=None, tmin=None, rain_text=None, rain_min=None, rain_max=None, chance=None, text=None):
    if rain_text is not None and rain_min is None and rain_max is None:
        rain_min, rain_max = rain_range(rain_text)
    return {"date": date, "tmax": num(tmax), "tmin": num(tmin), "rain_min": num(rain_min),
            "rain_max": num(rain_max), "rain_text": clean(rain_text) or None,
            "chance": num(chance), "text": clean(text) or None}


def get(url, headers=None, **kw):
    r = requests.get(url, headers={**HEADERS, **(headers or {})}, timeout=30, **kw)
    r.raise_for_status()
    return r


def compass(deg):
    if deg is None:
        return None
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][round(deg / 22.5) % 16]


def with_point(out, loc, place):
    """Name the site's own place when it isn't the location itself (e.g. Eurong for K'gari)."""
    p, n = clean(place).lower(), loc["name"].lower()
    if p and p not in n and n not in p:
        out["point"] = clean(place)
    return out


def local_hour(when, tz):
    """A timezone-aware time as the location's local clock hour, without a timezone (e.g. 2026-11-04 14:00)."""
    return when.astimezone(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)


def pack_hours(pts, step=1):
    """A source's hour-by-hour forecast, for the app's hourly chart, as lists running from the first hour:
    {"start": "2026-11-04T05", "temp": [...], "chance": [...], "mm": [...], "text": [...]}, None where missing.
    pts maps local clock hours to (temp, chance %, rain mm, description). A 3-hourly series (step=3) is filled
    in hour by hour: temperature joined in a straight line, chance and description held, rain spread evenly."""
    pts = {k: v for k, v in pts.items() if any(x is not None for x in v)}
    if not pts:
        return None
    ks = sorted(pts)
    if step > 1:
        full = {}
        for a, b in zip(ks, ks[1:] + [None]):
            n = step if b is None else max(1, min(step, int((b - a).total_seconds() // 3600)))
            ta, ca, ma, xa = pts[a]
            tb = pts[b][0] if b else None
            for i in range(n):
                t = ta if ta is None or tb is None else round(ta + (tb - ta) * i / n, 1)
                full[a + timedelta(hours=i)] = (t, ca, None if ma is None else round(ma / n, 2), xa)
        pts, ks = full, sorted(full)
    start = ks[0]
    size = int((ks[-1] - start).total_seconds() // 3600) + 1
    cols = [[None] * size for _ in range(4)]
    for k, v in pts.items():
        i = int((k - start).total_seconds() // 3600)
        for c in range(4):
            cols[c][i] = num(v[c]) if c < 3 else (clean(v[c]) or None)
    return {"start": start.strftime("%Y-%m-%dT%H"), "temp": cols[0], "chance": cols[1], "mm": cols[2], "text": cols[3]}


def try_hours(out, make):
    """Add a source's hourly forecast to its result when it can be read; the daily figures never depend on it."""
    try:
        h = make()
        if h:
            out["hourly"] = h
    except Exception as e:
        out["hourly_error"] = masked(e)
    return out


# ---- Bureau of Meteorology: JSON feed used by its own app ----
def scrape_bom(loc):
    url = f"https://api.weather.bom.gov.au/v1/locations/{loc['bom']}/forecasts/daily"
    tz = ZoneInfo(loc["tz"])
    days = []
    for d in get(url).json()["data"]:
        # Dates are local midnight expressed in UTC (e.g. 2026-10-26T14:00:00Z is 27 Oct in Brisbane)
        local = datetime.fromisoformat(d["date"].replace("Z", "+00:00")).astimezone(tz).date()
        amt = (d.get("rain") or {}).get("amount") or {}
        lo, hi = amt.get("min"), amt.get("max")
        txt = None
        if lo is not None or hi is not None:
            txt = f"{lo or 0}-{hi} mm" if hi is not None else f"{lo} mm"
        days.append(day(local.isoformat(), d.get("temp_max"), d.get("temp_min"), txt, lo, hi,
                        (d.get("rain") or {}).get("chance"), d.get("short_text")))
    return {"source": "bom", "name": "BOM official", "url": "https://www.bom.gov.au/", "days": days}


# ---- WillyWeather: plain HTML, temperatures on the main page, rain on the rainfall page ----
def scrape_willyweather(loc):
    url = f"https://www.willyweather.com.au/{loc['willyweather']}.html"
    main_html = get(url).text
    rain_html = get(f"https://rainfall.willyweather.com.au/{loc['willyweather']}.html").text
    main, rain = BeautifulSoup(main_html, "html.parser"), BeautifulSoup(rain_html, "html.parser")
    chance = {}
    for li in rain.select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        cv, ca = li.select_one(".chance-value"), li.select_one(".chance-amount")
        chance[t["datetime"]] = (cv.get_text() if cv else None, ca.get_text() if ca else None)
    days = []
    for li in main.select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        mn, mx, pr = li.select_one("li.min"), li.select_one("li.max"), li.select_one(".short-precis")
        c, amt = chance.get(t["datetime"], (None, None))
        days.append(day(t["datetime"], mx.get("data-max") if mx else None, mn.get("data-min") if mn else None,
                        amt, chance=c, text=pr.get_text() if pr else None))
    if not days:
        raise ValueError("no forecast days found in page")
    h1 = main.select_one("article h1")
    place = h1.find(string=True, recursive=False) if h1 else ""
    out = with_point({"source": "willyweather", "name": "WillyWeather", "url": url, "days": days}, loc, place)
    return try_hours(out, lambda: ww_hours(main_html, rain_html, ZoneInfo(loc["tz"])))


def ww_hours(main_html, rain_html, tz):
    """WillyWeather's graphs: temperature every hour, chance of rain every 3 hours (held for each 3).
    Their times are the local clock written as if it were UTC (midnight in Brisbane is 00:00 "UTC")."""
    at = lambda p: datetime.fromtimestamp(p["x"], timezone.utc).replace(minute=0, second=0, tzinfo=None)
    temps = {at(p): p.get("y") for p in ww_graph(main_html, "temperature")}
    chance = sorted((at(p), p.get("y")) for p in ww_graph(rain_html, "rainfallprobability"))
    pts = {}
    for h, t in temps.items():
        c = next((v for k, v in reversed(chance) if k <= h < k + timedelta(hours=3)), None)
        pts[h] = (t, c, None, None)
    return pack_hours(pts)


def capture_json(url, wanted):
    """Load url in headless Chromium and return the JSON bodies of responses whose URL satisfies wanted().
    Used for sites whose forecast only arrives as background requests the page itself signs."""
    from playwright.sync_api import sync_playwright

    found = []

    def on_resp(r):
        if wanted(r.url):
            try:
                found.append(r.json())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-AU", timezone_id="Australia/Brisbane",
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.on("response", on_resp)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(30):  # up to 30 s for the first matching response
                if found:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(5000)  # let any later matching responses arrive
        finally:
            browser.close()
    if not found:
        raise ValueError("expected background response never arrived")
    return found


# ---- Weatherzone: forecast arrives as background JSON, so load the page in a browser ----
def scrape_weatherzone(loc):
    url = f"https://www.weatherzone.com/au/{loc['weatherzone']}"
    # The page also fetches 72 hours of hourly figures (part-day), which feed the app's hourly chart
    found = capture_json(url, lambda u: "/weather/forecast/daily/TWCID/" in u and "start=" in u or "/api/forecast/part-day" in u)
    hours = max((b for b in found if isinstance(b, list)), key=len, default=[])
    found = [b for b in found if isinstance(b, dict)]
    if not found:
        raise ValueError("daily forecast response never arrived")
    # The page requests several date ranges from the same endpoint; keep the longest
    payload = max(found, key=lambda b: len(b.get("body") or []))
    place = next(iter(((payload.get("metadata") or {}).get("locations") or {}).values()), {}).get("name", "")
    days = [day(d["date"], d.get("temperatureMax"), d.get("temperatureMin"), d.get("precipitationAmountText"),
                chance=d.get("precipitationProbability"), text=d.get("precis"))
            for d in payload.get("body") or []]
    if not days:
        raise ValueError("forecast response had no days")
    # e.g. Archerfield for Oxley: the forecast point Weatherzone uses
    out = with_point({"source": "weatherzone", "name": "Weatherzone", "url": url, "days": days}, loc, place)
    tz = ZoneInfo(loc["tz"])
    return try_hours(out, lambda: pack_hours({
        local_hour(datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")), tz):
            (h.get("temperature"), h.get("precipitationProbability"), h.get("precipitationRate"), h.get("precis"))
        for h in hours if h.get("timestamp")}))


# ---- Windy: point forecast arrives as background JSON (signed by the page), temperatures in Kelvin ----
def scrape_windy(loc):
    url = f"https://www.windy.com/{loc['lat']}/{loc['lon']}"
    found = capture_json(url, lambda u: "/forecast/point/" in u)
    payload = max(found, key=lambda b: len(b.get("summary") or []))
    header = payload.get("header") or {}
    offset = timedelta(hours=header.get("utcOffset", 10))
    k2c = lambda k: None if k is None else round(k - 273.15, 1)
    data = payload.get("data") or {}
    rain, steps, wind = {}, {}, {}
    n = len(data.get("ts") or [])
    col = lambda k: (data.get(k) or [None] * n)
    for ts, mm, w, g, wd in zip(data.get("ts") or [], col("precipAmount"), col("wind"), col("windGust"), col("windDir")):
        d = (datetime.fromtimestamp(ts / 1000, timezone.utc) + offset).date().isoformat()
        rain[d] = rain.get(d, 0) + (mm or 0)
        steps[d] = steps.get(d, 0) + 1
        # Wind and gusts come in metres per second; keep each day's strongest, in km/h
        if w is not None:
            cur = wind.setdefault(d, {"wind_max": 0, "gust_max": 0, "wind_dir": None})
            if w * 3.6 > cur["wind_max"]:
                cur["wind_max"], cur["wind_dir"] = round(w * 3.6), compass(wd)
            if g is not None:
                cur["gust_max"] = max(cur["gust_max"], round(g * 3.6))
    days = []
    for s in payload.get("summary") or []:
        d = (datetime.fromtimestamp(s["timestamp"] / 1000, timezone.utc) + offset).date().isoformat()
        # Only total rain for days the 3-hourly series fully covers; the last day is often cut short
        full = steps.get(d, 0) >= s.get("segments", 8)
        mm = round(rain[d], 1) if full else None
        days.append({**day(d, k2c(s.get("tempMax")), k2c(s.get("tempMin")),
                           None if mm is None else f"{mm:g} mm", mm, mm), **(wind.get(d, {}) if full else {})})
    if not days:
        raise ValueError("forecast response had no daily summary")
    out = {"source": "windy", "name": "Windy", "url": url, "days": days}
    if header.get("model"):
        out["point"] = f"{header['model']} model"
    # Every 3 hours: temperature and rain for the hourly chart (Windy gives no chance of rain or description)
    tz = ZoneInfo(loc["tz"])
    return try_hours(out, lambda: pack_hours({
        local_hour(datetime.fromtimestamp(ts / 1000, timezone.utc), tz): (k2c(t), None, mm, None)
        for ts, t, mm in zip(data.get("ts") or [], col("temperature"), col("precipAmount"))}, step=3))


# ---- Open-Meteo: free forecast API (no key) for single weather models, by coordinates ----
# Free for non-commercial use with attribution (CC BY 4.0), which the app's cards give
WMO_TEXT = {0: "Clear", 1: "Mostly sunny", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
            51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Drizzle", 57: "Drizzle",
            61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Rain", 67: "Heavy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow",
            80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow", 86: "Snow",
            95: "Storms", 96: "Storms with hail", 99: "Storms with hail"}
OM_DAILY = ("temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
            "weather_code,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant")
OM_HOURLY = "temperature_2m,precipitation_probability,precipitation,weather_code"


def open_meteo(loc, model, source, name):
    # The hourly figures come in the same request, so the hourly chart costs no extra calls
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={loc['lat']}&longitude={loc['lon']}&models={model}"
           f"&daily={OM_DAILY}&hourly={OM_HOURLY}&timezone={loc['tz']}&forecast_days=8")
    # Shared GitHub addresses are sometimes rate-limited, and the API sometimes answers slowly: wait and try again
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(15)
            continue
        if r.status_code != 429 or attempt == 2:
            break
        time.sleep(20)
    r.raise_for_status()
    dl, hl = r.json().get("daily") or {}, r.json().get("hourly") or {}
    days = []
    for i, d in enumerate(dl.get("time") or []):
        col = lambda k: (dl.get(k) or [None] * (i + 1))[i]
        tmax, tmin, mm = col("temperature_2m_max"), col("temperature_2m_min"), col("precipitation_sum")
        if tmax is None and tmin is None:
            continue  # past the model's range
        mm = None if mm is None else round(mm, 1)
        w, g = col("wind_speed_10m_max"), col("wind_gusts_10m_max")
        days.append({**day(d, tmax, tmin, None if mm is None else f"{mm:g} mm", mm, mm,
                           chance=col("precipitation_probability_max"), text=WMO_TEXT.get(col("weather_code"))),
                     "wind_max": None if w is None else round(w), "gust_max": None if g is None else round(g),
                     "wind_dir": compass(col("wind_direction_10m_dominant"))})
    if not days:
        raise ValueError("forecast had no days")
    out = {"source": source, "name": name, "days": days,
           "url": f"https://open-meteo.com/en/docs?latitude={loc['lat']}&longitude={loc['lon']}&models={model}"}
    # Times are already the location's local clock ("2026-11-04T14:00")
    hcol = lambda k: hl.get(k) or [None] * len(hl.get("time") or [])
    return try_hours(out, lambda: pack_hours({
        datetime.fromisoformat(t): (tp, c, mm, WMO_TEXT.get(wc))
        for t, tp, c, mm, wc in zip(hl.get("time") or [], hcol("temperature_2m"), hcol("precipitation_probability"),
                                    hcol("precipitation"), hcol("weather_code"))}))


def scrape_ecmwf(loc):
    """The European Centre's model (ECMWF IFS, 9 km)."""
    return open_meteo(loc, "ecmwf_ifs", "ecmwf", "ECMWF")


def scrape_icon(loc):
    """The German weather service's model (DWD ICON)."""
    return open_meteo(loc, "icon_seamless", "icon", "ICON")


# ---- Foreca: 10-day cards in the plain HTML; "foreca" in locations.json is its place id ----
def scrape_foreca(loc):
    url = f"https://www.foreca.com/en/{loc['foreca']}/{loc['id']}/10-day-forecast"
    soup = BeautifulSoup(get(url).text, "html.parser")
    title = soup.title.get_text() if soup.title else ""  # "10 day forecast - Oxley, Brisbane, AU - Foreca.com"
    today = datetime.now(ZoneInfo(loc["tz"])).date()
    days = []
    for i, c in enumerate(soup.select(".day-container")):
        d = today + timedelta(days=i)
        # The card says "Mon 2": check its day of the month so a stale page can't shift every figure by a day
        label = c.select_one(".time_24h")
        dom = re.search(r"\d+", label.get_text()) if label else None
        if dom and int(dom.group()) != d.day:
            raise ValueError(f"day {i} is labelled {clean(label.get_text())}, expected {d.day}")
        temps = [num(t.get_text().strip().rstrip("°�").strip()) for t in c.select(".temp_c")]
        mm = c.select_one(".rain_mm")
        mm = num(mm.find(string=True, recursive=False)) if mm else None
        sym = c.select_one("img.symb")
        days.append(day(d.isoformat(), temps[0] if temps else None, temps[1] if len(temps) > 1 else None,
                        None if mm is None else f"{mm:g} mm", mm, mm, text=sym.get("title") if sym else None))
    if not days:
        raise ValueError(f"no forecast cards: {clean(title)[:80]}")
    m = re.search(r"forecast - ([^,]+)", title)
    out = with_point({"source": "foreca", "name": "Foreca", "url": url, "days": days}, loc, m.group(1) if m else "")
    return try_hours(out, lambda: foreca_hours(loc))


def foreca_hours(loc):
    """Foreca's hourly pages for today (the hours left) and tomorrow: two extra pages, as chosen for the hourly
    chart. Each page passes its hours to its own chart script as JSON, in local time."""
    pts = {}
    for d in (0, 1):
        html = get(f"https://www.foreca.com/en/{loc['foreca']}/{loc['id']}/hourly?day={d}").text
        m = re.search(r"renderHourly\(\{.*?data:\s*(\[.*?\])\s*,\s*\n", html, re.S)
        if not m:
            raise ValueError(f"no hourly data on day {d}'s page")
        for h in json.loads(m.group(1)):
            pts[datetime.fromisoformat(h["time"])] = (h.get("temp"), h.get("rainp"), h.get("rain"), h.get("wx"))
        time.sleep(1)
    return pack_hours(pts)


# ---- Bureau of Meteorology latest observations from the station nearest the location ----
def scrape_observations(loc):
    j = get(f"https://api.weather.bom.gov.au/v1/locations/{loc['bom']}/observations").json()
    d, st = j.get("data") or {}, (j.get("data") or {}).get("station") or {}
    if d.get("temp") is None:
        raise ValueError("no current reading")
    t = (j.get("metadata") or {}).get("observation_time")
    when = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ZoneInfo(loc["tz"])).isoformat() if t else None
    return {"source": "bom_obs", "station": st.get("name"), "distance_km": num(round(st["distance"] / 1000, 1)) if st.get("distance") else None,
            "time": when, "temp": num(d.get("temp")), "feels_like": num(d.get("temp_feels_like")),
            "humidity": num(d.get("humidity")), "wind_dir": (d.get("wind") or {}).get("direction"),
            "wind_kmh": num((d.get("wind") or {}).get("speed_kilometre")), "gust_kmh": num((d.get("gust") or {}).get("speed_kilometre")),
            "rain_since_9am": num(d.get("rain_since_9am"))}


# ---- Boating: tides and swell from WillyWeather, for coastal locations only ----
def ww_graph(html, key):
    """Hourly points of one WillyWeather forecast graph (e.g. swell-height), or [] if the page has none."""
    i = html.find('{"forecastGraphs"')
    if i < 0:
        return []
    graphs, _ = json.JSONDecoder().raw_decode(html[i:])
    series = ((graphs.get("forecastGraphs") or {}).get(key) or {}).get("dataConfig", {}).get("series", {})
    return [p for g in series.get("groups") or [] for p in g.get("points") or []]


def ww_page(kind, path):
    """A WillyWeather tides or swell page for path. Where the place has no such data, WillyWeather lists
    nearby spots that do; follow the nearest one. Returns (html, place name or None if it's the place itself)."""
    url = f"https://{kind}.willyweather.com.au/{path}.html"
    html = get(url).text
    if "does not have" not in html[:60000]:
        return html, None
    soup = BeautifulSoup(html, "html.parser")
    near = soup.select_one("section.search-results a[href$='.html']")
    if not near:
        return html, None
    html = get(f"https://{kind}.willyweather.com.au{near['href']}" if near["href"].startswith("/") else near["href"]).text
    # The page title names the spot, e.g. "Coolangatta Beach Tide Times, QLD - WillyWeather"
    m = re.search(r"<title>\s*(.+?)\s+(?:Tide Times|Swell Forecast)", html)
    return html, clean(unescape(m.group(1))) if m else None


def ww_tides(path):
    """High and low tides per day from a WillyWeather tides page, and the spot's name if WillyWeather
    sent us to a nearby one."""
    html, place = ww_page("tides", path)
    tides = {}
    for li in BeautifulSoup(html, "html.parser").select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        tides[t["datetime"]] = [{"type": "high" if "point-high" in (p.get("class") or []) else "low",
                                 "time": clean(p.find("h3").get_text()), "height_m": num(clean(p.find("span").get_text()).replace(" m", ""))}
                                for p in li.select("li.point-high, li.point-low") if p.find("h3") and p.find("span")]
    return tides, place


def ww_swell(path, tz):
    """Each day's biggest swell (height, direction, period) from a WillyWeather swell page."""
    html, place = ww_page("swell", path)
    heights, periods = ww_graph(html, "swell-height"), ww_graph(html, "swell-period")
    period_at = {p["x"]: p.get("y") for p in periods}
    swell = {}
    for p in heights:
        if p.get("y") is None:
            continue
        d = datetime.fromtimestamp(p["x"], timezone.utc).astimezone(tz).date().isoformat()
        cur = swell.get(d)
        if cur is None or p["y"] > cur["max_m"]:
            swell[d] = {"max_m": p["y"], "dir": p.get("directionText"), "period_s": num(round(period_at.get(p["x"]) or 0)) or None}
    return swell, place


# ---- Seabreeze: 7-day wind and wave series embedded in the forecast page (knots, every 1.5 hours) ----
def seabreeze(slug, tz):
    """Each day's strongest daytime wind (6am to 6pm) and biggest wave, and the place Seabreeze names."""
    html = get(f"https://www.seabreeze.com.au/weather/wind-forecast/{slug}").text
    i = html.find("sb.Weather.renderGraphs(json)")
    if i < 0:
        raise ValueError("forecast data not in page")
    seg = html[max(html.rfind("var json", 0, i), html.rfind("json =", 0, i)):i]
    obj, _ = json.JSONDecoder().raw_decode(seg[seg.find("{"):])
    graph = next((d for L, d in zip(obj["layout"], obj["data"]) if L["type"] == "F"), None)
    if not graph:
        raise ValueError("no wind and wave graph")
    head, title = graph.split(",", 13), obj["global"].get("location")
    start = datetime.strptime(head[12], "%Y%m%d%H%M").replace(tzinfo=tz)  # the series' local start
    pts = graph.split("7 day wind & wave forecast for ", 1)[1].split(",", 1)[1].split(",")
    days = {}
    for k in range(0, len(pts) - 5, 6):
        t, kts, wdir, wave = pts[k:k + 4]
        when = start + timedelta(hours=int(t) / 100)
        if not 6 <= when.hour <= 18 or not kts:
            continue
        cur = days.setdefault(when.date().isoformat(), {"wind_max": 0, "wind_dir": None, "wave_m": None})
        kmh = round(float(kts) * 1.852)
        if kmh > cur["wind_max"]:
            cur["wind_max"], cur["wind_dir"] = kmh, wdir or None
        if wave and (cur["wave_m"] is None or float(wave) > cur["wave_m"]):
            cur["wave_m"] = round(float(wave), 1)
    if not days:
        raise ValueError("no daytime forecast points")
    return days, title


# ---- Windfinder: today and tomorrow, every 3 hours, in the free forecast page (m/s) ----
def windfinder(kw, tz):
    """Each day's strongest daytime wind and gust (6am to 6pm), with direction and biggest wave."""
    r = get(f"https://www.windfinder.com/forecast/{kw}")
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    today = datetime.now(tz).date()
    days = {}
    for block in soup.select(".fc-day"):
        head = block.select_one(".fc-day-headline")
        try:
            d = datetime.strptime(f"{clean(head.get_text())} {today.year}", "%A, %b %d %Y").date()
            if d < today - timedelta(days=180):
                d = d.replace(year=today.year + 1)
        except (AttributeError, ValueError):
            continue
        for cell in block.select('[class*="_cell-timespan_"]'):
            row = cell.find_parent()
            m = re.match(r"(\d+)h", clean(cell.get_text()))
            if not m or not 6 <= int(m.group(1)) <= 18:
                continue
            ws = row.select_one('[class*="_cell-wind-speeds_"]')
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", ws.get_text())] if ws else []
            if not nums:
                continue
            cur = days.setdefault(d.isoformat(), {"wind_max": 0, "gust_max": 0, "wind_dir": None, "wave_m": None})
            wind = round(nums[0] * 3.6)
            if wind > cur["wind_max"]:
                arrow = row.select_one('[class*="_cell-direction_"] img[alt]')
                deg = num(re.sub(r"[^\d.]", "", arrow["alt"])) if arrow else None
                cur["wind_max"], cur["wind_dir"] = wind, compass(deg)  # the angle is where the wind comes from
            if len(nums) > 1:
                cur["gust_max"] = max(cur["gust_max"], round(nums[1] * 3.6))
            wv = row.select_one('[class*="_cell-wave-height_"]')
            h = re.search(r"\d+(?:\.\d+)?", wv.get_text()) if wv else None
            if h and (cur["wave_m"] is None or float(h.group()) > cur["wave_m"]):
                cur["wave_m"] = float(h.group())
    if not days:
        raise ValueError("no forecast hours in page")
    return days


def scrape_marine(loc):
    """Boating figures for each body of water at the location (its "waters" in locations.json, e.g. the
    Maroochy River and the open ocean at Maroochydore): tide times at that water's WillyWeather spot, swell
    for open ocean only, and Windy's wind at the spot. Places with no waters (Oxley, Maleny) are skipped."""
    waters = loc.get("waters")
    if not waters:
        return None
    tz = ZoneInfo(loc["tz"])
    out, problems = [], []
    sb = sb_place = None  # Seabreeze forecasts by town, so one forecast serves every water here
    if loc.get("seabreeze"):
        try:
            sb, sb_place = seabreeze(loc["seabreeze"], tz)
        except Exception as e:
            problems.append(f"Seabreeze: {masked(e)}")
    for w in waters:
        r = {"name": w["name"], "kind": w["kind"], "spot": w["spot"]}
        if w["kind"] != "lake":
            try:
                r["tides"], place = ww_tides(w["ww"])
                if place:
                    r["tides_place"] = place
            except Exception as e:
                problems.append(f"{w['name']} tides: {masked(e)}")
        if w["kind"] == "ocean":
            try:
                r["swell"], place = ww_swell(w["ww"], tz)
                if place:
                    r["swell_place"] = place  # e.g. a nearby reef or point where the spot has no swell forecast
            except Exception as e:
                problems.append(f"{w['name']} swell: {masked(e)}")
        try:
            days = scrape_windy({**loc, "lat": w["lat"], "lon": w["lon"]})["days"]
            r["windy"] = {d["date"]: {k: d[k] for k in ("wind_max", "gust_max", "wind_dir")}
                          for d in days if d.get("wind_max") is not None}
        except Exception as e:
            problems.append(f"{w['name']} Windy: {masked(e)}")
        if sb:
            r["seabreeze"], r["seabreeze_place"] = sb, sb_place
        if w.get("windfinder"):
            try:
                r["windfinder"] = windfinder(w["windfinder"]["kw"], tz)
                r["windfinder_place"] = w["windfinder"]["name"]
            except Exception as e:
                problems.append(f"{w['name']} Windfinder: {masked(e)}")
        out.append(r)
    if not any(x.get("tides") or x.get("swell") or x.get("windy") or x.get("seabreeze") or x.get("windfinder") for x in out):
        raise ValueError("no marine figures: " + "; ".join(problems))
    res = {"source": "willyweather, windy, seabreeze, windfinder", "waters": out}
    if problems:
        res["problems"] = problems
    return res


SCRAPERS = [scrape_bom, scrape_willyweather, scrape_weatherzone, scrape_windy, scrape_ecmwf, scrape_icon, scrape_foreca]
EXTRAS = [scrape_observations, scrape_marine]  # written as top-level "observations" and "marine"


def masked(e):
    return f"{type(e).__name__}: {e}"[:300]


def scrape_location(loc):
    out = os.path.join(DATA, f"{loc['id']}.json")
    try:
        prev = json.load(open(out, encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    old = {s["source"]: s for s in prev.get("sources", [])}
    sources, errors, ran = [], {}, 0
    result = {}
    for fn in EXTRAS:
        key = fn.__name__.replace("scrape_", "")
        try:
            r = fn(loc)
            if r is None:
                continue  # doesn't apply to this location
            ran += 1
            result[key] = {**r, "fetched": now_iso()}
            print(f"  {key}: ok")
        except Exception as e:
            ran += 1
            errors[key] = masked(e)
            traceback.print_exc()
            if prev.get(key):
                result[key] = prev[key]
            print(f"  {key}: FAILED ({errors[key]})", file=sys.stderr)
    for fn in SCRAPERS:
        key = fn.__name__.replace("scrape_", "")
        ran += 1
        try:
            s = fn(loc)
            s["fetched"] = now_iso()
            sources.append(s)
            print(f"  {key}: {len(s['days'])} days")
        except Exception as e:
            errors[key] = masked(e)
            traceback.print_exc()
            # Keep the last good entry, unless it is itself bad (e.g. saved before a unit fix)
            if key in old and all(-10 <= d.get("tmax", 0) <= 50 for d in old[key].get("days", [])
                                  if isinstance(d.get("tmax"), (int, float))):
                sources.append(old[key])
            print(f"  {key}: FAILED ({errors[key]})", file=sys.stderr)
    os.makedirs(DATA, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"id": loc["id"], "generated": now_iso(), "sources": sources, **result, "errors": errors},
                  f, indent=1, ensure_ascii=False)
        f.write("\n")
    return len(errors) < ran


def last_updated(loc):
    try:
        g = json.load(open(os.path.join(DATA, f"{loc['id']}.json"), encoding="utf-8"))["generated"]
        return datetime.fromisoformat(g.replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return datetime.min.replace(tzinfo=timezone.utc)


def pick_due(locs, updated, now):
    """The locations this run updates, oldest first: its share of those due, plus any overdue.
    updated maps location id to its last update time."""
    due = sorted((l for l in locs if updated[l["id"]] <= now - timedelta(minutes=DUE_MINUTES)),
                 key=lambda l: updated[l["id"]])
    overdue = sum(updated[l["id"]] <= now - timedelta(minutes=OVERDUE_MINUTES) for l in due)
    return due[:max(math.ceil(len(locs) / RUNS_PER_CYCLE), overdue)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="update every location now")
    ap.add_argument("--only", help="update just this location id")
    args = ap.parse_args()
    locs = json.load(open(LOCATIONS, encoding="utf-8"))
    if args.only:
        todo = [l for l in locs if l["id"] == args.only]
    elif args.all:
        todo = locs
    else:
        todo = pick_due(locs, {l["id"]: last_updated(l) for l in locs}, datetime.now(timezone.utc))
    if not todo:
        print("Nothing due")
        return 0
    ok, start = 0, time.monotonic()
    for i, loc in enumerate(todo):
        if time.monotonic() - start > TIME_BUDGET_MINUTES * 60:
            print(f"Time budget used; {len(todo) - i} left for the next run")
            break
        print(f"{loc['id']}:")
        ok += scrape_location(loc)
    # Fail the step only if every location came back empty, so partial results still get committed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
