# Maximus Backyard Forecast

A 10-day forecast that scores each day for backyard time. The dog is a German Shepherd who will spend all day outside watching birds and rabbits when it is cool and dry. He dislikes rain, and on dry days he dislikes above-normal humidity.

It refreshes itself every 3 hours on GitHub and publishes as a small web page.

## Where the data comes from

| Days | Source | Why |
|---|---|---|
| 1 to 7 | [National Weather Service API](https://www.weather.gov/documentation/services-web-api) | The official US government forecast. It only goes out 7 days. |
| 8 to 10 | [Open-Meteo](https://open-meteo.com/) | Serves the NOAA and European (ECMWF) computer models. Less reliable this far out. |
| "Normal" humidity | Open-Meteo historical archive (ERA5 reanalysis), past 20 years | Lets the page say whether a day is muggier or drier than usual for that date. |

No Wikipedia or user-edited sources are used.

## How the score works

Only daylight hours from 7 AM until 30 minutes before sunset count (he comes in before dark). Sunset is calculated with the NOAA solar equations and shown rounded to 15 minutes so it does not hint at an exact location.

- **Rain, 40%**: 100 minus the highest hourly rain chance.
- **Heat, 35%**: hottest daylight "feels like" (NWS heat index). 65°F or cooler = 100, 95°F or hotter = 0.
- **Humidity, 25%**: day's average dew point vs normal for that date. Normal = 50, 8°F drier = 100, 8°F muggier = 0.

Great is 75+, Good 60 to 74, Fair 45 to 59, Rough below 45. Each day also shows **nice hours** (daylight hours with a feels-like of 80°F or cooler and under a 30% rain chance) and the **best 4-hour stretch of the day**.

The **afternoon check** (1 PM until he comes in) compares the hottest the afternoon will feel with a typical July afternoon at the same spot, averaged over the same 20 years. Labels: 80°F or cooler is an easy afternoon, 81 to 90°F means keep an eye on him, above 90°F means short visits only.

These rules are a rule of thumb, not a veterinary standard. The numbers live at the top of `scripts/build_forecast.py` if you want to tune them.

## Privacy

- The location is stored only as two **GitHub repository secrets** (`LAT` and `LON`). Secrets are encrypted, never shown in the code, and GitHub hides them in logs.
- The published page and `data.json` contain no coordinates, city, zip code, weather office, or names of people.
- The weather itself (temperatures, humidity) hints at a general climate, but nothing on the page pins down a place.
- The page has a `noindex` tag so search engines are asked not to list it. Anyone with the link can still view it.

## One-time setup

1. **Create a new public repository** on GitHub (for example `backyard-forecast`) and upload everything in this folder, including the hidden `.github` folder. Free GitHub Pages needs a public repository. Nothing private is in the files.
2. **Add the location as secrets.** In the repo: Settings > Secrets and variables > Actions > New repository secret.
   - Name `LAT`, value: your latitude (two decimal places is plenty, for example `35.00`)
   - Name `LON`, value: your longitude, negative for the western hemisphere (for example `-80.00`)
3. **Turn on Pages.** Settings > Pages > Build and deployment > Source: **GitHub Actions**.
4. **Run it once.** Actions tab > "Refresh forecast" > Run workflow. The first run takes a minute or two longer because it downloads 20 years of history for the normals.
5. The page address appears in Settings > Pages (it looks like `https://<username>.github.io/<repo-name>/`). Share that link.

## Good to know

- **Manual refresh:** Actions > Refresh forecast > Run workflow.
- **GitHub pauses scheduled runs** in a public repo after 60 days with no commits. GitHub emails a warning first. To restart, open the Actions tab and re-enable the workflow, or make any small commit.
- If a weather service is down during a refresh, the page says so in a note at the top. If both are down, the previous version of the page stays up.
- To change the dog's name, edit `DOG_NAME` near the top of `scripts/build_forecast.py`.

## Running it locally (optional)

```
LAT=35.00 LON=-80.00 python3 scripts/build_forecast.py
cd site && python3 -m http.server 8000
```
Then open http://localhost:8000. Python 3.9 or newer, no extra packages.
