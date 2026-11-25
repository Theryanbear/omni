"""JSON command interface: one request in, one JSON answer out.

Used by the Jarvis desktop app (`python3 -m omni_mail api` with the request on stdin)
so Claude can drive every part of the engine without a server.

Request:  {"op": "ab_report", "args": {"campaign": "Fleet Dec"}}
Answer:   {"ok": true, "result": ...}  or  {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import os
import sys

from . import __version__, engine, service
from .db import connect, load_dotenv
from .transport import FakeTransport, RealTransport


def _transport():
    return FakeTransport() if os.environ.get("OMNI_MAIL_DRY_RUN") == "1" else RealTransport()


def _mailbox_check(conn, email):
    mb = conn.execute("SELECT * FROM mailboxes WHERE email=?", (email.lower(),)).fetchone()
    if not mb:
        raise ValueError(f"no mailbox {email}")
    return _transport().check(mb)


def _campaigns(conn):
    return service.overview(conn)["campaigns"]


OPS = {
    "version": lambda c: {"version": __version__},
    "overview": lambda c: service.overview(c),
    "settings": lambda c: service.get_settings(c),
    "set_setting": lambda c, key, value: service.update_setting(c, key, value),
    "mailboxes": lambda c: [{k: v for k, v in m.items() if k != "imap_last_uid"} for m in service.list_mailboxes(c)],
    "add_mailbox": lambda c, **kw: service.add_mailbox(c, **kw),
    "update_mailbox": lambda c, email, **kw: service.update_mailbox(c, email, **kw),
    "test_mailbox": lambda c, email: _mailbox_check(c, email),
    "upsert_leads": lambda c, leads, tags="": service.upsert_leads(c, leads, tags),
    "import_csv": lambda c, path, tags="": service.import_csv(c, path, tags=tags),
    "list_leads": lambda c, **kw: service.list_leads(c, **kw),
    "update_lead": lambda c, email, **kw: service.update_lead(c, email, **kw),
    "campaigns": _campaigns,
    "create_campaign": lambda c, **kw: service.create_campaign(c, **kw),
    "update_campaign": lambda c, campaign, **kw: service.update_campaign(c, campaign, **kw),
    "campaign": lambda c, campaign: service.campaign_detail(c, campaign),
    "set_sequence": lambda c, campaign, steps: service.set_sequence(c, campaign, steps),
    "add_variant": lambda c, campaign, step, label, subject, body: service.add_variant(c, campaign, step, label, subject, body),
    "retire_variant": lambda c, campaign, step, label: service.retire_variant(c, campaign, step, label),
    "ab_report": lambda c, campaign, min_sent=30: service.ab_report(c, campaign, min_sent),
    "enroll": lambda c, campaign, emails=None, tag="", status="": service.enroll(c, campaign, emails, tag, status),
    "unenroll": lambda c, campaign, emails: service.unenroll(c, campaign, emails),
    "preview": lambda c, campaign, lead_email=None, step=None: service.preview(c, campaign, lead_email, step),
    "validate": lambda c, campaign: service.validate_campaign(c, campaign),
    "start": lambda c, campaign: service.set_status(c, campaign, "active"),
    "pause": lambda c, campaign: service.set_status(c, campaign, "paused"),
    "replies": lambda c, unhandled_only=True, limit=25: service.list_replies(c, unhandled_only, limit),
    "inbox_events": lambda c, since="", limit=50: service.inbox_events(c, since, limit),
    "outbox": lambda c, limit=50, since="": service.outbox(c, limit, since),
    "thread": lambda c, lead_email: service.get_thread(c, lead_email),
    "send_reply": lambda c, lead_email, body: engine.send_manual_reply(c, _transport(), lead_email, body),
    "mark_handled": lambda c, message_ids: service.mark_handled(c, message_ids),
    "suppress": lambda c, entry, reason="manual": (engine.suppress(c, entry, reason), {"suppressed": entry})[1],
    "suppression": lambda c, limit=100: service.list_suppression(c, limit),
    "auto_optimize": lambda c, min_sent=30: service.auto_optimize(c, min_sent),
    "decisions": lambda c, unreported_only=False, mark_reported=False, limit=50: service.decisions(c, unreported_only, mark_reported, limit),
    "ensure_handoff_campaign": lambda c, name="Cowork outreach": service.ensure_handoff_campaign(c, name),
    "handoff": lambda c, leads, campaign="Cowork outreach": service.handoff(c, leads, campaign),
    "tick_preview": lambda c: engine.send_tick(c, FakeTransport(), dry_run=True),
    "sync": lambda c: engine.sync_inbox(c, _transport()),
}


def handle(request: dict) -> dict:
    op = request.get("op")
    args = request.get("args") or {}
    if op not in OPS:
        return {"ok": False, "error": f"unknown op {op!r}", "ops": sorted(OPS)}
    conn = connect()
    try:
        return {"ok": True, "result": OPS[op](conn, **args)}
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - report everything back to the caller
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def main(raw: str | None = None) -> None:
    load_dotenv()
    raw = raw if raw is not None else sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON: {e}"}))
        return
    print(json.dumps(handle(req), default=str))
