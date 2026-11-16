# GitHub Gmail Finder

A web app that searches GitHub users by **name** and **area**. It collects the
**Gmail** addresses those users have made public on their profiles, together
with their avatars, and saves them to a database.

Enter a name and an area, then click **Estimate** to see how many GitHub users
match and how long fetching will take. Then click **Fetch**. The backend runs
the same search you would type into GitHub's search box:

```
ABC in:name location:XYZ type:user
```

It then checks **every** matching profile. Progress and results appear in the
browser while it runs.

## Features

- Name and Area inputs with **Estimate**, **Fetch** and **Stop** buttons
- **Estimate first.** Clicking Estimate counts the matching users and estimates how long fetching will take, without fetching any addresses. **Fetch** stays disabled until you estimate, and it locks again if you change Name or Area or when a fetch finishes successfully. After **Stop** or an error you can click Fetch again
- **Fetches every result.** GitHub's search returns at most 1,000 users, so larger searches are split by account creation date and combined (see [How "fetch all" works](#how-fetch-all-works))
- **Waits through rate limits.** If GitHub's limit is reached, the app pauses, shows a countdown, and continues on its own
- Live progress bar with the number of profiles checked, the number of Gmail addresses found, and the estimated time left
- Keeps only `@gmail.com` and `@googlemail.com` addresses; all other emails are ignored
- Results table with each user's **GitHub avatar**, name, profile link, area and Gmail address, **20 rows per page**
- Export to **CSV**, **TXT** (one address per line) or **XLSX** (Excel)
- First-run setup screen for the **GitHub token**, so anyone who clones the project can paste their token in the browser
- Saves results in SQLite, so they're still there after a restart and repeated searches don't create duplicates
- Light and dark mode, and works on mobile
- Needs nothing installed beyond Python 3.10 or newer (standard library only, including the XLSX export)

## Quick start

```bash
git clone <this-repo-url> gmail-scrapper
cd gmail-scrapper
python3 run.py          # Windows: py run.py
```

The browser opens at <http://127.0.0.1:8765/>. If no token is saved yet, the
page asks for one:

1. Open <https://github.com/settings/personal-access-tokens/new>.
2. Enter any name and choose an expiration. Under repository access, select **Public repositories (read-only)**. No extra permissions are needed.
3. Click **Generate token**, then paste the token into the app and click **Save token**.

The app checks the token with GitHub and saves it to `.env` in the project
folder. `.env` is listed in `.gitignore`, so the token is never committed. To
use a different token later, click **Change token**.

Advanced users can set the `GITHUB_TOKEN` environment variable instead of
using the page.

## Project structure

```
gmail-scrapper/
├── run.py               # Starts the web app
├── backend/
│   ├── finder.py        # GitHub API client, search splitting, rate-limit handling, Gmail filter, SQLite store
│   ├── exporters.py     # CSV / TXT / XLSX export (XLSX written with the standard library)
│   └── server.py        # HTTP server: serves the frontend and the REST/SSE API, saves the token
├── frontend/
│   ├── index.html       # UI layout
│   ├── style.css        # Styling
│   └── app.js           # UI logic: token setup, live progress, results table
├── tests/               # Unit and API tests (no network needed)
├── .env                 # Your GitHub token (created by the app; never commit it)
└── data/results.db      # Saved results (created automatically)
```

## Architecture

```
 Browser (frontend)                  Python backend                      GitHub REST API
 ──────────────────                  ──────────────                      ───────────────
 [Token] [Save] ──POST /api/token──▶ validate ─────────────────────────▶ /rate_limit
                                     save to .env
 [Estimate] ──GET /api/estimate──▶ count only ─────────────────────────▶ /search/users (1 request)
 [Name] [Area] [Fetch] ─GET /api/fetch─▶ server.py ──▶ finder.py ──────▶ /search/users?q=...
                                            │              │              (split by created: date
   progress bar  ◀──── SSE events ──────────┘              │               if > 1000 results)
   results table ◀──── GET /api/results ◀── SQLite ◀───────┴───────────▶ /users/{login}
   CSV / TXT / XLSX ◀── GET /api/results.{csv,txt,xlsx}                    (public email, avatar)
```

The browser receives progress through **Server-Sent Events** (SSE). The backend
sends an event after it checks each profile, so the progress bar and the table
update in real time.

### How "fetch all" works

GitHub's search API returns at most 1,000 results for any query, and the website
has the same limit. When a search matches more than that, the backend:

1. Adds an account-creation date range to the query, for example `created:2007-10-01..2026-11-16`.
2. Splits any range that still has more than 1,000 results in half, again and again, until every range fits under the limit.
3. Goes through each range page by page (100 users per page) and skips users it has already seen.

A test against the live API shows how this works. `John in:name location:UK type:user`
reports 1,046 users. A plain search can only reach 1,000 of them; with splitting,
all 1,046 are reached.

The app checks about 5,000 profiles per hour at most, because that is GitHub's
limit for a token. Very large searches therefore take time. When the limit is
reached, the app waits until GitHub resets it, then continues automatically.
Keep the browser tab open; every address found is saved as soon as it's found.

### API

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Report whether a GitHub token is saved: `{"token": true/false}` |
| POST | `/api/token` | Check a token with GitHub and save it to `.env`. Body: `{"token": "..."}` |
| GET | `/api/estimate?name=&location=` | Count matching users and estimate the fetch time, without fetching anything. Returns `{"query", "total", "seconds"}` |
| GET | `/api/fetch?name=&location=` | Run a search. Streams `start`, `total`, `info`, `wait`, `checked`, `found`, `done` and `fail` events |
| POST | `/api/stop` | Stop the running fetch. Body: `{"run": <id from the start event>}` |
| GET | `/api/results` | List all saved records as JSON |
| GET | `/api/results.csv` / `.txt` / `.xlsx` | Download all saved records |
| DELETE | `/api/results` | Delete all saved records |

## Command-line use (optional)

```bash
GITHUB_TOKEN=github_pat_... python3 -m backend.finder --name "John" --location "Tokyo"
```

## Tests

```bash
python3 -m unittest -v
```

There are 42 tests. They cover building the search query, the Gmail filter,
the estimate and its time calculation, paging, search splitting, waiting
through rate limits, stopping a fetch, the database upgrade, the three export
formats, token setup, and every API endpoint. A fake GitHub
client stands in for the real one, so the tests don't use the network.

## Responsible use

- **Only public emails are collected.** If a user hides their email, the app saves nothing for them. It doesn't try to recover hidden addresses from commits or activity.
- **GitHub's Acceptable Use Policy** forbids using data taken from GitHub to send unsolicited email or to sell users' personal information.
- **The results are personal data.** Privacy laws such as Japan's APPI and the EU's GDPR may apply. Keep `data/results.db` and any exported files private, and use **Clear all** when you're finished.
