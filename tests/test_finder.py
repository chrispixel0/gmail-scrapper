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

    def test_empty_name_and_area_rejected(self):
        with self.assertRaises(ValueError):
            finder.build_query("  ", "")

    def test_area_only_query(self):
        self.assertEqual(finder.build_query("", "Tokyo"), "location:Tokyo type:user")
        self.assertEqual(finder.build_query(None, "New York"), 'location:"New York" type:user')


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


class EstimateTests(unittest.TestCase):
    def test_estimate_counts_without_fetching_profiles(self):
        client = FakeClient()
        result = finder.estimate("ABC", "XYZ", client)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["query"], "ABC in:name location:XYZ type:user")
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertEqual(client.profile_calls, 0)        # no profile was opened

    def test_estimate_seconds_within_quota(self):
        # 100 profiles at 1s each + 1 search page
        self.assertEqual(finder.estimate_seconds(100, 1.0, remaining=5000, reset_in=0), 101)

    def test_estimate_seconds_waits_for_rate_limit(self):
        # 10 left in this hour, reset in 30 min, then 90 more at 0.1s each
        secs = finder.estimate_seconds(100, 0.1, remaining=10, reset_in=1800)
        self.assertAlmostEqual(secs, 1800 + 90 * 0.1 + 1 * 0.1)

    def test_estimate_seconds_multiple_hours(self):
        # fast connection: 12,000 profiles need two full hourly windows after the first
        secs = finder.estimate_seconds(12000, 0.1, remaining=0, reset_in=0, hourly=5000)
        self.assertGreater(secs, 2 * 3600)


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

    def test_skip_checked_skips_profiles_from_earlier_fetches(self):
        self.fetch(skip_checked=True)
        self.assertEqual(self.store.checked_count(), 4)      # Gmail or not, all are remembered
        client = FakeClient()
        events = self.fetch(client, skip_checked=True)
        self.assertEqual((events[-1]["checked"], events[-1]["skipped"]), (0, 4))
        self.assertEqual(client.profile_calls, 0)

    def test_stopped_fetch_resumes_where_it_left_off(self):
        gen = finder.iter_fetch("ABC", "XYZ", FakeClient(), self.store, skip_checked=True)
        for ev in gen:                                        # stop after 2 profiles
            if ev["type"] in ("checked", "found") and ev["checked"] == 2:
                gen.close()
                break
        client = FakeClient()
        events = self.fetch(client, skip_checked=True)
        self.assertEqual((events[-1]["checked"], events[-1]["skipped"]), (2, 2))
        self.assertEqual(client.profile_calls, 2)             # only the 2 unchecked ones

    def test_cancel_stops_before_next_profile(self):
        import threading
        cancel = threading.Event()
        client = FakeClient()
        gen = finder.iter_fetch("ABC", "XYZ", client, self.store, skip_checked=True, cancel=cancel)
        for ev in gen:
            if ev["type"] in ("checked", "found") and ev["checked"] == 1:
                cancel.set()                                  # Stop pressed after the 1st profile
        self.assertEqual(client.profile_calls, 1)             # no further profile was opened
        self.assertEqual(self.store.checked_count(), 1)

    def test_skipped_progress_reported_in_batches(self):
        users = {f"u{i}": {"login": f"u{i}", "email": None, "created_at": "2015-01-01T00:00:00Z"}
                 for i in range(250)}
        for login in users:
            self.store.mark_checked(login)
        with mock.patch.object(finder, "SEARCH_RESULT_CAP", 1000):
            events = self.fetch(FakeClient(users=users), skip_checked=True)
        batches = [e["skipped"] for e in events if e["type"] == "skipped"]
        self.assertEqual(batches, [100, 200])
        self.assertEqual(events[-1]["skipped"], 250)

    def test_clear_checked(self):
        self.fetch(skip_checked=True)
        self.assertEqual(self.store.clear_checked(), 4)
        self.assertEqual(self.store.checked_count(), 0)

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
