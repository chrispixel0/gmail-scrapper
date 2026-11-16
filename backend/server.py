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

from . import exporters, finder

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

def read_env_token(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "GITHUB_TOKEN":
            return value.strip().strip('"').strip("'") or None
    return None


def save_env_token(path: Path, token: str) -> None:
    """Write GITHUB_TOKEN to .env, keeping any other lines in the file."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines = [l for l in lines if l.strip().partition("=")[0].strip() != "GITHUB_TOKEN"]
    lines.append(f"GITHUB_TOKEN={token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)   # readable only by the current user (no-op on Windows)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path: str, env_path: Path, token: str | None,
                 client_factory: Callable[[str | None], object],
                 token_validator: Callable[[str], None], delay: float):
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
            self._json({"token": self.server.has_token})
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
        else:
            self._json({"error": "Not found"}, 404)

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

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/token", "/api/stop"):
            return self._json({"error": "Not found"}, 404)
        # Requiring JSON means other websites can't submit these requests from a browser.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"error": "Expected JSON."}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
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

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/api/results":
            return self._json({"error": "Not found"}, 404)
        store = finder.Store(self.server.db_path)
        try:
            self._json({"deleted": store.clear()})
        finally:
            store.close()

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
        if not name:
            emit("fail", {"message": "Please enter a name."})
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
                                        max_results=max_results, delay=self.server.delay):
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


def make_server(host: str = "127.0.0.1", port: int = 8765, db_path: str | None = None,
                env_path: Path | None = None,
                client_factory: Callable[[str | None], object] | None = None,
                token_validator: Callable[[str], None] | None = None,
                delay: float = 0.1) -> AppServer:
    env_path = Path(env_path or DEFAULT_ENV)
    token = os.environ.get("GITHUB_TOKEN") or read_env_token(env_path)
    return AppServer(
        (host, port),
        db_path=str(db_path or DEFAULT_DB),
        env_path=env_path,
        token=token,
        client_factory=client_factory or (lambda t: finder.GitHubClient(token=t)),
        token_validator=token_validator or finder.validate_token,
        delay=delay,
    )
