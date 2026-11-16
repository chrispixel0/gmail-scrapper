import os
import tempfile
import unittest
from unittest import mock

from backend import finder
from tests.fakes import FakeClient


class QueryTests(unittest.TestCase):
    def test_basic_query(self):
        self.assertEqual(finder.build_query("ABC", "XYZ"), "ABC in:name location:XYZ type:user")

    def test_quotes_multiword_terms(self):
        self.assertEqual(
            finder.build_query("John Smith", "New York"),
            '"John Smith" in:name location:"New York" type:user',
        )

    def test_no_location(self):
        self.assertEqual(finder.build_query("ABC", ""), "ABC in:name type:user")

    def test_strips_user_quotes(self):
        self.assertEqual(finder.build_query('"ABC"', None), "ABC in:name type:user")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            finder.build_query("  ")


class GmailTests(unittest.TestCase):
    def test_is_gmail(self):
        self.assertTrue(finder.is_gmail("a@gmail.com"))
        self.assertTrue(finder.is_gmail("a@GMail.COM"))
        self.assertTrue(finder.is_gmail("a@googlemail.com"))
        self.assertFalse(finder.is_gmail("a@example.com"))
        self.assertFalse(finder.is_gmail("a@gmail.com.evil.org"))
        self.assertFalse(finder.is_gmail(None))
        self.assertFalse(finder.is_gmail(""))
        self.assertFalse(finder.is_gmail("not-an-email"))


class IterFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = finder.Store(os.path.join(self.tmp.name, "sub", "r.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fetch(self, client=None, **kw):
        return list(finder.iter_fetch("ABC", "XYZ", client or FakeClient(), self.store, **kw))

    def test_only_gmail_saved_with_avatar(self):
        events = self.fetch()
        self.assertEqual(events[0], {"type": "start", "query": "ABC in:name location:XYZ type:user"})
        self.assertEqual(events[1], {"type": "total", "total": 4})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual((events[-1]["checked"], events[-1]["found"]), (4, 2))
        saved = {r["login"]: r for r in self.store.all()}
        self.assertEqual(sorted(saved), ["alice", "dave"])
        self.assertEqual(saved["alice"]["avatar_url"], "https://avatars.githubusercontent.com/u/1?v=4")

    def test_non_gmail_email_not_exposed(self):
        events = self.fetch()
        bob = next(e for e in events if e.get("login") == "bob")
        self.assertEqual(bob["reason"], "not a Gmail address")
        self.assertNotIn("bob@example.com", str(events))

    def test_max_results_respected(self):
        events = self.fetch(max_results=1)
        self.assertEqual(events[-1]["checked"], 1)

    def test_fetches_all_results_across_pages(self):
        with mock.patch.object(finder, "SEARCH_PAGE_SIZE", 1):
            events = self.fetch()
        self.assertEqual(events[-1]["checked"], 4)

    def test_splits_search_when_over_result_cap(self):
        # Pretend GitHub only returns 1 result per search: every user must still be reached.
        client = FakeClient()
        with mock.patch.object(finder, "SEARCH_RESULT_CAP", 1), \
             mock.patch.object(finder, "SEARCH_PAGE_SIZE", 1):
            events = self.fetch(client)
        self.assertTrue(any(e["type"] == "info" for e in events))
        self.assertEqual(events[-1]["checked"], 4)
        self.assertTrue(any("created:" in q for q in client.queries))
        self.assertEqual(sorted(r["login"] for r in self.store.all()), ["alice", "dave"])

    def test_waits_through_rate_limit(self):
        events = self.fetch(FakeClient(rate_limit_once=True))
        self.assertTrue(any(e["type"] == "wait" for e in events))
        self.assertEqual(events[-1]["checked"], 4)

    def test_rerun_does_not_duplicate(self):
        self.fetch()
        self.fetch()
        self.assertEqual(len(self.store.all()), 2)

    def test_clear(self):
        self.fetch()
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.all(), [])

    def test_upgrades_old_database_without_avatar_column(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (login TEXT PRIMARY KEY, name TEXT, location TEXT, "
                     "email TEXT NOT NULL, profile_url TEXT, query TEXT, fetched_at TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES ('old', 'Old', 'X', 'old@gmail.com', '', 'q', '2026-01-01')")
        conn.commit()
        conn.close()
        store = finder.Store(path)
        try:
            self.assertEqual(store.all()[0]["avatar_url"], None)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
