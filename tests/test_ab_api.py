import json
import random
from datetime import timedelta
from email.message import EmailMessage

from omni_mail import api, engine, service
from omni_mail.transport import FakeTransport
from tests.test_engine import TUE_10AM, conn  # noqa: F401  (fixture)


def _send_all(conn, t, start, n=20):
    for i in range(n):
        engine.send_tick(conn, t, now=start + timedelta(minutes=10 * i), rng=random.Random(i))


def test_variants_split_and_report(conn):
    service.set_sequence(conn, "Apartments", [
        {"variants": [{"label": "A", "subject": "quick one about your trucks", "body": "Hi {{first_name|there}}, A body."},
                      {"label": "B", "subject": "gut checking this", "body": "Hi {{first_name|there}}, B body."}]},
        {"subject": "", "body": "Follow up.", "delay_days": 2,
         "variants": [{"label": "A", "subject": "", "body": "A follow."}, {"label": "B", "subject": "", "body": "B follow."}]},
    ])
    p = service.preview(conn, "Apartments", "jane@apt.test")
    assert [(x["step"], x["variant"]) for x in p] == [(1, "A"), (1, "B"), (2, "A"), (2, "B")]
    service.set_status(conn, "Apartments", "active")
    t = FakeTransport()
    _send_all(conn, t, TUE_10AM, 3)
    subjects = sorted(m["Subject"] for _, m in t.sent)
    assert subjects == ["gut checking this", "quick one about your trucks"]  # one lead each
    labels = {r["email"]: r["variant"] for r in conn.execute(
        "SELECT l.email, e.variant FROM enrollments e JOIN leads l ON l.id=e.lead_id")}
    assert sorted(labels.values()) == ["A", "B"]
    # follow-up keeps the lead's own variant and threads under its subject
    _send_all(conn, t, TUE_10AM + timedelta(days=2, minutes=5), 3)
    for _, m in t.sent[2:]:
        lead = next(k for k in labels if k in m["To"])
        assert m.get_content().startswith(labels[lead] + " follow.")
    # a reply on B counts for B
    bmsg = next(m for _, m in t.sent if m["Subject"] == "gut checking this")
    r = EmailMessage(); r["From"] = bmsg["To"]; r["Subject"] = "Re: x"; r["Message-ID"] = "<z@y>"; r["In-Reply-To"] = bmsg["Message-ID"]
    r.set_content("Sure, call me.")
    t.inbox["ryan@omni-mail.test"] = [(1, r)]
    engine.sync_inbox(conn, t)
    rep = service.ab_report(conn, "Apartments", min_sent=1)
    first = {v["label"]: v for v in rep["variants"] if v["step"] == 1}
    assert first["B"]["replied"] == 1 and first["A"]["replied"] == 0 and first["B"]["sent"] == 1
    assert rep["leader"] == "B"
    assert "need more data" in service.ab_report(conn, "Apartments")["verdict"]
    # retire A, add C
    service.retire_variant(conn, "Apartments", 1, "A")
    rep = service.add_variant(conn, "Apartments", 1, "C", "raising my hand", "C body")
    assert {v["label"]: v["active"] for v in rep["variants"] if v["step"] == 1} == {"A": False, "B": True, "C": True}


def test_api_roundtrip(conn, monkeypatch, tmp_path):
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("OMNI_MAIL_DB", db)
    monkeypatch.setenv("OMNI_MAIL_DRY_RUN", "1")
    out = api.handle({"op": "overview"})
    assert out["ok"] and out["result"]["campaigns"][0]["name"] == "Apartments"
    assert api.handle({"op": "nope"})["ok"] is False
    bad = api.handle({"op": "start", "args": {"campaign": "missing"}})
    assert bad["ok"] is False and "no campaign" in bad["error"]
    res = api.handle({"op": "upsert_leads", "args": {"leads": [{"email": "new@x.test", "company": "X"}], "tags": "t"}})
    assert res["result"]["added"] == 1
    json.dumps(api.handle({"op": "campaign", "args": {"campaign": "Apartments"}}))


def test_auto_optimize_retires_loser(conn):
    service.set_sequence(conn, "Apartments", [{"variants": [
        {"label": "A", "subject": "a", "body": "A"}, {"label": "B", "subject": "b", "body": "B"}]}])
    service.set_status(conn, "Apartments", "active")
    # fake history: A 40 sent / 0 replies, B 40 sent / 6 replies
    cid = conn.execute("SELECT id FROM campaigns").fetchone()[0]
    for i in range(80):
        lab = "A" if i < 40 else "B"
        conn.execute("INSERT INTO leads (email, created_at) VALUES (?, '2026-01-01')", (f"x{i}@t.test",))
        lid = conn.execute("SELECT id FROM leads WHERE email=?", (f"x{i}@t.test",)).fetchone()[0]
        conn.execute("INSERT INTO enrollments (campaign_id, lead_id, next_send_at, created_at, variant, current_step, status) "
                     "VALUES (?,?,?,?,?,1,'active')", (cid, lid, "2026-01-01", "2026-01-01", lab))
        conn.execute("INSERT INTO messages (direction, kind, campaign_id, lead_id, step_number, sent_at, variant) "
                     "VALUES ('out','sequence',?,?,1,'2026-01-01',?)", (cid, lid, lab))
        if lab == "B" and i % 6 == 0:
            conn.execute("INSERT INTO messages (direction, kind, campaign_id, lead_id, sent_at) VALUES ('in','reply',?,?,'2026-01-02')", (cid, lid))
    made = service.auto_optimize(conn)
    assert len(made) == 1 and "B won" in made[0]["summary"]
    assert {v["label"]: v["active"] for v in service.ab_report(conn, "Apartments")["variants"]} == {"A": False, "B": True}
    assert service.auto_optimize(conn) == []  # nothing left to decide
    d = service.decisions(conn, unreported_only=True, mark_reported=True)
    assert len(d) == 1 and d[0]["data"]["retired"] == ["A"]
    assert service.decisions(conn, unreported_only=True) == []
