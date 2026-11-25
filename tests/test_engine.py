import json
import os
import random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from omni_mail import engine, service
from omni_mail.db import connect, iso
from omni_mail.render import RenderError, render, unsubscribe_token
from omni_mail.transport import FakeTransport

# Tuesday 2026-12-01 15:00 UTC = 10:00 America/New_York
TUE_10AM = datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    service.update_setting(c, "physical_address", "123 Main St, Louisville, KY 40202")
    service.update_setting(c, "company_name", "OMNI")
    service.add_mailbox(c, "ryan@omni-mail.test", "Ryan Bear", "PW_RYAN", warmup_increment=0, daily_limit=50,
                        signature="Ryan\nOMNI")
    # backdate mailbox creation so warmup math is predictable
    c.execute("UPDATE mailboxes SET created_at=?", (iso(TUE_10AM - timedelta(days=30)),))
    service.upsert_leads(c, [
        {"email": "Jane@Apt.test", "first_name": "Jane", "company": "Apt Co", "unit_count": "240"},
        {"Email": "bob@club.test", "First Name": "", "Company Name": "Club"},
    ], tags="louisville")
    service.create_campaign(c, "Apartments", daily_limit=10)
    service.set_sequence(c, "Apartments", [
        {"subject": "{Quick|Fast} question, {{first_name|there}}", "body": "Hi {{first_name|there}}, "
         "{{company}} has {{unit_count|lots of}} units.", "delay_days": 0},
        {"subject": "", "body": "Following up.", "delay_days": 2},
        {"subject": "", "body": "Last note.", "delay_days": 3},
    ])
    service.enroll(c, "Apartments", tag="louisville")
    yield c
    c.close()


def tick(conn, t, now, **kw):
    return engine.send_tick(conn, t, now=now, rng=random.Random(1), **kw)


def test_render_fallback_spintax_and_missing():
    assert render("Hi {{first_name|there}}", {"first_name": ""}) == "Hi there"
    assert render("Hi {{ first_name }}", {"first_name": "Al"}) == "Hi Al"
    out = render("{Hi|Hey} {{x|y}}", {}, seed="s")
    assert out in ("Hi y", "Hey y")
    assert render("{Hi|Hey}", {}, seed="s") == render("{Hi|Hey}", {}, seed="s")  # deterministic
    with pytest.raises(RenderError):
        render("Hi {{company}}", {"company": ""})


def test_lead_import_normalizes(conn):
    jane = service.list_leads(conn, search="jane")["leads"][0]
    assert jane["email"] == "jane@apt.test"
    assert json.loads(jane["custom"]) == {"unit_count": "240"}
    assert jane["tags"] == "louisville"
    bob = service.list_leads(conn, search="bob")["leads"][0]
    assert bob["company"] == "Club"
    r = service.upsert_leads(conn, [{"email": "not-an-email"}])
    assert r["invalid_count"] == 1


def test_requires_physical_address(conn):
    service.update_setting(conn, "physical_address", "")
    service.set_status(conn, "Apartments", "draft")
    with pytest.raises(ValueError):
        service.set_status(conn, "Apartments", "active")
    conn.execute("UPDATE campaigns SET status='active'")
    r = tick(conn, FakeTransport(), TUE_10AM)
    assert r["errors"] and not r["sent"]


def test_full_sequence_threads_and_respects_gaps(conn):
    t = FakeTransport()
    service.set_status(conn, "Apartments", "active")

    r = tick(conn, t, TUE_10AM)
    assert len(r["sent"]) == 1  # one per mailbox per tick
    r = tick(conn, t, TUE_10AM + timedelta(seconds=30))
    assert r["sent"] == []  # gap not elapsed
    r = tick(conn, t, TUE_10AM + timedelta(minutes=10))
    assert len(r["sent"]) == 1
    assert len(t.sent) == 2

    first = {m["To"]: m for _, m in t.sent}
    jane_first = next(m for k, m in first.items() if "jane@apt.test" in k)
    assert jane_first["Subject"] in ("Quick question, Jane", "Fast question, Jane")
    body = jane_first.get_content()
    assert "Apt Co has 240 units" in body and "123 Main St" in body and "hear from me" in body and "--" not in body
    assert "List-Unsubscribe" in jane_first

    # follow-up not due yet
    assert tick(conn, t, TUE_10AM + timedelta(days=1))["sent"] == []
    # two days later -> follow-ups, threaded
    later = TUE_10AM + timedelta(days=2, minutes=20)
    tick(conn, t, later)
    tick(conn, t, later + timedelta(minutes=10))
    followups = [m for _, m in t.sent[2:]]
    assert len(followups) == 2
    jf = next(m for m in followups if "jane@apt.test" in m["To"])
    assert jf["Subject"] == "Re: " + jane_first["Subject"]
    assert jf["In-Reply-To"] == jane_first["Message-ID"]

    stats = service.campaign_stats(conn, 1)
    assert stats["emails_sent"] == 4 and stats["leads_contacted"] == 2


def test_send_window_and_weekend(conn):
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    assert tick(conn, t, datetime(2026, 12, 1, 11, 0, tzinfo=timezone.utc))["sent"] == []  # 6am ET
    assert tick(conn, t, datetime(2026, 12, 5, 15, 0, tzinfo=timezone.utc))["sent"] == []  # Saturday
    assert len(tick(conn, t, TUE_10AM)["sent"]) == 1


def test_mailbox_warmup_limit(conn):
    conn.execute("UPDATE mailboxes SET warmup_start=1, warmup_increment=1, created_at=?", (iso(TUE_10AM),))
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    tick(conn, t, TUE_10AM)
    tick(conn, t, TUE_10AM + timedelta(minutes=10))
    assert len(t.sent) == 1  # day-0 limit is 1


