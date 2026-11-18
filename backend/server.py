"""
HTTP backend: serves the frontend and a small JSON / Server-Sent Events API.

  GET    /                          frontend page
  GET    /api/status                {"token": bool}
  POST   /api/token                 {"token": "..."} -> validate with GitHub and save to .env
  GET    /api/estimate?name=&location=  number of matching users + time estimate (no fetching)
  GET    /api/fetch?name=&location= live progress stream (text/event-stream)
  GET    /api/results               all saved records (JSON)
  GET    /api/results.csv|.txt|.xlsx   download saved records
  DELETE /api/results               delete all saved records

  GET    /api/gmail/status          Gmail connection + sent counts (never the password)
  POST   /api/gmail                 {address, app_password, sender_name} -> verify + save to .env
  POST   /api/send/start            {recipients[], subject, body, gap, cap, add_unsub} -> {job}
  GET    /api/send/stream?job=      live send progress (text/event-stream)
  GET    /api/sent                  every address already sent to [{email, sent_at}]
  POST   /api/send/stop             {job} -> stop the running send
  DELETE /api/sent                  reset the "already sent" history
  GET    /api/blocked               block list [{email, added_at}]
  POST   /api/blocked               {emails: "a@x.com, b@y.com"} -> add to the block list
  DELETE /api/blocked?email=        remove one address from the block list
  DELETE /api/checked               forget which GitHub profiles were already checked
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import exporters, finder, mailer

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DEFAULT_DB = ROOT / "data" / "results.db"
DEFAULT_ENV = ROOT / ".env"

# Only these files are served, so no other file on disk is reachable.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
EXPORT_PATH = re.compile(r"^/api/results\.(csv|txt|xlsx)$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]{20,255}$")


# --------------------------------------------------------------------------
# Token storage (.env file in the project folder)
# --------------------------------------------------------------------------

def read_env(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file into a dict."""
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env_values(path: Path, updates: dict[str, str | None]) -> None:
    """Add/replace keys in .env (a value of None removes the key), keeping the rest."""
    values = read_env(path)
    for key, value in updates.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)   # readable only by the current user (no-op on Windows)
    except OSError:
        pass


def read_env_token(path: Path) -> str | None:
    return read_env(path).get("GITHUB_TOKEN") or None


