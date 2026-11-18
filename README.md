# GitHub Gmail Finder

A web app that searches GitHub users by **area**. It collects the **Gmail**
addresses those users have made public on their profiles, together with their
avatars, and saves them to a database.

> This is the **`develop`** branch: an area-only version with no Name field and
> no Estimate step. The `main` branch has the Name + Area + Estimate version.

Enter an area and click **Fetch**. The backend runs the same search you would
type into GitHub's search box:

```
location:XYZ type:user
```

It then checks **every** matching profile. A status line and the results table
update in the browser while it runs. Click **Stop** at any time; the next
**Fetch** skips every profile that was already checked and continues with the rest.

## Features

- One **Area** input with **Fetch** and **Stop** buttons
- **Skips profiles already checked.** Every profile the app opens is remembered (Gmail or not), so a fetch that was stopped resumes where it left off, and a later fetch of any area skips people already checked. **Forget checked history** re-checks everyone on the next fetch; saved addresses are kept
- **Fetches every result.** GitHub's search returns at most 1,000 users, so larger searches are split by account creation date and combined (see [How "fetch all" works](#how-fetch-all-works))
- **Waits through rate limits.** If GitHub's limit is reached, the app pauses, shows a countdown, and continues on its own
- One status line with profiles checked, Gmail addresses found, profiles skipped, and the estimated time left
- Keeps only `@gmail.com` and `@googlemail.com` addresses; all other emails are ignored
- Results table with each user's **GitHub avatar**, name, profile link, area and Gmail address, **20 rows per page**
- Export to **CSV**, **TXT** (one address per line) or **XLSX** (Excel)
- **Send emails** from your own Gmail account to the stored addresses you tick (only people who opted in) — with a title, content, a time gap between emails, a daily limit, automatic resume, and permanent de-duplication, and a **block list** of addresses that are never emailed (see [Sending emails](#sending-emails))
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
│   ├── mailer.py       # Gmail SMTP sending, daily cap, permanent de-duplication
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
 [Area] [Fetch] ──GET /api/fetch──▶ server.py ──▶ finder.py ──────────▶ /search/users?q=...
                                            │              │              (split by created: date
   status line   ◀──── SSE events ──────────┘              │               if > 1000 results)
   results table ◀──── GET /api/results ◀── SQLite ◀───────┴───────────▶ /users/{login}
   CSV / TXT / XLSX ◀── GET /api/results.{csv,txt,xlsx}                    (public email, avatar)
```

The browser receives progress through **Server-Sent Events** (SSE). The backend
sends an event after it checks each profile, so the status line and the table
update in real time.

### Skipping profiles that were already checked

Area searches are large (when tested: Shibuya 804 users, Osaka 5,871, Tokyo
60,340, Japan 105,521), so a fetch may be stopped and resumed many times. The
database keeps a `checked` table with every GitHub profile the app has opened.
On each fetch, profiles in that table are skipped without any request to
GitHub, so resuming costs only the search pages (one request per 100 users).
A profile is recorded only after it has been fully checked, so stopping never
loses or double-counts one.

The trade-off: someone who had no Gmail when they were checked, and adds one
later, is not picked up again until you click **Forget checked history**.

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
| GET | `/api/status` | Whether a GitHub token is saved and how many profiles were already checked: `{"token", "checked"}` |
| POST | `/api/token` | Check a token with GitHub and save it to `.env`. Body: `{"token": "..."}` |
| GET | `/api/fetch?location=` | Run an area search, skipping profiles already checked. Streams `start`, `total`, `info`, `wait`, `skipped`, `checked`, `found`, `done` and `fail` events. (`name=` is still accepted.) |
| GET | `/api/estimate?name=&location=` | Not used by this branch's page; still available. Counts matching users and estimates the fetch time |
| POST | `/api/stop` | Stop the running fetch. Body: `{"run": <id from the start event>}` |
| GET | `/api/results` | List all saved records as JSON |
| GET | `/api/results.csv` / `.txt` / `.xlsx` | Download all saved records |
| DELETE | `/api/results` | Delete all saved records |
| GET | `/api/gmail/status` | Gmail connection state and sent counts (never the password) |
| POST | `/api/gmail` | Verify Gmail address + App Password and save them to `.env` |
| POST | `/api/send/start` | Validate a send and queue it. Body: `{recipients[], subject, body, gap, cap, add_unsub}` -> `{job}` |
| GET | `/api/send/stream?job=` | Run the send. Streams `start`, `sent`, `skipped`, `error`, `limit`, `done`, `fail` events |
| POST | `/api/send/stop` | Stop the running send. Body: `{job}` |
| DELETE | `/api/sent` | Reset the "already sent" history |
| GET | `/api/blocked` | List the block list: `[{email, added_at}]` |
| POST | `/api/blocked` | Add to the block list. Body: `{"emails": "a@gmail.com, b@example.com"}` (commas, spaces or new lines) |
| DELETE | `/api/blocked?email=` | Remove one address from the block list |
| DELETE | `/api/checked` | Forget which GitHub profiles were already checked |

## Command-line use (optional)

```bash
GITHUB_TOKEN=github_pat_... python3 -m backend.finder --location "Tokyo"
```

## Tests

```bash
python3 -m unittest -v
```

There are 82 tests. They cover building the search query (including
area-only), the Gmail filter, the estimate and its time calculation, paging,
search splitting, waiting through rate limits, stopping a fetch, skipping and
forgetting already-checked profiles, resuming a stopped fetch, the database upgrade, the three export formats, token
setup, Gmail credential checks, sending with de-duplication and the daily cap,
stop/resume, the block list, and every API endpoint. A fake GitHub
client stands in for the real one, so the tests don't use the network.

## Sending emails

The **Send emails** card lets you email a list of people **from your own Gmail
account**. Use it only for recipients who agreed to hear from you — never the
addresses collected from GitHub, which would be spam and would break GitHub's
and Gmail's rules.

**Setup (once):** connect your Gmail account in the card. You need a Gmail
**App Password**:

1. Turn on **2-Step Verification** at <https://myaccount.google.com/security>.
2. Create an App Password at <https://myaccount.google.com/apppasswords> and copy the 16 characters.
3. Enter your Gmail address, that App Password, and a sender name, then click **Connect**.

The app checks the credentials with Gmail and saves them to `.env` (gitignored,
never uploaded), so you only do this once until you change accounts.

**Each send:**

1. In the **Send** tab, tick the people who agreed to hear from you in **Stored Gmail addresses**. Already-sent rows can't be ticked; **Only not sent yet** hides them and **Select all** ticks every address not sent yet.
2. Enter a **Title** and **Content**.
3. Set the **Time gap** between emails and the **Daily limit** (default 200, maximum 500).
4. Optionally tick **Add unsubscribe line** to append a sender identity and opt-out note to each email.
5. Click **Send**. Progress shows live, and **Stop** halts it at any time.

**Safety rules built in:**

- **Permanent de-duplication.** Every address ever emailed is remembered and is never emailed again, on any later run. This also means that if you Stop partway and click Send again, it automatically continues with the addresses it hasn't reached yet. (A **Reset sent history** is available via `DELETE /api/sent` if you ever need to start over.)
- **Daily limit counted across restarts.** The count is stored per calendar day, so closing the app cannot reset it and let you exceed the limit.
- **The App Password can send mail as you.** It is kept only on your computer and never committed. Treat it like a password.
- A per-recipient failure (a bounced or mistyped address) is skipped and logged; it does not stop the batch.
- **Block list.** In the **Block list** card at the bottom of the Send tab, type or paste addresses (any domain, separated by commas, spaces or new lines) and click **Add to block list**. Blocked addresses show as **Blocked** in the table and can't be ticked. The server also checks the list right before every email, so a blocked address is never sent to, even if it was added while a send is running. Click **Remove** next to an address to unblock it. The list is stored in `data/results.db` and is not affected by **Reset sent history**.

## Responsible use

- **Only public emails are collected.** If a user hides their email, the app saves nothing for them. It doesn't try to recover hidden addresses from commits or activity.
- **GitHub's Acceptable Use Policy** forbids using data taken from GitHub to send unsolicited email or to sell users' personal information.
- **The results are personal data.** Privacy laws such as Japan's APPI and the EU's GDPR may apply. Keep `data/results.db` and any exported files private, and delete `data/results.db` when you're finished.
