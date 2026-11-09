# Consolidated Weather

A weather app for boating and jet skiing in Queensland, with any place in Australia searchable.

**Site:** https://deskjet808.github.io/consolidated-weather

## What it shows

- **Featured locations** (the dropdown, grouped by region): 33 beaches, bays and jet ski lakes, plus a
  few others. For each one, forecasts from seven sources side by side: the Bureau of Meteorology,
  Weatherzone, WillyWeather, Windy, Foreca, and the ECMWF and ICON models (weather data by [Open-Meteo.com](https://open-meteo.com), CC BY 4.0).
- **Any other place:** search a suburb, town or postcode to see the Bureau of Meteorology forecast and
  the nearest weather station's latest reading. Tap "Save place" to keep it in "My places" on that
  device.
- **Hour by hour:** the whole week as one long strip you drag, swipe or scroll through, shown as a
  chart or as hourly cards (the choice is remembered). Each hour is the median of every source with
  hourly figures, with the spread between sources shown. Days alternate light and dark blue, day
  buttons jump to a day, and the rest of the page follows the day you scroll to.
- **Tides** for each river, bay and stretch of open ocean at a featured location: a one-line summary,
  whether the tide is rising or falling now (updated every minute), the next high and low, a curve of
  the chosen day (tap or hover to read any time, swipe to change day), a "good time to launch" hint
  (up to 90 minutes either side of a daylight high tide, with the Bureau's wind on that water) and the
  next 3 days of tides (7 on request), each with a boat coloured calm, choppy or rough that explains
  itself when hovered or tapped. The page
  remembers which water you last looked at. Times and heights are WillyWeather's; the
  curve between them is an estimate.
- **Boating conditions** for the chosen day, with a separate report for each body of water at a
  featured location (for example, Maroochydore has the Maroochy River and the open ocean). Each
  report has a calm / choppy / rough guide from the strongest daytime wind, the Bureau's hourly wind
  and gusts and Windy's wind for a point on that water, Seabreeze's wind and waves for the town,
  Windfinder's wind, gusts and waves at its nearest spot (today and tomorrow), and tide times at a
  WillyWeather spot on that water. Open-ocean reports add swell; lakes have wind only. Any Bureau warnings for the area show
  above the reports. The waters and their spots are listed under `waters` in `locations.json`.

## How it works

- `index.html` is the whole app, served by GitHub Pages.
- `locations.json` lists the featured locations and each weather site's page for them.
- `scraper/scrape.py` collects the figures from the weather sites into `data/<location>.json`.
  GitHub Actions (`.github/workflows/scrape.yml`) runs it every 30 minutes; each run updates at most a
  sixth of the locations (the oldest that are due), so every location refreshes about every 3 hours
  without hitting the sites all at once. After a run that updates everything, they spread back out
  within a cycle.
- Searched places, hourly wind and warnings come straight from the Bureau of Meteorology in the
  browser, so they need no scraping.

To add a featured location, add it to `locations.json`; the next run fills in its data.

## Refresh now

The "Refresh now" button under Forecast sites runs the scraper for just the location on screen, and the app
shows the new figures when they're in (about 3 minutes). Choosing a location whose figures are over an hour
old starts one automatically. The button needs a small relay, because starting a GitHub Actions run needs a
token that can't go in a public page. `relay/worker.js` is that relay, for Cloudflare Workers (free plan). It
won't start a run if the location's figures are under 30 minutes old, one is already running for it, or 3
are running.

To set it up:

1. On GitHub, create a fine-grained personal access token for `deskjet808/consolidated-weather` only, with
   **Actions: Read and write** and **Contents: Read-only**.
2. On Cloudflare, create a Worker (Workers & Pages, Create, Start with Hello World), name it
   `aus-forecast-refresh`, deploy it, then Edit code, replace the code with `relay/worker.js` and deploy.
3. In the Worker's Settings, Variables and Secrets, add a **Secret** named `GITHUB_TOKEN` holding the token.
4. Put the Worker's address (for example `https://aus-forecast-refresh.<name>.workers.dev`) in `RELAY` near
   the top of the Refresh now code in `index.html`. The button stays hidden while `RELAY` is empty.

Figures belong to the sites they come from. For warnings and safety decisions, follow the Bureau of
Meteorology.
