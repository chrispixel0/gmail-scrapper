import os
import tempfile
import threading
import unittest

from backend import mailer
from tests.fakes import FakeSender


class HelperTests(unittest.TestCase):
    def test_is_valid_email(self):
        self.assertTrue(mailer.is_valid_email("a@gmail.com"))
        self.assertFalse(mailer.is_valid_email("nope"))
        self.assertFalse(mailer.is_valid_email("a@b"))
        self.assertFalse(mailer.is_valid_email(""))

    def test_parse_recipients_dedupes_and_lowercases(self):
        text = "A@Gmail.com\n b@gmail.com , a@gmail.com\n\nnot-an-email\n<c@gmail.com>"
        self.assertEqual(mailer.parse_recipients(text), ["a@gmail.com", "b@gmail.com", "c@gmail.com"])

    def test_clamp_cap(self):
        self.assertEqual(mailer.clamp_cap(200), 200)
        self.assertEqual(mailer.clamp_cap(99999), mailer.MAX_DAILY_CAP)
        self.assertEqual(mailer.clamp_cap(0), 1)
        self.assertEqual(mailer.clamp_cap("abc"), mailer.DEFAULT_DAILY_CAP)

    def test_build_message_unsub(self):
        sender = FakeSender(sender_name="Paul")
        real = mailer.GmailSender("me@gmail.com", "pw", "Paul")
        with_note = real.build_message("x@gmail.com", "Hi", "Body", add_unsub=True)
        self.assertIn("unsubscribe", with_note["List-Unsubscribe"].lower())
        self.assertIn("STOP", with_note.get_content())
        self.assertEqual(real.build_message("x@gmail.com", "Hi", "Body", False).get("List-Unsubscribe"), None)
        self.assertEqual(with_note["From"], "Paul <me@gmail.com>")


class IterSendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = mailer.SentStore(os.path.join(self.tmp.name, "d", "r.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_send(self, recipients, sender, cap=200, cancel=None, add_unsub=False):
        if cancel is not None and hasattr(sender, "bind_cancel"):
            sender.bind_cancel(cancel)
        return list(mailer.iter_send(recipients, "Hi", "Body", gap=0, cap=cap,
                                     add_unsub=add_unsub, sender=sender, store=self.store,
                                     cancel=cancel))

    def test_sends_and_records(self):
        sender = FakeSender()
        events = self.run_send(["a@gmail.com", "b@gmail.com"], sender)
        self.assertEqual(sender.sent, ["a@gmail.com", "b@gmail.com"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["sent"], 2)
        self.assertTrue(self.store.already_sent("a@gmail.com"))

    def test_permanent_dedup_skips_known_addresses(self):
        self.store.record("a@gmail.com", "old", "2026-01-01T00:00:00+00:00")
        sender = FakeSender()
        events = self.run_send(["a@gmail.com", "b@gmail.com"], sender)
        self.assertEqual(sender.sent, ["b@gmail.com"])           # a was skipped
        skip = next(e for e in events if e["type"] == "skipped")
        self.assertIn("already sent", skip["reason"])

    def test_invalid_addresses_skipped(self):
        sender = FakeSender()
        events = self.run_send(["good@gmail.com", "bad-address"], sender)
        self.assertEqual(sender.sent, ["good@gmail.com"])
        self.assertEqual(events[-1]["skipped"], 1)

    def test_daily_cap_stops(self):
        sender = FakeSender()
        recips = [f"u{i}@gmail.com" for i in range(5)]
        events = self.run_send(recips, sender, cap=2)
        self.assertEqual(len(sender.sent), 2)
        self.assertTrue(any(e["type"] == "limit" for e in events))

    def test_daily_cap_counts_across_restart(self):
        # Pretend 2 were already sent today, cap is 3 -> only 1 more allowed now.
        from datetime import date
        today = date.today().isoformat()
        for i in range(2):
            self.store.record(f"old{i}@gmail.com", "x", f"{today}T10:00:00+00:00")
        sender = FakeSender()
        self.run_send(["n1@gmail.com", "n2@gmail.com", "n3@gmail.com"], sender, cap=3)
        self.assertEqual(sender.sent, ["n1@gmail.com"])

    def test_per_recipient_failure_continues(self):
        sender = FakeSender()
        sender.fail_addresses = {"b@gmail.com"}
        events = self.run_send(["a@gmail.com", "b@gmail.com", "c@gmail.com"], sender)
        self.assertEqual(sender.sent, ["a@gmail.com", "c@gmail.com"])
        self.assertEqual(events[-1]["failed"], 1)
        self.assertFalse(self.store.already_sent("b@gmail.com"))   # a failure is not recorded

    def test_auth_error_stops_whole_batch(self):
        sender = FakeSender()
        sender.auth_error = True
        with self.assertRaises(mailer.MailError) as cm:
            self.run_send(["a@gmail.com", "b@gmail.com"], sender)
        self.assertEqual(cm.exception.code, "bad_credentials")

    def test_stop_then_resume_continues_where_it_stopped(self):
        recips = [f"u{i}@gmail.com" for i in range(4)]
        first = FakeSender()
        first.stop_after = 2
        cancel = threading.Event()
        self.run_send(recips, first, cancel=cancel)
        self.assertEqual(len(first.sent), 2)                      # stopped after 2

        second = FakeSender()                                     # user clicks Send again
        self.run_send(recips, second, cancel=threading.Event())
        self.assertEqual(second.sent, ["u2@gmail.com", "u3@gmail.com"])   # resumed, no repeats


if __name__ == "__main__":
    unittest.main()
