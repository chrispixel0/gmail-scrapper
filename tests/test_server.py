import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import io
from pathlib import Path

from backend.finder import GitHubAPIError
from backend.server import make_server, read_env_token
from tests.fakes import FakeClient

GOOD_TOKEN = "github_pat_" + "A" * 40


def fake_validator(token):
    if token != GOOD_TOKEN:
        raise GitHubAPIError("GitHub rejected the token. Please enter a valid token.", "bad_token")


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmp.name) / ".env"
        self.client = FakeClient()
        self._old_env = os.environ.pop("GITHUB_TOKEN", None)
        self.server = make_server("127.0.0.1", 0, os.path.join(self.tmp.name, "r.db"),
                                  env_path=self.env_path,
                                  client_factory=lambda token: self.client,
                                  token_validator=fake_validator, delay=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self._old_env is not None:
            os.environ["GITHUB_TOKEN"] = self._old_env
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers, r.read()

    def get_json(self, path):
        return json.loads(self.get(path)[2])

    def post_token(self, token, content_type="application/json"):
        req = urllib.request.Request(self.base + "/api/token", method="POST",
                                     data=json.dumps({"token": token}).encode(),
                                     headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def fetch(self, **params):
        _, headers, body = self.get("/api/fetch?" + urllib.parse.urlencode(params))
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        return parse_sse(body.decode())


class TokenTests(ServerTestBase):
    def test_no_token_initially(self):
        self.assertEqual(self.get_json("/api/status"), {"token": False, "checked": 0})

    def test_fetch_refused_without_token(self):
        events = self.fetch(name="ABC", location="XYZ")
        self.assertEqual(events[0][0], "fail")
        self.assertEqual(events[0][1]["code"], "no_token")

    def test_save_valid_token_writes_env_file(self):
        self.env_path.write_text("OTHER=1\nGITHUB_TOKEN=old_value_xxxxxxxxxxxxxxx\n")
        status, body = self.post_token(GOOD_TOKEN)
        self.assertEqual((status, body), (200, {"token": True}))
        self.assertTrue(self.get_json("/api/status")["token"])
        self.assertEqual(read_env_token(self.env_path), GOOD_TOKEN)
        self.assertIn("OTHER=1", self.env_path.read_text())        # other settings kept
        self.assertEqual(self.env_path.read_text().count("GITHUB_TOKEN="), 1)

    def test_invalid_token_rejected(self):
        status, body = self.post_token("github_pat_" + "B" * 40)
        self.assertEqual(status, 400)
        self.assertIn("rejected", body["error"])
        self.assertFalse(self.env_path.exists())

    def test_malformed_token_rejected(self):
        status, _ = self.post_token("not a token")
        self.assertEqual(status, 400)

    def test_token_requires_json(self):
        status, _ = self.post_token(GOOD_TOKEN, content_type="text/plain")
        self.assertEqual(status, 415)

    def test_token_loaded_from_env_file_on_start(self):
        self.env_path.write_text(f"GITHUB_TOKEN={GOOD_TOKEN}\n")
        server = make_server("127.0.0.1", 0, env_path=self.env_path)
        try:
            self.assertTrue(server.has_token)
        finally:
            server.server_close()


class ApiTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.server.token = GOOD_TOKEN

    def test_serves_frontend(self):
        for path, needle in [("/", b"<form"), ("/app.js", b"EventSource"), ("/style.css", b":root")]:
            status, _, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn(needle, body)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/../backend/server.py")
        self.assertEqual(cm.exception.code, 404)

    def get_error(self, path):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(path)
        return cm.exception.code, json.loads(cm.exception.read())

    def test_estimate_returns_count_and_saves_nothing(self):
        data = self.get_json("/api/estimate?name=ABC&location=XYZ")
        self.assertEqual(data["total"], 4)
        self.assertIn("seconds", data)
        self.assertEqual(self.client.profile_calls, 0)
        self.assertEqual(self.get_json("/api/results"), [])

    def test_estimate_requires_name(self):
        status, body = self.get_error("/api/estimate?name=&location=XYZ")
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"])

    def test_estimate_requires_token(self):
        self.server.token = None
        status, body = self.get_error("/api/estimate?name=ABC")
        self.assertEqual((status, body["code"]), (400, "no_token"))

    def test_fetch_streams_progress_and_saves_all(self):
        events = self.fetch(name="ABC", location="XYZ")
        types = [t for t, _ in events]
        self.assertEqual(types[:2], ["start", "total"])
        self.assertEqual(types[-1], "done")
        self.assertEqual(types.count("found"), 2)
        self.assertEqual(events[-1][1]["checked"], 4)
        rows = self.get_json("/api/results")
        self.assertEqual(sorted(r["email"] for r in rows), ["Dave@GoogleMail.com", "alice@gmail.com"])
        self.assertTrue(all(r["avatar_url"] for r in rows))

    def _open_stream(self):
        """Start a fetch and return (connection, response, run id from the start event)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("GET", "/api/fetch?name=ABC&location=XYZ")
        resp = conn.getresponse()
        while True:
            line = resp.readline().decode()
            if line.startswith("data: ") and '"start"' in line:
                return conn, resp, json.loads(line[6:])["run"]

    def _post_stop(self, run):
        req = urllib.request.Request(self.base + "/api/stop", method="POST",
                                     data=json.dumps({"run": run}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_stop_ends_running_fetch_and_ignores_stale_ids(self):
        self.server.delay = 0.3
        conn, resp, run = self._open_stream()
        self._post_stop(run - 1)                     # stale id: must not stop this fetch
        self._post_stop(run)                         # correct id: stops it
        rest = resp.read().decode()                  # stream closes once the fetch stops
        conn.close()
        self.assertNotIn("event: done", rest)
        self.assertLess(self.client.profile_calls, 4)

    def test_new_fetch_replaces_stopping_one(self):
        self.server.delay = 0.3
        conn, resp, _ = self._open_stream()
        conn.close()                                 # browser pressed Stop without telling the server
        events = self.fetch(name="ABC", location="XYZ")
        self.assertEqual(events[-1][0], "done")

    def test_fetch_requires_area(self):
        events = self.fetch(name="", location="  ")
        self.assertEqual(events, [("fail", {"message": "Please enter an area."})])

    def test_area_only_fetch(self):
        events = self.fetch(location="XYZ")
        self.assertEqual(self.client.queries[0], "location:XYZ type:user")
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["checked"], 4)

    def test_second_fetch_skips_already_checked(self):
        self.fetch(location="XYZ")
        calls_after_first = self.client.profile_calls
        events = self.fetch(location="XYZ")
        done = events[-1][1]
        self.assertEqual((done["checked"], done["skipped"]), (0, 4))
        self.assertEqual(self.client.profile_calls, calls_after_first)   # no profile re-opened
        self.assertEqual(self.get_json("/api/status")["checked"], 4)

    def test_forget_checked_history(self):
        self.fetch(location="XYZ")
        req = urllib.request.Request(self.base + "/api/checked", method="DELETE")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"deleted": 4})
        events = self.fetch(location="XYZ")
        self.assertEqual(events[-1][1]["checked"], 4)                    # checked again
        self.assertEqual(len(self.get_json("/api/results")), 2)          # saved addresses kept

    def test_fetch_reports_api_error_and_keeps_partial_results(self):
        self.client.fail_after = 1
        events = self.fetch(name="ABC", location="XYZ")
        self.assertEqual(events[-1][0], "fail")
        self.assertEqual(events[-1][1]["code"], "bad_token")
        self.assertEqual([r["login"] for r in self.get_json("/api/results")], ["alice"])

    def test_exports(self):
        self.fetch(name="ABC", location="XYZ")

        _, headers, body = self.get("/api/results.csv")
        self.assertIn('filename="gmail_results.csv"', headers["Content-Disposition"])
        self.assertIn(b"alice@gmail.com", body)

        _, headers, body = self.get("/api/results.txt")
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(sorted(body.decode().split()), ["Dave@GoogleMail.com", "alice@gmail.com"])

        _, headers, body = self.get("/api/results.xlsx")
        self.assertIn("spreadsheetml", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertIn(b"alice@gmail.com", z.read("xl/worksheets/sheet1.xml"))

    def test_clear(self):
        self.fetch(name="ABC", location="XYZ")
        req = urllib.request.Request(self.base + "/api/results", method="DELETE")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"deleted": 2})
        self.assertEqual(self.get_json("/api/results"), [])


if __name__ == "__main__":
    unittest.main()


def fake_cred_validator(address, app_password):
    if app_password == "wrongpassword0000":
        from backend import mailer
        raise mailer.MailError("Gmail rejected the App Password.", "bad_credentials")


class SendTests(unittest.TestCase):
    def setUp(self):
        from tests.fakes import FakeSender
        self.tmp = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmp.name) / ".env"
        self.sender = FakeSender()
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("GITHUB_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")}
        self.server = make_server("127.0.0.1", 0, os.path.join(self.tmp.name, "r.db"),
                                  env_path=self.env_path,
                                  sender_factory=lambda a, p, n: self.sender,
                                  cred_validator=fake_cred_validator, delay=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def connect_gmail(self):
        return self.post("/api/gmail", {"address": "me@gmail.com",
                                        "app_password": "goodpassword0000", "sender_name": "Me"})

    def send(self, recipients, **kw):
        payload = {"recipients": recipients, "subject": "Hi", "body": "Body", "gap": 0}
        payload.update(kw)
        status, data = self.post("/api/send/start", payload)
        self.assertEqual(status, 200, data)
        with urllib.request.urlopen(self.base + f"/api/send/stream?job={data['job']}", timeout=10) as r:
            return parse_sse(r.read().decode())

    def test_gmail_not_configured_initially(self):
        self.assertFalse(self.get_json("/api/gmail/status")["configured"])

    def test_connect_gmail_saves_to_env_without_exposing_password(self):
        status, data = self.connect_gmail()
        self.assertEqual(status, 200)
        self.assertEqual(data["address"], "me@gmail.com")
        self.assertNotIn("password", json.dumps(data).lower())
        from backend.server import read_env
        self.assertEqual(read_env(self.env_path)["GMAIL_APP_PASSWORD"], "goodpassword0000")
        self.assertTrue(self.get_json("/api/gmail/status")["configured"])

    def test_bad_app_password_rejected(self):
        status, data = self.post("/api/gmail", {"address": "me@gmail.com",
                                                "app_password": "wrongpassword0000"})
        self.assertEqual((status, data["code"]), (400, "bad_credentials"))

    def test_send_requires_gmail(self):
        status, data = self.post("/api/send/start",
                                 {"recipients": ["a@gmail.com"], "subject": "Hi", "body": "B"})
        self.assertEqual((status, data["code"]), (400, "gmail_not_set"))

    def test_send_requires_subject_and_body(self):
        self.connect_gmail()
        status, data = self.post("/api/send/start", {"recipients": ["a@gmail.com"], "subject": "", "body": "B"})
        self.assertEqual(status, 400)

    def test_full_send_flow(self):
        self.connect_gmail()
        events = self.send(["a@gmail.com", "b@gmail.com"])
        types = [t for t, _ in events]
        self.assertEqual(types[0], "start")
        self.assertEqual(types[-1], "done")
        self.assertEqual(types.count("sent"), 2)
        self.assertEqual(self.sender.sent, ["a@gmail.com", "b@gmail.com"])
        self.assertEqual(self.get_json("/api/gmail/status")["sent_total"], 2)

    def test_second_send_skips_already_sent(self):
        self.connect_gmail()
        self.send(["a@gmail.com", "b@gmail.com"])
        self.sender.sent.clear()
        events = self.send(["a@gmail.com", "b@gmail.com", "c@gmail.com"])
        self.assertEqual(self.sender.sent, ["c@gmail.com"])          # only the new one
        self.assertEqual(sum(1 for t, _ in events if t == "skipped"), 2)

    def test_daily_cap_enforced(self):
        self.connect_gmail()
        events = self.send([f"u{i}@gmail.com" for i in range(5)], cap=2)
        self.assertEqual(len(self.sender.sent), 2)
        self.assertTrue(any(t == "limit" for t, _ in events))

    def test_cap_over_500_is_clamped(self):
        self.connect_gmail()
        events = self.send([f"u{i}@gmail.com" for i in range(3)], cap=99999)
        start = next(d for t, d in events if t == "start")
        self.assertEqual(start["cap"], 500)

    def test_reset_sent_history(self):
        self.connect_gmail()
        self.send(["a@gmail.com"])
        req = urllib.request.Request(self.base + "/api/sent", method="DELETE")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["deleted"], 1)
        self.assertEqual(self.get_json("/api/gmail/status")["sent_total"], 0)
