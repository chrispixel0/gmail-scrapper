"""
Send emails through the user's own Gmail account over SMTP.

Design rules baked in for safety:
- Only an explicit list of recipients (an uploaded TXT of people who opted in)
  is ever mailed.
- Every address ever emailed is remembered, and is never emailed again
  ("permanent de-duplication"). This also makes Stop/Send resume for free.
- A daily cap (default 200, hard maximum 500) is counted across restarts, so
  closing the app cannot reset it.

Uses only the Python standard library (smtplib, email).
"""

from __future__ import annotations

import os
import re
import smtplib
import sqlite3
import ssl
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterator

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
DEFAULT_DAILY_CAP = 200
MAX_DAILY_CAP = 500                     # a guard so a typo can't send thousands
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

UNSUBSCRIBE_NOTE = (
    "\n\n--\n"
    "You are receiving this email because you signed up to hear from {sender}.\n"
    "If you no longer want these emails, reply to this message with \"STOP\"."
)


def is_valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match((addr or "").strip()))


def parse_recipients(text: str) -> list[str]:
    """Pull valid addresses out of an uploaded TXT (one per line, or comma/space separated)."""
    seen, out = set(), []
    for token in re.split(r"[\s,;]+", text or ""):
        addr = token.strip().strip("<>").lower()
        if addr and addr not in seen and is_valid_email(addr):
            seen.add(addr)
            out.append(addr)
    return out


def clamp_cap(cap) -> int:
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP
    return max(1, min(MAX_DAILY_CAP, cap))


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class MailError(RuntimeError):
    def __init__(self, message: str, code: str = "mail_error"):
        super().__init__(message)
        self.code = code


def _friendly_smtp_error(exc: Exception) -> MailError:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return MailError(
            "Gmail rejected the address or App Password. Make sure 2-Step Verification "
            "is on and you pasted the 16-character App Password (not your normal password).",
            "bad_credentials",
        )
    if isinstance(exc, smtplib.SMTPException):
        return MailError(f"Gmail refused the message: {exc}", "smtp_error")
    return MailError(f"Could not reach Gmail: {exc}", "network")


# --------------------------------------------------------------------------
# Gmail SMTP sender
# --------------------------------------------------------------------------

class GmailSender:
    """Keeps one SMTP connection open and reconnects if Gmail drops it."""

    def __init__(self, address: str, app_password: str, sender_name: str = ""):
        self.address = address.strip()
        self.app_password = app_password.strip().replace(" ", "")  # App Passwords show as 4x4
        self.sender_name = sender_name.strip()
        self._smtp: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        try:
            smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.address, self.app_password)
            return smtp
        except Exception as e:
            raise _friendly_smtp_error(e) from e

    def verify(self) -> None:
        """Log in and disconnect, to confirm the credentials work."""
        self._connect().quit()

    def build_message(self, to: str, subject: str, body: str, add_unsub: bool) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.sender_name or None, self.address))
        msg["To"] = to
        msg["Subject"] = subject
        text = body
        if add_unsub:
            text += UNSUBSCRIBE_NOTE.format(sender=self.sender_name or self.address)
            # Lets Gmail show a one-click unsubscribe to the recipient.
            msg["List-Unsubscribe"] = f"<mailto:{self.address}?subject=unsubscribe>"
        msg.set_content(text)
        return msg

    def send(self, to: str, subject: str, body: str, add_unsub: bool) -> None:
        msg = self.build_message(to, subject, body, add_unsub)
        for attempt in (1, 2):                     # reconnect once if the link went stale
            if self._smtp is None:
                self._smtp = self._connect()
            try:
                self._smtp.send_message(msg)
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
                self.close()
                if attempt == 2:
                    raise _friendly_smtp_error(e) from e
            except smtplib.SMTPRecipientsRefused as e:
                raise MailError(f"Gmail refused this address: {to}", "bad_recipient") from e
            except smtplib.SMTPException as e:
                raise _friendly_smtp_error(e) from e

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except Exception:
                pass
            self._smtp = None