def save_env_token(path: Path, token: str) -> None:
    save_env_values(path, {"GITHUB_TOKEN": token})


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path: str, env_path: Path, token: str | None,
                 client_factory: Callable[[str | None], object],
                 token_validator: Callable[[str], None], delay: float,
                 gmail: dict | None = None,
                 sender_factory: Callable[[str, str, str], object] | None = None,
                 cred_validator: Callable[[str, str], None] | None = None):
        super().__init__(address, Handler)
        self.db_path = db_path
        self.env_path = env_path
        self.token = token
        self.client_factory = client_factory
        self.token_validator = token_validator
        self.delay = delay
        self.fetch_lock = threading.Lock()   # one fetch at a time (protects rate limits)
        self.runs: dict[int, threading.Event] = {}   # fetch id -> "please stop" flag
        self.next_run_id = 1
        self.runs_lock = threading.Lock()

        # Gmail sending.
        self.gmail = gmail or {}             # {"address", "app_password", "sender_name"}
        self.sender_factory = sender_factory or (lambda a, p, n: mailer.GmailSender(a, p, n))
        self.cred_validator = cred_validator or mailer.validate_credentials
        self.send_lock = threading.Lock()    # one send run at a time
        self.send_jobs: dict[int, dict] = {} # job id -> {"cancel", "params"}
        self.next_job_id = 1
        self.jobs_lock = threading.Lock()

    def new_run(self) -> tuple[int, threading.Event]:
        with self.runs_lock:
            run_id, self.next_run_id = self.next_run_id, self.next_run_id + 1
            self.runs[run_id] = threading.Event()
            return run_id, self.runs[run_id]

    def end_run(self, run_id: int) -> None:
        with self.runs_lock:
            self.runs.pop(run_id, None)

    def stop_runs(self, run_id: int | None = None) -> None:
        """Stop one fetch by id, or every running fetch when run_id is None."""
        with self.runs_lock:
            for rid, flag in self.runs.items():
                if run_id is None or rid == run_id:
                    flag.set()

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def has_gmail(self) -> bool:
        return bool(self.gmail.get("address") and self.gmail.get("app_password"))

    # ----- send jobs -----
    def new_send_job(self, params: dict) -> int:
        with self.jobs_lock:
            job_id, self.next_job_id = self.next_job_id, self.next_job_id + 1
            self.send_jobs[job_id] = {"cancel": threading.Event(), "params": params}
            return job_id

    def get_send_job(self, job_id: int) -> dict | None:
        with self.jobs_lock:
            return self.send_jobs.get(job_id)

    def end_send_job(self, job_id: int) -> None:
        with self.jobs_lock:
            self.send_jobs.pop(job_id, None)

    def stop_sends(self, job_id: int | None = None, keep: int | None = None) -> None:
        """Stop one send (job_id), or every send except `keep`."""
        with self.jobs_lock:
            for jid, job in self.send_jobs.items():
                if job_id is not None:
                    if jid == job_id:
                        job["cancel"].set()
                elif jid != keep:
                    job["cancel"].set()


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "GmailFinder/2.0"

    # ---------- helpers ----------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _rows(self) -> list[dict]:
        store = finder.Store(self.server.db_path)
        try:
            return store.all()
        finally:
            store.close()

    # ---------- routes ----------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        export = EXPORT_PATH.match(url.path)
        if url.path in STATIC_FILES:
            filename, ctype = STATIC_FILES[url.path]
            self._send(200, (FRONTEND_DIR / filename).read_bytes(), ctype)
        elif url.path == "/api/status":
            store = finder.Store(self.server.db_path)
            try:
                checked = store.checked_count()
            finally:
                store.close()
            self._json({"token": self.server.has_token, "checked": checked})
        elif url.path == "/api/results":
            self._json(self._rows())
        elif export:
            fmt = export.group(1)
            render, ctype = exporters.FORMATS[fmt]
            self._send(200, render(self._rows()), ctype,
                       {"Content-Disposition": f'attachment; filename="gmail_results.{fmt}"'})
        elif url.path == "/api/estimate":
            self._estimate(parse_qs(url.query))
        elif url.path == "/api/fetch":
            self._fetch(parse_qs(url.query))
        elif url.path == "/api/gmail/status":
            self._json(self._gmail_status())
        elif url.path == "/api/send/stream":
            self._send_stream(parse_qs(url.query))
        elif url.path == "/api/sent":
            store = mailer.SentStore(self.server.db_path)
            try:
                self._json(store.all())
            finally:
                store.close()
        elif url.path == "/api/blocked":
            self._json(self._blocked())
        else:
            self._json({"error": "Not found"}, 404)

    # ---------- block list ----------
    def _blocked(self) -> list[dict]:
        store = mailer.BlockStore(self.server.db_path)
        try:
            return store.all()
        finally:
            store.close()

    def _add_blocked(self, body: dict) -> None:
        raw = body.get("emails", "")
        if isinstance(raw, list):
            raw = "\n".join(r for r in raw if isinstance(r, str))
        if not isinstance(raw, str):
            return self._json({"error": "Invalid request."}, 400)
        valid, invalid = mailer.split_addresses(raw)
        if not valid:
            msg = ("That doesn't look like a valid email address." if invalid
                   else "Please enter at least one email address.")
            return self._json({"error": msg, "invalid": invalid[:20]}, 400)
        store = mailer.BlockStore(self.server.db_path)
        try:
            added = store.add(valid)
            rows = store.all()
        finally:
            store.close()
        self._json({"added": added, "already": len(valid) - added,
                    "invalid": invalid[:20], "blocked": rows})

    # ---------- gmail status (never returns the password) ----------
    def _gmail_status(self) -> dict:
        store = mailer.SentStore(self.server.db_path)
        try:
            from datetime import date
            sent_today = store.count_sent_on(date.today().isoformat())
            sent_total = store.total()
        finally:
            store.close()
        return {
            "configured": self.server.has_gmail,
            "address": self.server.gmail.get("address", ""),
            "sender_name": self.server.gmail.get("sender_name", ""),
            "sent_today": sent_today,
            "sent_total": sent_total,
            "cap_default": mailer.DEFAULT_DAILY_CAP,
            "cap_max": mailer.MAX_DAILY_CAP,
        }

    # ---------- estimate (count only, nothing is fetched or saved) ----------
    def _estimate(self, qs: dict) -> None:
        name = qs.get("name", [""])[0].strip()
        location = qs.get("location", [""])[0].strip()
        if not self.server.has_token:
            return self._json({"error": "Please save your GitHub token first.", "code": "no_token"}, 400)
        if not name:
            return self._json({"error": "Please enter a name."}, 400)
        try:
            client = self.server.client_factory(self.server.token)
            self._json(finder.estimate(name, location, client, delay=self.server.delay))
        except finder.RateLimited as e:
            self._json({"error": f"GitHub's search limit was reached. Please try again in "
                                 f"{int(e.wait) + 1} seconds."}, 429)
        except finder.GitHubAPIError as e:
            self._json({"error": str(e), "code": e.code}, 502)

    # Max JSON body per POST route. /api/send/start carries the recipient list.
    POST_LIMITS = {
        "/api/token": 4096,
        "/api/stop": 4096,
        "/api/gmail": 8192,
        "/api/send/start": 4_000_000,
        "/api/send/stop": 4096,
        "/api/blocked": 1_000_000,
    }

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in self.POST_LIMITS:
            return self._json({"error": "Not found"}, 404)
        # Requiring JSON means other websites can't submit these requests from a browser.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"error": "Expected JSON."}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.POST_LIMITS[path]:
            return self._json({"error": "Request too large."}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "Invalid request."}, 400)

        if path == "/api/stop":
            run = body.get("run")
            self.server.stop_runs(run if isinstance(run, int) else None)
            return self._json({"stopped": True})
        if path == "/api/gmail":
            return self._save_gmail(body)
        if path == "/api/send/start":
            return self._send_start(body)
        if path == "/api/send/stop":
            job = body.get("job")
            self.server.stop_sends(job if isinstance(job, int) else None)
            return self._json({"stopped": True})
        if path == "/api/blocked":
            return self._add_blocked(body)

        # path == "/api/token"
        token = str(body.get("token", "")).strip()
        if not TOKEN_PATTERN.match(token):
            return self._json({"error": "That doesn't look like a GitHub token. "
                                        "It usually starts with github_pat_ or ghp_."}, 400)
        try:
            self.server.token_validator(token)
        except finder.GitHubAPIError as e:
            return self._json({"error": str(e)}, 400)
        save_env_token(self.server.env_path, token)
        self.server.token = token
        self._json({"token": True})

    # ---------- save Gmail credentials ----------
    def _save_gmail(self, body: dict) -> None:
        address = str(body.get("address", "")).strip()
        app_password = str(body.get("app_password", "")).strip().replace(" ", "")
        sender_name = str(body.get("sender_name", "")).strip()[:120]
        if not mailer.is_valid_email(address):
            return self._json({"error": "Please enter a valid Gmail address."}, 400)
        if len(app_password) < 12:
            return self._json({"error": "Please paste the 16-character App Password."}, 400)
        try:
            self.server.cred_validator(address, app_password)
        except mailer.MailError as e:
            return self._json({"error": str(e), "code": e.code}, 400)

        self.server.gmail = {"address": address, "app_password": app_password,
                             "sender_name": sender_name}
        save_env_values(self.server.env_path, {
            "GMAIL_ADDRESS": address,
            "GMAIL_APP_PASSWORD": app_password,
            "GMAIL_SENDER_NAME": sender_name,
        })
        self._json({"configured": True, "address": address, "sender_name": sender_name})

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        path = url.path
        if path == "/api/blocked":
            email = parse_qs(url.query).get("email", [""])[0]
            store = mailer.BlockStore(self.server.db_path)
            try:
                self._json({"deleted": store.remove(email)})
            finally:
                store.close()
        elif path == "/api/results":
            store = finder.Store(self.server.db_path)
            try:
                self._json({"deleted": store.clear()})
            finally:
                store.close()
        elif path == "/api/sent":
            store = mailer.SentStore(self.server.db_path)
            try:
                self._json({"deleted": store.clear()})
            finally:
                store.close()
        elif path == "/api/checked":
            store = finder.Store(self.server.db_path)
            try:
                self._json({"deleted": store.clear_checked()})
            finally:
                store.close()
        else:
            self._json({"error": "Not found"}, 404)

    # ---------- live fetch (Server-Sent Events) ----------
    def _fetch(self, qs: dict) -> None:
        name = qs.get("name", [""])[0].strip()
        location = qs.get("location", [""])[0].strip()
        max_results = None
        if qs.get("max"):
            try:
                max_results = max(1, int(qs["max"][0]))
            except ValueError:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(event: str, data: dict) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
            self.wfile.flush()

        if not self.server.has_token:
            emit("fail", {"message": "Please save your GitHub token first.", "code": "no_token"})
            return
        if not name and not location:
            emit("fail", {"message": "Please enter an area."})
            return
        # A new fetch replaces any earlier one that is still winding down (e.g. after Stop).
        self.server.stop_runs()
        if not self.server.fetch_lock.acquire(timeout=30):
            emit("fail", {"message": "The previous fetch is still stopping. Please try again."})
            return
        run_id, cancelled = self.server.new_run()

        store = finder.Store(self.server.db_path)
        try:
            client = self.server.client_factory(self.server.token)
            for ev in finder.iter_fetch(name, location, client, store,
                                        max_results=max_results, delay=self.server.delay,
                                        skip_checked=True, cancel=cancelled):
                if cancelled.is_set():
                    break           # Stop was pressed, or a newer fetch started
                if ev["type"] == "start":
                    ev = {**ev, "run": run_id}      # lets the browser stop exactly this fetch
                emit(ev["type"], ev)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser pressed Stop or closed the tab; results so far are saved
        except (finder.GitHubAPIError, ValueError) as e:
            try:
                emit("fail", {"message": str(e), "code": getattr(e, "code", "error")})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            store.close()
            self.server.end_run(run_id)
            self.server.fetch_lock.release()

    # ---------- start a send (stores the job, returns its id) ----------
    def _send_start(self, body: dict) -> None:
        if not self.server.has_gmail:
            return self._json({"error": "Please connect your Gmail account first.",
                               "code": "gmail_not_set"}, 400)

        raw = body.get("recipients")
        if not isinstance(raw, list):
            return self._json({"error": "No recipients were selected."}, 400)
        recipients = mailer.parse_recipients("\n".join(r for r in raw if isinstance(r, str)))
        if not recipients:
            return self._json({"error": "Please tick at least one address to send to."}, 400)
        if len(recipients) > 20000:
            return self._json({"error": "That list is too large (over 20,000 addresses)."}, 400)

        subject = str(body.get("subject", "")).strip()
        content = str(body.get("body", ""))
        if not subject:
            return self._json({"error": "Please enter a subject."}, 400)
        if not content.strip():
            return self._json({"error": "Please enter the email content."}, 400)

        try:
            gap = max(0.0, min(3600.0, float(body.get("gap", 30))))
        except (TypeError, ValueError):
            gap = 30.0
        cap = mailer.clamp_cap(body.get("cap"))
        add_unsub = bool(body.get("add_unsub"))

        job_id = self.server.new_send_job({
            "recipients": recipients, "subject": subject, "body": content,
            "gap": gap, "cap": cap, "add_unsub": add_unsub,
        })
        self._json({"job": job_id, "count": len(recipients)})

    # ---------- run a send (Server-Sent Events) ----------
    def _send_stream(self, qs: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(event: str, data: dict) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            job_id = int(qs.get("job", ["0"])[0])
        except ValueError:
            job_id = 0
        job = self.server.get_send_job(job_id)
        if job is None:
            return emit("fail", {"message": "This send has expired. Please click Send again."})

        self.server.stop_sends(keep=job_id)          # replace any send still winding down
        if not self.server.send_lock.acquire(timeout=30):
            return emit("fail", {"message": "A previous send is still stopping. Please try again."})

        params, cancel = job["params"], job["cancel"]
        g = self.server.gmail
        sender = self.server.sender_factory(g["address"], g["app_password"], g.get("sender_name", ""))
        store = mailer.SentStore(self.server.db_path)
        blocklist = mailer.BlockStore(self.server.db_path)
        try:
            for ev in mailer.iter_send(sender=sender, store=store, cancel=cancel,
                                       blocklist=blocklist, **params):
                if ev["type"] == "start":
                    ev = {**ev, "job": job_id}
                emit(ev["type"], ev)
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # tab closed; what was sent is recorded
        except mailer.MailError as e:
            try:
                emit("fail", {"message": str(e), "code": e.code})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            store.close()
            blocklist.close()
            self.server.end_send_job(job_id)
            self.server.send_lock.release()


def make_server(host: str = "127.0.0.1", port: int = 8765, db_path: str | None = None,
                env_path: Path | None = None,
                client_factory: Callable[[str | None], object] | None = None,
                token_validator: Callable[[str], None] | None = None,
                delay: float = 0.1,
                sender_factory: Callable[[str, str, str], object] | None = None,
                cred_validator: Callable[[str, str], None] | None = None) -> AppServer:
    env_path = Path(env_path or DEFAULT_ENV)
    env = read_env(env_path)
    token = os.environ.get("GITHUB_TOKEN") or env.get("GITHUB_TOKEN") or None

    gmail = {}
    address = os.environ.get("GMAIL_ADDRESS") or env.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD") or env.get("GMAIL_APP_PASSWORD")
    if address and password:
        gmail = {"address": address, "app_password": password,
                 "sender_name": os.environ.get("GMAIL_SENDER_NAME") or env.get("GMAIL_SENDER_NAME", "")}

    return AppServer(
        (host, port),
        db_path=str(db_path or DEFAULT_DB),
        env_path=env_path,
        token=token,
        client_factory=client_factory or (lambda t: finder.GitHubClient(token=t)),
        token_validator=token_validator or finder.validate_token,
        delay=delay,
        gmail=gmail,
        sender_factory=sender_factory,
        cred_validator=cred_validator,
    )
