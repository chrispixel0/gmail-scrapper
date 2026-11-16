"""
Core logic: search GitHub users by name and area, read each profile's
public email, and keep only Gmail addresses.

Only the ``email`` field a user has chosen to make public is used. Hidden
emails are never inferred from commits or other activity.

GitHub returns at most 1000 results per search. To fetch *all* matching
users, a search with more results is split into smaller searches by account
creation date (``created:A..B``) until every piece fits under that cap.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

API_ROOT = "https://api.github.com"
USER_AGENT = "github-gmail-finder-student-project"
SEARCH_PAGE_SIZE = 100                  # max allowed by the API
SEARCH_RESULT_CAP = 1000                # GitHub search never returns more than this
GITHUB_EPOCH = datetime(2007, 10, 1, tzinfo=timezone.utc)   # before the first GitHub account
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
SHORT_WAIT = 3                          # rate-limit waits up to this are slept silently
WAIT_TICK = 5                           # seconds between "wait" progress events


# --------------------------------------------------------------------------
# Query building / filtering
# --------------------------------------------------------------------------

def _quote(term: str) -> str:
    """Wrap a search term in quotes if it contains whitespace."""
    term = term.strip().replace('"', "")
    return f'"{term}"' if any(ch.isspace() for ch in term) else term


def build_query(name: str, location: str | None = None) -> str:
    """Build a GitHub user-search query, e.g. 'ABC in:name location:XYZ type:user'."""
    if not name or not name.strip():
        raise ValueError("Please enter a name.")
    parts = [_quote(name), "in:name"]
    if location and location.strip():
        parts.append(f"location:{_quote(location)}")
    parts.append("type:user")
    return " ".join(parts)


def is_gmail(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[1].strip().lower() in GMAIL_DOMAINS


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# GitHub API client
# --------------------------------------------------------------------------

class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, code: str = "api_error"):
        super().__init__(message)
        self.code = code


class RateLimited(Exception):
    """Raised when GitHub asks us to wait longer than SHORT_WAIT seconds."""

    def __init__(self, wait: float):
        super().__init__(f"rate limited for {wait:.0f}s")
        self.wait = wait


class GitHubClient:
    def __init__(self, token: str | None = None, max_retries: int = 3):
        self.token = token
        self.max_retries = max_retries

    def _request(self, path: str, params: dict | None = None) -> dict:
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code in (403, 429):
                    wait = self._rate_limit_wait(e.headers, body)
                    if wait is not None:
                        if wait > SHORT_WAIT:
                            raise RateLimited(wait) from e
                        time.sleep(wait)
                        continue
                if e.code >= 500 and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise self._error(e.code, body) from e
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubAPIError(f"Could not reach GitHub: {e.reason}", "network") from e
        raise GitHubAPIError("GitHub did not respond after several retries.", "network")

    @staticmethod
    def _rate_limit_wait(headers, body: str) -> float | None:
        """Seconds to wait if the response is a rate-limit response, else None."""
        retry_after = headers.get("Retry-After")
        if retry_after:
            return float(retry_after) + 1
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset")
            if reset:
                return max(0.0, int(reset) - time.time()) + 1
        if "rate limit" in body.lower():          # secondary rate limit without headers
            return 60.0
        return None

    @staticmethod
    def _error(status: int, body: str) -> GitHubAPIError:
        if status == 401:
            return GitHubAPIError("GitHub rejected the token. Please enter a valid token.", "bad_token")
        try:
            detail = json.loads(body).get("message", body)
        except ValueError:
            detail = body
        return GitHubAPIError(f"GitHub API error {status}: {detail}")

    def search_page(self, query: str, page: int = 1, per_page: int = SEARCH_PAGE_SIZE) -> dict:
        """One page of /search/users, oldest accounts first (stable ordering for paging)."""
        return self._request("/search/users", {
            "q": query, "per_page": per_page, "page": page, "sort": "joined", "order": "asc",
        })

    def get_user(self, login: str) -> dict:
        return self._request(f"/users/{urllib.parse.quote(login)}")


def validate_token(token: str) -> None:
    """Raise GitHubAPIError if GitHub does not accept the token."""
    try:
        GitHubClient(token=token, max_retries=1)._request("/rate_limit")
    except RateLimited:
        pass  # token is valid, just currently rate limited


# --------------------------------------------------------------------------
# Storage (SQLite)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    login       TEXT PRIMARY KEY,
    name        TEXT,
    location    TEXT,
    email       TEXT NOT NULL,
    profile_url TEXT,
    avatar_url  TEXT,
    query       TEXT,
    fetched_at  TEXT NOT NULL
);
"""
COLUMNS = ["login", "name", "location", "email", "profile_url", "avatar_url", "query", "fetched_at"]


@dataclass
class UserRecord:
    login: str
    name: str | None
    location: str | None
    email: str
    profile_url: str | None
    avatar_url: str | None
    query: str
    fetched_at: str


class Store:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute(SCHEMA)
        # Upgrade databases created by older versions of the app.
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        if "avatar_url" not in existing:
            self.conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
        self.conn.commit()

    def upsert(self, rec: UserRecord) -> None:
        placeholders = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "login")
        self.conn.execute(
            f"INSERT INTO users ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(login) DO UPDATE SET {updates}",
            tuple(getattr(rec, c) for c in COLUMNS),
        )
        self.conn.commit()

    def all(self) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM users ORDER BY fetched_at DESC, login"
        ).fetchall()
        return [dict(zip(COLUMNS, r)) for r in rows]

    def clear(self) -> int:
        n = self.conn.execute("DELETE FROM users").rowcount
        self.conn.commit()
        return n

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------
# Fetch workflow (used by both the web server and the CLI)
# --------------------------------------------------------------------------

