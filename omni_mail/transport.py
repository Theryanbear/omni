"""SMTP sending and IMAP fetching. Swappable for tests."""

from __future__ import annotations

import email
import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, Protocol


class TransportError(RuntimeError):
    pass


def mailbox_password(mailbox) -> str:
    env = mailbox["password_env"]
    pw = os.environ.get(env)
    if not pw:
        raise TransportError(f"environment variable {env} is not set (password for {mailbox['email']})")
    return pw


class Transport(Protocol):
    def send(self, mailbox, msg: EmailMessage) -> None: ...
    def fetch_new(self, mailbox, since_uid: int) -> Iterable[tuple[int, email.message.Message]]: ...
    def check(self, mailbox) -> dict: ...


class RealTransport:
    """SMTP (STARTTLS on 587, SSL on 465) + IMAP over SSL."""

    def _smtp(self, mailbox):
        ctx = ssl.create_default_context()
        host, port = mailbox["smtp_host"], int(mailbox["smtp_port"])
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls(context=ctx)
        s.login(mailbox["smtp_user"], mailbox_password(mailbox))
        return s

    def send(self, mailbox, msg: EmailMessage) -> None:
        try:
            with self._smtp(mailbox) as s:
                s.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise TransportError(f"recipient refused: {e.recipients}") from e
        except (smtplib.SMTPException, OSError) as e:
            raise TransportError(str(e)) from e

    def fetch_new(self, mailbox, since_uid: int):
        if not mailbox["imap_host"]:
            return []
        out = []
        try:
            with imaplib.IMAP4_SSL(mailbox["imap_host"], int(mailbox["imap_port"])) as m:
                m.login(mailbox["smtp_user"], mailbox_password(mailbox))
                m.select("INBOX", readonly=True)
                typ, data = m.uid("search", None, f"UID {since_uid + 1}:*")
                uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid]
                for uid in uids[-500:]:
                    typ, msgdata = m.uid("fetch", str(uid), "(BODY.PEEK[])")
                    for part in msgdata:
                        if isinstance(part, tuple):
                            out.append((uid, email.message_from_bytes(part[1])))
        except (imaplib.IMAP4.error, OSError) as e:
            raise TransportError(f"IMAP error for {mailbox['email']}: {e}") from e
        return out

    def check(self, mailbox) -> dict:
        result = {"smtp": "not tested", "imap": "not configured"}
        try:
            with self._smtp(mailbox):
                result["smtp"] = "ok"
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            result["smtp"] = f"failed: {e}"
        if mailbox["imap_host"]:
            try:
                with imaplib.IMAP4_SSL(mailbox["imap_host"], int(mailbox["imap_port"])) as m:
                    m.login(mailbox["smtp_user"], mailbox_password(mailbox))
                    result["imap"] = "ok"
            except Exception as e:  # noqa: BLE001
                result["imap"] = f"failed: {e}"
        return result


class FakeTransport:
    """In-memory transport for tests and dry runs."""

    def __init__(self):
        self.sent: list[tuple[str, EmailMessage]] = []
        self.inbox: dict[str, list[tuple[int, email.message.Message]]] = {}

    def send(self, mailbox, msg: EmailMessage) -> None:
        self.sent.append((mailbox["email"], msg))

    def fetch_new(self, mailbox, since_uid: int):
        return [(u, m) for u, m in self.inbox.get(mailbox["email"].lower(), []) if u > since_uid]

    def check(self, mailbox) -> dict:
        return {"smtp": "ok", "imap": "ok"}
