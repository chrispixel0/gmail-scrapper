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
        self.assertEqual(self.get_json("/api/status"), {"token": False})

    def test_fetch_refused_without_token(self):
        events = self.fetch(name="ABC", location="XYZ")
        self.assertEqual(events[0][0], "fail")
        self.assertEqual(events[0][1]["code"], "no_token")

    def test_save_valid_token_writes_env_file(self):
        self.env_path.write_text("OTHER=1\nGITHUB_TOKEN=old_value_xxxxxxxxxxxxxxx\n")
        status, body = self.post_token(GOOD_TOKEN)
        self.assertEqual((status, body), (200, {"token": True}))
        self.assertEqual(self.get_json("/api/status"), {"token": True})
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

    def test_fetch_requires_name(self):
        events = self.fetch(name="  ", location="XYZ")
        self.assertEqual(events, [("fail", {"message": "Please enter a name."})])

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