# --------------------------------------------------------------------------
# Record of what has been sent (SQLite, shared db file)
# --------------------------------------------------------------------------

SENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_emails (
    email    TEXT PRIMARY KEY,
    subject  TEXT,
    sent_at  TEXT NOT NULL
);
"""


class SentStore:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute(SENT_SCHEMA)
        self.conn.commit()

    def already_sent(self, email: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM sent_emails WHERE email = ?", (email,)).fetchone()
        return row is not None

    def count_sent_on(self, day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sent_emails WHERE substr(sent_at, 1, 10) = ?", (day,)
        ).fetchone()[0]

    def record(self, email: str, subject: str, when: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sent_emails (email, subject, sent_at) VALUES (?, ?, ?)",
            (email, subject, when),
        )
        self.conn.commit()

    def total(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sent_emails").fetchone()[0]

    def clear(self) -> int:
        n = self.conn.execute("DELETE FROM sent_emails").rowcount
        self.conn.commit()
        return n

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------
# Send workflow
# --------------------------------------------------------------------------

def iter_send(recipients: list[str], subject: str, body: str, gap: float, cap: int,
              add_unsub: bool, sender: GmailSender, store: SentStore,
              cancel=None) -> Iterator[dict]:
    """
    Send to each recipient, yielding progress events (dicts with a "type"):
      start   {"total", "sent_today", "cap", "remaining"}
      sent    {"email", "sent", "skipped", "sent_today", "total"}
      skipped {"email", "reason", "skipped", ...}
      error   {"email", "message", "failed", ...}
      limit   {"cap", "sent_today"}             daily cap reached -> stop
      done    {"sent", "skipped", "failed", "sent_today"}
    Addresses already in the store are skipped (permanent de-duplication), which
    also makes a second run resume where the first stopped.
    """
    cap = clamp_cap(cap)
    today = date.today().isoformat()
    sent_today = store.count_sent_on(today)
    total = len(recipients)
    yield {"type": "start", "total": total, "sent_today": sent_today, "cap": cap,
           "remaining": max(0, cap - sent_today)}

    sent = skipped = failed = 0
    try:
        for raw in recipients:
            if cancel is not None and cancel.is_set():
                break
            email = (raw or "").strip().lower()

            if not is_valid_email(email):
                skipped += 1
                yield {"type": "skipped", "email": raw, "reason": "not a valid address",
                       "skipped": skipped}
                continue
            if store.already_sent(email):
                skipped += 1
                yield {"type": "skipped", "email": email, "reason": "already sent before",
                       "skipped": skipped}
                continue
            if sent_today >= cap:
                yield {"type": "limit", "cap": cap, "sent_today": sent_today}
                break

            try:
                sender.send(email, subject, body, add_unsub)
            except MailError as e:
                if e.code in ("bad_credentials", "network", "smtp_error"):
                    raise            # a whole-batch problem: stop and report once
                failed += 1
                yield {"type": "error", "email": email, "message": str(e), "failed": failed}
                continue

            store.record(email, subject, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            sent += 1
            sent_today += 1
            yield {"type": "sent", "email": email, "sent": sent, "skipped": skipped,
                   "sent_today": sent_today, "total": total}

            # Wait the gap before the next send, but wake up immediately on Stop.
            if sent_today < cap and gap > 0:
                if cancel is not None:
                    if cancel.wait(gap):
                        break
                else:
                    time.sleep(gap)
    finally:
        sender.close()

    yield {"type": "done", "sent": sent, "skipped": skipped, "failed": failed,
           "sent_today": sent_today}


def validate_credentials(address: str, app_password: str) -> None:
    """Raise MailError if Gmail does not accept the address + App Password."""
    if not is_valid_email(address):
        raise MailError("That doesn't look like a valid email address.", "bad_address")
    GmailSender(address, app_password).verify()