def test_campaign_daily_limit(conn):
    service.update_campaign(conn, "Apartments", daily_limit=1)
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    tick(conn, t, TUE_10AM)
    r = tick(conn, t, TUE_10AM + timedelta(minutes=10))
    assert len(t.sent) == 1 and any("daily limit" in s for s in r["skipped"])


def _reply(to_msg, from_addr, body, subject=None, **headers):
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = to_msg["From"]
    m["Subject"] = subject or "Re: " + to_msg["Subject"]
    m["Message-ID"] = f"<r{random.random()}@x>"
    m["In-Reply-To"] = to_msg["Message-ID"]
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body + "\n\nOn Tue someone wrote:\n> old stuff")
    return m


def _start_and_send_first(conn):
    t = FakeTransport()
    service.set_status(conn, "Apartments", "active")
    tick(conn, t, TUE_10AM)
    tick(conn, t, TUE_10AM + timedelta(minutes=10))
    jane = next(m for _, m in t.sent if "jane@apt.test" in m["To"])
    return t, jane


def test_reply_stops_sequence_and_manual_reply_threads(conn):
    t, jane = _start_and_send_first(conn)
    t.inbox["ryan@omni-mail.test"] = [(5, _reply(jane, "Jane <jane@apt.test>", "Sure, call me Thursday."))]
    r = engine.sync_inbox(conn, t, now=TUE_10AM + timedelta(hours=1))
    assert r["processed"][0]["type"] == "reply"
    replies = service.list_replies(conn)
    assert replies[0]["body"] == "Sure, call me Thursday."
    lead = service.list_leads(conn, search="jane")["leads"][0]
    assert lead["status"] == "replied"
    # a second sync doesn't double-process
    assert engine.sync_inbox(conn, t)["processed"] == []

    later = TUE_10AM + timedelta(days=3)
    tick(conn, t, later)
    tick(conn, t, later + timedelta(minutes=10))
    assert not any("jane@apt.test" in m["To"] for _, m in t.sent[2:])  # no follow-up to Jane

    res = engine.send_manual_reply(conn, t, "jane@apt.test", "Great, Thursday 2pm works.")
    sent = t.sent[-1][1]
    assert sent["Subject"].startswith("Re: ") and sent["In-Reply-To"]
    assert res["from"] == "ryan@omni-mail.test"
    assert service.list_replies(conn) == []  # now handled


def test_unsubscribe_reply_and_auto_reply(conn):
    t, jane = _start_and_send_first(conn)
    bob = next(m for _, m in t.sent if "bob@club.test" in m["To"])
    t.inbox["ryan@omni-mail.test"] = [
        (1, _reply(jane, "jane@apt.test", "I'm out of the office", subject="Automatic reply: hi",
                   Auto_Submitted="auto-replied")),
        (2, _reply(bob, "bob@club.test", "Please unsubscribe me")),
    ]
    r = engine.sync_inbox(conn, t)
    kinds = {p["lead"]: p["type"] for p in r["processed"]}
    assert kinds == {"jane@apt.test": "auto_reply", "bob@club.test": "unsubscribe"}
    assert engine.is_suppressed(conn, "bob@club.test")
    enr = {row["email"]: row["status"] for row in conn.execute(
        "SELECT l.email, e.status FROM enrollments e JOIN leads l ON l.id=e.lead_id")}
    assert enr["jane@apt.test"] == "active"  # auto-reply doesn't stop the sequence
    assert enr["bob@club.test"] == "unsubscribed"
    # can't be re-enrolled
    service.create_campaign(conn, "Other")
    service.set_sequence(conn, "Other", [{"subject": "x", "body": "y"}])
    assert service.enroll(conn, "Other", emails=["bob@club.test"])["enrolled"] == 0


def test_bounce(conn):
    t, jane = _start_and_send_first(conn)
    b = EmailMessage()
    b["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    b["Subject"] = "Delivery Status Notification (Failure)"
    b["X-Failed-Recipients"] = "jane@apt.test"
    b.set_content("Address not found")
    t.inbox["ryan@omni-mail.test"] = [(9, b)]
    r = engine.sync_inbox(conn, t)
    assert r["processed"] == [{"type": "bounce", "lead": "jane@apt.test"}]
    assert service.list_leads(conn, search="jane")["leads"][0]["status"] == "bounced"
    assert engine.is_suppressed(conn, "jane@apt.test")


def test_domain_suppression_and_render_error(conn):
    engine.suppress(conn, "@club.test", "competitor")
    service.update_lead(conn, "jane@apt.test", fields={"company": ""})
    conn.execute("UPDATE leads SET company='' WHERE email='jane@apt.test'")
    v = service.validate_campaign(conn, "Apartments")
    assert v["render_errors"]
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    r = tick(conn, t, TUE_10AM)
    assert t.sent == [] and any("company" in e for e in r["errors"])


def test_preview_and_dry_run(conn):
    p = service.preview(conn, "Apartments", "jane@apt.test")
    assert [s["step"] for s in p] == [1, 2, 3]
    assert p[1]["subject"] == "Re: " + p[0]["subject"]
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    r = tick(conn, t, TUE_10AM, dry_run=True)
    assert len(r["sent"]) == 1 and t.sent == []
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_unsubscribe_token_stable(conn):
    assert unsubscribe_token(conn, "A@b.c") == unsubscribe_token(conn, "a@b.c")


def test_password_env_validation(conn):
    with pytest.raises(ValueError):
        service.add_mailbox(conn, "x@y.test", "X", "hunter2 my password")


def test_mcp_server_imports():
    os.environ["OMNI_MAIL_DRY_RUN"] = "1"
    from omni_mail import mcp_server  # noqa: F401