def _call(fn, *args):
    """
    Call a client method; while GitHub rate-limits us, yield "wait" events
    (so the caller can show a countdown or stop) and retry afterwards.
    Use as:  result = yield from _call(client.get_user, login)
    """
    while True:
        try:
            return fn(*args)
        except RateLimited as e:
            resume_at = time.time() + e.wait
            while (left := resume_at - time.time()) > 0:
                yield {"type": "wait", "seconds": int(left) + 1}
                time.sleep(min(WAIT_TICK, left))


def _iter_pages(client, query: str, first: dict | None = None):
    """Yield {"type": "user"} for every result of one search (up to the 1000 cap)."""
    page, data = 1, first
    while True:
        if data is None:
            data = yield from _call(client.search_page, query, page, SEARCH_PAGE_SIZE)
        items = data.get("items", [])
        for item in items:
            yield {"type": "user", "login": item["login"]}
        if len(items) < SEARCH_PAGE_SIZE or page * SEARCH_PAGE_SIZE >= SEARCH_RESULT_CAP:
            return
        page, data = page + 1, None


def _iter_slices(client, query: str, start: datetime, end: datetime):
    """Split a search by account creation date until each piece fits under the cap."""
    sliced = f"{query} created:{_fmt(start)}..{_fmt(end)}"
    data = yield from _call(client.search_page, sliced, 1, SEARCH_PAGE_SIZE)
    count = int(data.get("total_count", 0))
    if count == 0:
        return
    if count <= SEARCH_RESULT_CAP or end - start <= timedelta(seconds=1):
        yield from _iter_pages(client, sliced, first=data)
        return
    mid = start + (end - start) / 2
    mid = mid.replace(microsecond=0)
    yield from _iter_slices(client, query, start, mid)
    yield from _iter_slices(client, query, mid + timedelta(seconds=1), end)


def iter_fetch(name: str, location: str | None, client, store: Store,
               max_results: int | None = None, delay: float = 0.0) -> Iterator[dict]:
    """
    Search GitHub and yield progress events (dicts with a "type" key):
      start    {"query"}
      total    {"total"}                       number of users GitHub found
      info     {"message"}
      wait     {"seconds"}                     paused by GitHub's rate limit
      checked  {"checked", "total", "login", "reason"}   no Gmail on this profile
      found    {"checked", "total", "record"}            Gmail saved
      done     {"checked", "found", "query"}
    By default every matching user is checked; max_results limits that.
    """
    query = build_query(name, location)
    yield {"type": "start", "query": query}

    first = yield from _call(client.search_page, query, 1, SEARCH_PAGE_SIZE)
    total = int(first.get("total_count", 0))
    if max_results is not None:
        total = min(total, max(1, int(max_results)))
    yield {"type": "total", "total": total}

    fits_in_one_search = int(first.get("total_count", 0)) <= SEARCH_RESULT_CAP
    if fits_in_one_search or (max_results is not None and max_results <= SEARCH_RESULT_CAP):
        users = _iter_pages(client, query, first=first)
    else:
        yield {"type": "info", "message": (
            "GitHub returns at most 1000 results per search, so this search is being "
            "split by account creation date to reach all of them.")}
        users = _iter_slices(client, query, GITHUB_EPOCH, datetime.now(timezone.utc).replace(microsecond=0))

    seen: set[str] = set()
    checked = found = 0
    for ev in users:
        if ev["type"] != "user":
            yield ev
            continue
        login = ev["login"]
        if login in seen:
            continue
        if max_results is not None and checked >= total:
            break
        seen.add(login)
        checked += 1

        profile = yield from _call(client.get_user, login)
        email = (profile.get("email") or "").strip()   # public email only

        if is_gmail(email):
            rec = UserRecord(
                login=login,
                name=profile.get("name"),
                location=profile.get("location"),
                email=email,
                profile_url=profile.get("html_url"),
                avatar_url=profile.get("avatar_url"),
                query=query,
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            store.upsert(rec)
            found += 1
            yield {"type": "found", "checked": checked, "total": total, "record": asdict(rec)}
        else:
            reason = "no public email" if not email else "not a Gmail address"
            yield {"type": "checked", "checked": checked, "total": total,
                   "login": login, "reason": reason}
        if delay:
            time.sleep(delay)

    yield {"type": "done", "checked": checked, "found": found, "query": query}


# --------------------------------------------------------------------------
# Command-line interface (optional; the web UI is the main entry point)
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Find public Gmail addresses on GitHub profiles.")
    p.add_argument("--name", required=True)
    p.add_argument("--location")
    p.add_argument("--max-results", type=int, help="Stop after this many profiles (default: all)")
    p.add_argument("--db", default=os.path.join("data", "results.db"))
    args = p.parse_args(argv)

    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN"))
    store = Store(args.db)
    try:
        for ev in iter_fetch(args.name, args.location, client, store,
                             max_results=args.max_results, delay=0.1):
            t = ev["type"]
            if t == "start":
                print(f"Query: {ev['query']}")
            elif t == "total":
                print(f"GitHub found {ev['total']} user(s).")
            elif t == "info":
                print(ev["message"])
            elif t == "wait":
                print(f"Rate limited, resuming in {ev['seconds']}s ...")
            elif t == "found":
                print(f"[{ev['checked']}/{ev['total']}] {ev['record']['login']:<25} {ev['record']['email']}")
            elif t == "checked":
                print(f"[{ev['checked']}/{ev['total']}] {ev['login']:<25} ({ev['reason']})")
            elif t == "done":
                print(f"Done. Checked {ev['checked']}, saved {ev['found']} Gmail address(es).")
    except (GitHubAPIError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Addresses found so far are saved.")
        return 130
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
