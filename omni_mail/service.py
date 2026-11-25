"""High-level operations shared by the CLI and the MCP server."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import engine
from .db import get_setting, iso, set_setting, utcnow
from .render import RenderError

LEAD_COLUMNS = ("email", "first_name", "last_name", "company", "title", "phone", "website", "city")
LEAD_STATUSES = ("new", "contacted", "replied", "interested", "not_interested", "meeting", "won",
                 "bounced", "unsubscribed")
HEADER_ALIASES = {
    "e-mail": "email", "email address": "email", "work email": "email",
    "first": "first_name", "firstname": "first_name", "first name": "first_name",
    "last": "last_name", "lastname": "last_name", "last name": "last_name",
    "company name": "company", "organization": "company", "business": "company", "business name": "company",
    "job title": "title", "position": "title", "phone number": "phone", "url": "website", "site": "website",
}
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- settings

def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings WHERE key != 'secret'")}


def update_setting(conn, key: str, value: str) -> dict:
    if key == "secret":
        raise ValueError("secret cannot be changed here")
    set_setting(conn, key, value)
    return get_settings(conn)


# ---------------------------------------------------------------- mailboxes

PROVIDERS = {
    "google": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "imap_host": "imap.gmail.com", "imap_port": 993},
    "outlook": {"smtp_host": "smtp.office365.com", "smtp_port": 587, "imap_host": "outlook.office365.com",
                "imap_port": 993},
    "zoho": {"smtp_host": "smtp.zoho.com", "smtp_port": 587, "imap_host": "imap.zoho.com", "imap_port": 993},
}


def add_mailbox(conn, email: str, from_name: str, password_env: str, provider: str = "google",
                smtp_host: str = "", smtp_port: int = 0, imap_host: str = "", imap_port: int = 0,
                smtp_user: str = "", daily_limit: int = 30, warmup_start: int = 10, warmup_increment: int = 3,
                min_gap_seconds: int = 120, max_gap_seconds: int = 420, signature: str = "") -> dict:
    preset = PROVIDERS.get(provider, {})
    vals = dict(
        email=email.strip().lower(), from_name=from_name, password_env=password_env,
        smtp_host=smtp_host or preset.get("smtp_host", ""), smtp_port=smtp_port or preset.get("smtp_port", 587),
        imap_host=imap_host or preset.get("imap_host", ""), imap_port=imap_port or preset.get("imap_port", 993),
        smtp_user=smtp_user or email.strip().lower(), daily_limit=daily_limit, warmup_start=warmup_start,
        warmup_increment=warmup_increment, min_gap_seconds=min_gap_seconds, max_gap_seconds=max_gap_seconds,
        signature=signature, created_at=iso(utcnow()),
    )
    if not vals["smtp_host"]:
        raise ValueError("smtp_host required (or provider = google|outlook|zoho)")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", password_env):
        raise ValueError("password_env must be an ENV_VAR_NAME, not the password itself")
    cols = ",".join(vals)
    conn.execute(f"INSERT INTO mailboxes ({cols}) VALUES ({','.join('?' * len(vals))}) "
                 f"ON CONFLICT(email) DO UPDATE SET " +
                 ",".join(f"{k}=excluded.{k}" for k in vals if k != "created_at"), list(vals.values()))
    return list_mailboxes(conn, email)[0]


def update_mailbox(conn, email: str, **fields) -> dict:
    allowed = {"from_name", "daily_limit", "warmup_start", "warmup_increment", "min_gap_seconds",
               "max_gap_seconds", "signature", "active", "password_env", "smtp_host", "smtp_port",
               "imap_host", "imap_port", "smtp_user"}
    fields = {k: v for k, v in fields.items() if v is not None}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update: {sorted(bad)}")
    if fields:
        conn.execute(f"UPDATE mailboxes SET {','.join(f'{k}=?' for k in fields)} WHERE email=?",
                     [*fields.values(), email.lower()])
    return list_mailboxes(conn, email)[0]


def list_mailboxes(conn, email: str | None = None) -> list[dict]:
    now = utcnow()
    q = "SELECT * FROM mailboxes" + (" WHERE email=?" if email else "") + " ORDER BY id"
    out = []
    for m in conn.execute(q, (email.lower(),) if email else ()):
        d = dict(m)
        d["sent_today"] = engine.mailbox_sent_today(conn, m["id"], now)
        d["limit_today"] = engine.mailbox_effective_limit(m, now)
        out.append(d)
    if email and not out:
        raise ValueError(f"no mailbox {email}")
    return out


# ---------------------------------------------------------------- leads

def _normalize_lead(raw: dict) -> dict:
    lead: dict = {}
    custom: dict = {}
    for k, v in raw.items():
        if k is None:
            continue
        key = k.strip().lower()
        key = HEADER_ALIASES.get(key, key.replace(" ", "_"))
        val = "" if v is None else str(v).strip()
        if key in LEAD_COLUMNS:
            lead[key] = val
        elif key in ("tags", "notes"):
            lead[key] = val
        elif key == "custom" and isinstance(v, dict):
            custom.update(v)
        elif val:
            custom[key] = val
    if "first_name" not in lead and raw.get("name"):
        parts = str(raw["name"]).strip().split(" ", 1)
        lead["first_name"] = parts[0]
        lead["last_name"] = parts[1] if len(parts) > 1 else ""
        custom.pop("name", None)
    lead["custom"] = custom
    lead["email"] = lead.get("email", "").lower()
    return lead


def upsert_leads(conn, rows: list[dict], tags: str = "") -> dict:
    added = updated = 0
    invalid, suppressed = [], []
    now = iso(utcnow())
    for raw in rows:
        lead = _normalize_lead(raw)
        email = lead["email"]
        if not EMAIL_OK.match(email):
            invalid.append(email or str(raw)[:60])
            continue
        if engine.is_suppressed(conn, email):
            suppressed.append(email)
        tag_set = {t.strip() for t in (lead.get("tags", "") + "," + tags).split(",") if t.strip()}
        existing = conn.execute("SELECT * FROM leads WHERE email=?", (email,)).fetchone()
        if existing:
            merged_custom = {**json.loads(existing["custom"] or "{}"), **lead["custom"]}
            merged_tags = {t for t in existing["tags"].split(",") if t} | tag_set
            sets = {k: lead[k] for k in LEAD_COLUMNS if lead.get(k)}
            sets.update(custom=json.dumps(merged_custom), tags=",".join(sorted(merged_tags)))
            if lead.get("notes"):
                sets["notes"] = lead["notes"]
            conn.execute(f"UPDATE leads SET {','.join(f'{k}=?' for k in sets)} WHERE id=?",
                         [*sets.values(), existing["id"]])
            updated += 1
        else:
            conn.execute(
                "INSERT INTO leads (email, first_name, last_name, company, title, phone, website, city, "
                "custom, tags, notes, created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (email, *(lead.get(k, "") for k in LEAD_COLUMNS[1:]), json.dumps(lead["custom"]),
                 ",".join(sorted(tag_set)), lead.get("notes", ""), now,
                 "unsubscribed" if email in suppressed else "new"))
            added += 1
    return {"added": added, "updated": updated, "invalid": invalid[:50], "invalid_count": len(invalid),
            "already_suppressed": suppressed[:50]}


def import_csv(conn, path: str | None = None, csv_text: str | None = None, tags: str = "") -> dict:
    if path:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    elif csv_text:
        text = csv_text
    else:
        raise ValueError("give a path or csv_text")
    rows = list(csv.DictReader(io.StringIO(text)))
    return upsert_leads(conn, rows, tags)


def list_leads(conn, status: str = "", tag: str = "", search: str = "", campaign: str = "",
               limit: int = 50, offset: int = 0) -> dict:
    where, args = [], []
    if status:
        where.append("l.status=?"); args.append(status)
    if tag:
        where.append("(',' || l.tags || ',') LIKE ?"); args.append(f"%,{tag},%")
    if search:
        where.append("(l.email LIKE ? OR l.company LIKE ? OR l.first_name LIKE ? OR l.last_name LIKE ?)")
        args += [f"%{search}%"] * 4
    if campaign:
        c = get_campaign_row(conn, campaign)
        where.append("l.id IN (SELECT lead_id FROM enrollments WHERE campaign_id=?)"); args.append(c["id"])
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM leads l{w}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM leads l{w} ORDER BY l.id DESC LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    return {"total": total, "leads": [dict(r) for r in rows]}


def update_lead(conn, email: str, status: str | None = None, notes: str | None = None,
                add_tags: str = "", fields: dict | None = None) -> dict:
    lead = conn.execute("SELECT * FROM leads WHERE email=?", (email.lower(),)).fetchone()
    if not lead:
        raise ValueError(f"no lead {email}")
    if status:
        if status not in LEAD_STATUSES:
            raise ValueError(f"status must be one of {LEAD_STATUSES}")
        conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead["id"]))
        if status in engine.STOP_LEAD_STATUSES:
            engine.stop_lead(conn, lead["id"], "stopped")
        if status == "unsubscribed":
            engine.suppress(conn, lead["email"], "marked unsubscribed")
    if notes is not None:
        conn.execute("UPDATE leads SET notes=? WHERE id=?", (notes, lead["id"]))
    if add_tags or fields:
        upsert_leads(conn, [{"email": lead["email"], **(fields or {})}], add_tags)
    return dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead["id"],)).fetchone())


# ---------------------------------------------------------------- campaigns

def get_campaign_row(conn, name_or_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM campaigns WHERE name=? OR CAST(id AS TEXT)=?",
                       (str(name_or_id), str(name_or_id))).fetchone()
    if not row:
        raise ValueError(f"no campaign {name_or_id!r}")
    return row


def _mailbox_ids(conn, mailboxes: list[str] | None) -> str:
    if not mailboxes:
        return ""
    ids = []
    for e in mailboxes:
        r = conn.execute("SELECT id FROM mailboxes WHERE email=?", (e.lower(),)).fetchone()
        if not r:
            raise ValueError(f"no mailbox {e}")
        ids.append(str(r["id"]))
    return ",".join(ids)


def _validate_schedule(tz: str, days: str, start: str, end: str) -> None:
    ZoneInfo(tz)
    if not all(d.strip() in "1234567" and d.strip() for d in days.split(",")):
        raise ValueError("send_days like '1,2,3,4,5' (1=Mon … 7=Sun)")
    if datetime.strptime(start, "%H:%M") >= datetime.strptime(end, "%H:%M"):
        raise ValueError("window_start must be before window_end")


def create_campaign(conn, name: str, timezone: str = "America/New_York", send_days: str = "1,2,3,4,5",
                    window_start: str = "08:30", window_end: str = "16:30", daily_limit: int = 100,
                    mailboxes: list[str] | None = None, stop_on_reply: bool = True) -> dict:
    _validate_schedule(timezone, send_days, window_start, window_end)
    conn.execute("INSERT INTO campaigns (name, timezone, send_days, window_start, window_end, daily_limit, "
                 "mailbox_ids, stop_on_reply, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (name, timezone, send_days, window_start, window_end, daily_limit,
                  _mailbox_ids(conn, mailboxes), int(stop_on_reply), iso(utcnow())))
    return campaign_detail(conn, name)


def update_campaign(conn, campaign: str, **fields) -> dict:
    c = get_campaign_row(conn, campaign)
    fields = {k: v for k, v in fields.items() if v is not None}
    if "mailboxes" in fields:
        fields["mailbox_ids"] = _mailbox_ids(conn, fields.pop("mailboxes"))
    if "stop_on_reply" in fields:
        fields["stop_on_reply"] = int(fields["stop_on_reply"])
    allowed = {"name", "timezone", "send_days", "window_start", "window_end", "daily_limit", "mailbox_ids",
               "stop_on_reply"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update: {sorted(bad)}")
    merged = {**dict(c), **fields}
    _validate_schedule(merged["timezone"], merged["send_days"], merged["window_start"], merged["window_end"])
    if fields:
        conn.execute(f"UPDATE campaigns SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                     [*fields.values(), c["id"]])
    return campaign_detail(conn, c["id"])


def set_sequence(conn, campaign: str, steps: list[dict]) -> dict:
    """Replace a campaign's steps. Each step: {subject, body, delay_days}. Step 1 needs a subject.

    Leave subject empty on follow-ups to send them as replies in the same thread.
    """
    c = get_campaign_row(conn, campaign)
    if not steps:
        raise ValueError("at least one step required")
    first_subject = steps[0].get("subject") or ((steps[0].get("variants") or [{}])[0].get("subject", ""))
    if not str(first_subject).strip():
        raise ValueError("step 1 needs a subject")
    sent = conn.execute("SELECT MAX(current_step) FROM enrollments WHERE campaign_id=?", (c["id"],)).fetchone()[0] or 0
    if sent and len(steps) < sent:
        raise ValueError(f"leads have already received {sent} steps; keep at least that many")
    conn.execute("DELETE FROM steps WHERE campaign_id=?", (c["id"],))
    for i, s in enumerate(steps, 1):
        variants = s.get("variants") or []
        if variants:
            s = {**s, "subject": s.get("subject") or variants[0].get("subject", ""), "body": s.get("body") or variants[0].get("body", "")}
            if i == 1 and any(not str(v.get("subject", "")).strip() for v in variants):
                raise ValueError("every step 1 variant needs a subject")
        if not str(s.get("body", "")).strip():
            raise ValueError(f"step {i} has an empty body")
        cur = conn.execute("INSERT INTO steps (campaign_id, step_number, delay_days, subject, body) VALUES (?,?,?,?,?)",
                           (c["id"], i, float(s.get("delay_days", 0 if i == 1 else 3)), s.get("subject", ""), s["body"]))
        for j, v in enumerate(variants):
            label = str(v.get("label") or chr(65 + j))
            if not str(v.get("body", "")).strip():
                raise ValueError(f"step {i} variant {label} has an empty body")
            conn.execute("INSERT INTO step_variants (step_id, label, subject, body, created_at) VALUES (?,?,?,?,?)",
                         (cur.lastrowid, label, v.get("subject", ""), v["body"], iso(utcnow())))
    return campaign_detail(conn, c["id"])


def _step_row(conn, campaign: str, step: int):
    c = get_campaign_row(conn, campaign)
    st = conn.execute("SELECT * FROM steps WHERE campaign_id=? AND step_number=?", (c["id"], int(step))).fetchone()
    if not st:
        raise ValueError(f"campaign {campaign!r} has no step {step}")
    return c, st


def add_variant(conn, campaign: str, step: int, label: str, subject: str, body: str) -> dict:
    """Add (or replace) an A/B variant on a step. New leads get split across all active variants."""
    c, st = _step_row(conn, campaign, step)
    if int(step) == 1 and not subject.strip():
        raise ValueError("step 1 variants need a subject")
    if not body.strip():
        raise ValueError("body required")
    conn.execute("INSERT INTO step_variants (step_id, label, subject, body, created_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(step_id, label) DO UPDATE SET subject=excluded.subject, body=excluded.body, active=1",
                 (st["id"], label, subject, body, iso(utcnow())))
    return ab_report(conn, campaign)


def retire_variant(conn, campaign: str, step: int, label: str) -> dict:
    """Stop assigning new leads to a variant. Leads already on it keep their copy for follow-ups."""
    c, st = _step_row(conn, campaign, step)
    n = conn.execute("UPDATE step_variants SET active=0 WHERE step_id=? AND label=?", (st["id"], label)).rowcount
    if not n:
        raise ValueError(f"no variant {label} on step {step}")
    return ab_report(conn, campaign)


def _z_test(r1: int, n1: int, r2: int, n2: int) -> float:
    import math
    if not n1 or not n2:
        return 0.0
    p = (r1 + r2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0
    return ((r1 / n1) - (r2 / n2)) / se if se else 0.0


def ab_report(conn, campaign: str, min_sent: int = 30) -> dict:
    """Reply rate per variant, with a plain-language verdict.

    A reply counts for the variant the lead was assigned, whichever step they replied to.
    Verdict rules: every active variant needs `min_sent` first emails; the leader must beat
    the runner-up by 2+ points; z >= 1.65 (about 90% confidence) is called 'confident'.
    """
    c = get_campaign_row(conn, campaign)
    rows = []
    for v in conn.execute("SELECT sv.*, s.step_number FROM step_variants sv JOIN steps s ON s.id=sv.step_id "
                          "WHERE s.campaign_id=? ORDER BY s.step_number, sv.id", (c["id"],)):
        sent = conn.execute("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out' AND kind='sequence' "
                            "AND step_number=? AND variant=?", (c["id"], v["step_number"], v["label"])).fetchone()[0]
        leads = conn.execute("SELECT COUNT(*) FROM enrollments WHERE campaign_id=? AND variant=?", (c["id"], v["label"])).fetchone()[0]
        replied = conn.execute("SELECT COUNT(DISTINCT m.lead_id) FROM messages m JOIN enrollments e ON e.lead_id=m.lead_id "
                               "AND e.campaign_id=? WHERE m.direction='in' AND m.kind='reply' AND e.variant=?",
                               (c["id"], v["label"])).fetchone()[0]
        bounced = conn.execute("SELECT COUNT(*) FROM enrollments WHERE campaign_id=? AND variant=? AND status='bounced'",
                               (c["id"], v["label"])).fetchone()[0]
        rows.append({"step": v["step_number"], "label": v["label"], "active": bool(v["active"]), "subject": v["subject"],
                     "leads": leads, "sent": sent, "replied": replied, "bounced": bounced,
                     "reply_rate": round(replied / sent, 4) if sent else 0.0})
    if not rows:  # copy written per lead (e.g. by Cowork) carries its own variant label
        for lab in [r["variant"] for r in conn.execute(
                "SELECT DISTINCT variant FROM enrollments WHERE campaign_id=? AND variant!='' ORDER BY variant", (c["id"],))]:
            sent = conn.execute("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out' AND kind='sequence' "
                                "AND step_number=1 AND variant=?", (c["id"], lab)).fetchone()[0]
            leads = conn.execute("SELECT COUNT(*) FROM enrollments WHERE campaign_id=? AND variant=?", (c["id"], lab)).fetchone()[0]
            replied = conn.execute("SELECT COUNT(DISTINCT m.lead_id) FROM messages m JOIN enrollments e ON e.lead_id=m.lead_id "
                                   "AND e.campaign_id=? WHERE m.direction='in' AND m.kind='reply' AND e.variant=?",
                                   (c["id"], lab)).fetchone()[0]
            rows.append({"step": 1, "label": lab, "active": True, "subject": "(written per lead)", "leads": leads,
                         "sent": sent, "replied": replied, "bounced": 0,
                         "reply_rate": round(replied / sent, 4) if sent else 0.0, "per_lead": True})
    verdict, leader = "no A/B test running", None
    firsts = [r for r in rows if r["step"] == 1 and r["active"]]
    if len(firsts) >= 2:
        ranked = sorted(firsts, key=lambda r: r["reply_rate"], reverse=True)
        a, b = ranked[0], ranked[1]
        z = _z_test(a["replied"], a["sent"], b["replied"], b["sent"])
        gap = (a["reply_rate"] - b["reply_rate"]) * 100
        if min(r["sent"] for r in firsts) < min_sent:
            need = max(min_sent - r["sent"] for r in firsts)
            verdict = f"need more data: about {need} more sends on the smallest variant before calling it"
        elif gap >= 2 and z >= 1.65:
            verdict, leader = f"{a['label']} wins (confident): {gap:.1f} points ahead, z={z:.2f}", a["label"]
        elif gap >= 2:
            verdict, leader = f"{a['label']} leads by {gap:.1f} points but it could be noise (z={z:.2f}); keep running", a["label"]
        else:
            verdict = f"too close to call ({gap:.1f} point gap)"
    return {"campaign": c["name"], "variants": rows, "verdict": verdict, "leader": leader, "min_sent": min_sent}


def outbox(conn, limit: int = 50, since: str = "") -> list[dict]:
    q = ("SELECT m.id, m.sent_at, m.from_addr, m.to_addr, m.subject, m.step_number, m.variant, m.kind, c.name AS campaign "
         "FROM messages m LEFT JOIN campaigns c ON c.id=m.campaign_id WHERE m.direction='out'")
    args: list = []
    if since:
        q += " AND m.sent_at > ?"; args.append(since)
    return [dict(r) for r in conn.execute(q + " ORDER BY m.id DESC LIMIT ?", (*args, limit))]


def inbox_events(conn, since: str = "", limit: int = 50) -> list[dict]:
    """Replies, bounces and unsubscribes after `since` (ISO time), newest first."""
    q = ("SELECT m.id, m.kind, m.sent_at, m.from_addr, m.subject, substr(m.body,1,600) AS body, m.handled, "
         "l.company, l.first_name, c.name AS campaign, e.variant FROM messages m LEFT JOIN leads l ON l.id=m.lead_id "
         "LEFT JOIN campaigns c ON c.id=m.campaign_id LEFT JOIN enrollments e ON e.lead_id=m.lead_id AND e.campaign_id=m.campaign_id "
         "WHERE m.direction='in'")
    args: list = []
    if since:
        q += " AND m.sent_at > ?"; args.append(since)
    return [dict(r) for r in conn.execute(q + " ORDER BY m.id DESC LIMIT ?", (*args, limit))]


def enroll(conn, campaign: str, emails: list[str] | None = None, tag: str = "", status: str = "") -> dict:
    c = get_campaign_row(conn, campaign)
    where, args = ["1=1"], []
    if emails:
        where.append(f"email IN ({','.join('?' * len(emails))})"); args += [e.lower() for e in emails]
    if tag:
        where.append("(',' || tags || ',') LIKE ?"); args.append(f"%,{tag},%")
    if status:
        where.append("status=?"); args.append(status)
    if not (emails or tag or status):
        raise ValueError("choose leads by emails, tag or status")
    leads = conn.execute(f"SELECT * FROM leads WHERE {' AND '.join(where)}", args).fetchall()
    added, skipped = 0, []
    now = iso(utcnow())
    for lead in leads:
        if engine.is_suppressed(conn, lead["email"]) or lead["status"] in engine.STOP_LEAD_STATUSES:
            skipped.append(f"{lead['email']} ({lead['status']})")
            continue
        try:
            label = str(json.loads(lead["custom"] or "{}").get("variant", ""))[:40]
        except json.JSONDecodeError:
            label = ""
        cur = conn.execute("INSERT OR IGNORE INTO enrollments (campaign_id, lead_id, next_send_at, created_at, variant) "
                           "VALUES (?,?,?,?,?)", (c["id"], lead["id"], now, now, label))
        if cur.rowcount:
            added += 1
        else:
            skipped.append(f"{lead['email']} (already enrolled)")
    if added and c["status"] == "completed":
        conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (c["id"],))
    return {"enrolled": added, "skipped": len(skipped), "skipped_examples": skipped[:25]}


def unenroll(conn, campaign: str, emails: list[str]) -> dict:
    c = get_campaign_row(conn, campaign)
    n = 0
    for e in emails:
        n += conn.execute("UPDATE enrollments SET status='stopped' WHERE campaign_id=? AND status='active' AND "
                          "lead_id=(SELECT id FROM leads WHERE email=?)", (c["id"], e.lower())).rowcount
    return {"stopped": n}


def preview(conn, campaign: str, lead_email: str | None = None, step: int | None = None) -> list[dict]:
    """Render a campaign's steps for one lead (default: first enrolled lead)."""
    c = get_campaign_row(conn, campaign)
    if lead_email:
        lead = conn.execute("SELECT * FROM leads WHERE email=?", (lead_email.lower(),)).fetchone()
    else:
        lead = conn.execute("SELECT l.* FROM leads l JOIN enrollments e ON e.lead_id=l.id WHERE e.campaign_id=? "
                            "ORDER BY e.id LIMIT 1", (c["id"],)).fetchone()
    if not lead:
        raise ValueError("no lead to preview with; pass lead_email or enroll leads first")
    boxes = engine.campaign_mailboxes(conn, c)
    if not boxes:
        raise ValueError("no active mailboxes; add one first")
    mb = boxes[0]
    out, thread_subject = [], ""
    for s in conn.execute("SELECT * FROM steps WHERE campaign_id=? ORDER BY step_number", (c["id"],)):
        if step and s["step_number"] != step:
            if s["subject"].strip() and s["step_number"] < step:
                try:
                    thread_subject, _ = engine.render_step(conn, c, s, lead, mb)
                except RenderError:
                    pass
            continue
        variants = [dict(v) for v in engine.step_variants(conn, s["id"])] or [None]
        for v in variants:
            try:
                subj, body = engine.render_step(conn, c, s, lead, mb, thread_subject=thread_subject, variant=v)
                out.append({"step": s["step_number"], "variant": v["label"] if v else "", "delay_days": s["delay_days"],
                            "from": mb["email"], "to": lead["email"], "subject": subj, "body": body})
            except RenderError as e:
                out.append({"step": s["step_number"], "variant": v["label"] if v else "", "error": str(e)})
        if s["subject"].strip() and out and "subject" in out[-len(variants)]:
            thread_subject = out[-len(variants)]["subject"]
    return out


def validate_campaign(conn, campaign: str) -> dict:
    c = get_campaign_row(conn, campaign)
    problems, warnings = [], []
    steps = conn.execute("SELECT * FROM steps WHERE campaign_id=? ORDER BY step_number", (c["id"],)).fetchall()
    if not steps:
        problems.append("no sequence steps")
    if not engine.campaign_mailboxes(conn, c):
        problems.append("no active mailboxes")
    if not get_setting(conn, "physical_address").strip():
        problems.append("physical_address setting is empty (required by CAN-SPAM)")
    enrolled = conn.execute("SELECT l.* FROM leads l JOIN enrollments e ON e.lead_id=l.id "
                            "WHERE e.campaign_id=? AND e.status='active'", (c["id"],)).fetchall()
    if not enrolled:
        warnings.append("no active enrollments")
    boxes = engine.campaign_mailboxes(conn, c)
    bad = []
    if steps and boxes:
        for lead in enrolled:
            for s in steps:
                try:
                    for v in [dict(x) for x in engine.step_variants(conn, s["id"])] or [None]:
                        engine.render_step(conn, c, s, lead, boxes[0], variant=v)
                except RenderError as e:
                    bad.append(f"{lead['email']} step {s['step_number']}: {e}")
                    break
    if bad:
        warnings.append(f"{len(bad)} lead(s) are missing merge fields and will be skipped (status=error)")
    capacity = sum(engine.mailbox_effective_limit(m, utcnow()) for m in boxes)
    return {"ok": not problems, "problems": problems, "warnings": warnings, "render_errors": bad[:25],
            "enrolled_active": len(enrolled), "mailbox_capacity_today": capacity,
            "campaign_daily_limit": c["daily_limit"]}


def set_status(conn, campaign: str, status: str) -> dict:
    c = get_campaign_row(conn, campaign)
    if status == "active":
        v = validate_campaign(conn, campaign)
        if not v["ok"]:
            raise ValueError("cannot start: " + "; ".join(v["problems"]))
    conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, c["id"]))
    return campaign_detail(conn, c["id"])


def campaign_stats(conn, cid: int) -> dict:
    q = lambda sql, *a: conn.execute(sql, (cid, *a)).fetchone()[0]  # noqa: E731
    enr = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM enrollments WHERE campaign_id=? GROUP BY status", (cid,))}
    contacted = q("SELECT COUNT(DISTINCT lead_id) FROM messages WHERE campaign_id=? AND direction='out' "
                  "AND kind='sequence'")
    replied = q("SELECT COUNT(DISTINCT lead_id) FROM messages WHERE campaign_id=? AND direction='in' "
                "AND kind='reply'")
    bounced = enr.get("bounced", 0)
    return {
        "enrolled": sum(enr.values()), "by_status": enr,
        "emails_sent": q("SELECT COUNT(*) FROM messages WHERE campaign_id=? AND direction='out' AND kind='sequence'"),
        "leads_contacted": contacted, "leads_replied": replied,
        "reply_rate": round(replied / contacted, 3) if contacted else 0.0,
        "bounce_rate": round(bounced / contacted, 3) if contacted else 0.0,
        "unsubscribed": enr.get("unsubscribed", 0),
        "per_step_sent": {r["step_number"]: r["n"] for r in conn.execute(
            "SELECT step_number, COUNT(*) n FROM messages WHERE campaign_id=? AND direction='out' "
            "AND kind='sequence' GROUP BY step_number", (cid,))},
    }


def campaign_detail(conn, campaign) -> dict:
    c = get_campaign_row(conn, campaign)
    d = dict(c)
    d["steps"] = []
    for s in conn.execute("SELECT id, step_number, delay_days, subject, body FROM steps WHERE campaign_id=? ORDER BY step_number", (c["id"],)):
        row = dict(s)
        row["variants"] = [{k: v[k] for k in ("label", "subject", "body", "active")} for v in engine.step_variants(conn, s["id"], False)]
        row.pop("id")
        d["steps"].append(row)
    d["mailboxes"] = [m["email"] for m in engine.campaign_mailboxes(conn, c)]
    d["stats"] = campaign_stats(conn, c["id"])
    return d


def overview(conn) -> dict:
    camps = []
    for c in conn.execute("SELECT * FROM campaigns ORDER BY id DESC"):
        s = campaign_stats(conn, c["id"])
        camps.append({"name": c["name"], "status": c["status"], "enrolled": s["enrolled"],
                      "sent": s["emails_sent"], "replied": s["leads_replied"], "reply_rate": s["reply_rate"],
                      "bounce_rate": s["bounce_rate"]})
    return {
        "campaigns": camps,
        "mailboxes": [{k: m[k] for k in ("email", "active", "sent_today", "limit_today")}
                      for m in list_mailboxes(conn)],
        "leads_by_status": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM leads GROUP BY status")},
        "unhandled_replies": conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND kind='reply' AND handled=0").fetchone()[0],
        "physical_address_set": bool(get_setting(conn, "physical_address").strip()),
    }


# ---------------------------------------------------------------- inbox

def list_replies(conn, unhandled_only: bool = True, limit: int = 25) -> list[dict]:
    q = ("SELECT m.id, m.kind, m.from_addr, m.subject, m.body, m.sent_at, m.handled, l.first_name, l.last_name, "
         "l.company, l.status AS lead_status, c.name AS campaign FROM messages m "
         "LEFT JOIN leads l ON l.id=m.lead_id LEFT JOIN campaigns c ON c.id=m.campaign_id "
         "WHERE m.direction='in' AND m.kind IN ('reply','auto_reply','unsubscribe')")
    if unhandled_only:
        q += " AND m.handled=0"
    return [dict(r) for r in conn.execute(q + " ORDER BY m.id DESC LIMIT ?", (limit,))]


def get_thread(conn, lead_email: str) -> dict:
    lead = conn.execute("SELECT * FROM leads WHERE email=?", (lead_email.lower(),)).fetchone()
    if not lead:
        raise ValueError(f"no lead {lead_email}")
    msgs = [dict(r) for r in conn.execute(
        "SELECT id, direction, kind, from_addr, to_addr, subject, body, sent_at, step_number FROM messages "
        "WHERE lead_id=? ORDER BY id", (lead["id"],))]
    return {"lead": dict(lead), "messages": msgs}


def mark_handled(conn, message_ids: list[int]) -> dict:
    n = 0
    for i in message_ids:
        n += conn.execute("UPDATE messages SET handled=1 WHERE id=? AND direction='in'", (i,)).rowcount
    return {"marked": n}


def list_suppression(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM suppression ORDER BY created_at DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- self-optimizing A/B

def auto_optimize(conn, min_sent: int = 30) -> list[dict]:
    """Retire losing step 1 variants once a winner is confident, and record why.

    Runs hourly from the worker. Each change is written to `decisions` with the numbers
    behind it so Jarvis can report it (and push it to the Jarvis Log in Notion).
    """
    made = []
    for c in conn.execute("SELECT * FROM campaigns WHERE status='active'").fetchall():
        rep = ab_report(conn, c["name"], min_sent)
        firsts = [v for v in rep["variants"] if v["step"] == 1 and v["active"]]
        if any(v.get("per_lead") for v in firsts):
            continue  # labels come from the writer (Cowork); report only, it picks the next test
        if rep["leader"] and "confident" in rep["verdict"] and len(firsts) > 1:
            losers = [v["label"] for v in firsts if v["label"] != rep["leader"]]
            for label in losers:
                retire_variant(conn, c["name"], 1, label)
            summary = (f"{c['name']}: variant {rep['leader']} won ({rep['verdict']}). Retired {', '.join(losers)}. "
                       f"All new leads now get {rep['leader']}. Add a new challenger to keep testing.")
            made.append(_decide(conn, c["name"], "ab_winner", summary, {"report": rep, "retired": losers}))
    return made


def _decide(conn, campaign: str, kind: str, summary: str, data: dict) -> dict:
    cur = conn.execute("INSERT INTO decisions (at, campaign, kind, summary, data) VALUES (?,?,?,?,?)",
                       (iso(utcnow()), campaign, kind, summary, json.dumps(data, default=str)))
    return {"id": cur.lastrowid, "campaign": campaign, "kind": kind, "summary": summary}


def decisions(conn, unreported_only: bool = False, mark_reported: bool = False, limit: int = 50) -> list[dict]:
    q = "SELECT * FROM decisions" + (" WHERE reported=0" if unreported_only else "") + " ORDER BY id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(q, (limit,))]
    for r in rows:
        r["data"] = json.loads(r["data"] or "{}")
    if mark_reported and rows:
        conn.execute(f"UPDATE decisions SET reported=1 WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
    return rows


HANDOFF_STEPS = [
    {"subject": "{{subject_1}}", "body": "{{body_1}}", "delay_days": 0},
    {"subject": "", "body": "{{body_2}}", "delay_days": 2},
    {"subject": "", "body": "{{body_3}}", "delay_days": 5},
    {"subject": "", "body": "{{body_4}}", "delay_days": 7},
]


def ensure_handoff_campaign(conn, name: str = "Cowork outreach") -> dict:
    """The campaign Cowork's written emails flow into: each lead carries its own four touches
    (subject_1, body_1..body_4) as merge fields, so the engine only distributes and follows up."""
    if not conn.execute("SELECT 1 FROM campaigns WHERE name=?", (name,)).fetchone():
        create_campaign(conn, name, daily_limit=400)
        set_sequence(conn, name, HANDOFF_STEPS)
    return campaign_detail(conn, name)


def handoff(conn, leads: list[dict], campaign: str = "Cowork outreach") -> dict:
    """Import leads that Cowork researched and wrote for, and enroll them. Each lead needs email,
    subject_1 and body_1..body_4; optional variant (A/B label), first_name, company, hook, notion_url."""
    ensure_handoff_campaign(conn, campaign)
    ok, bad = [], []
    for ld in leads:
        missing = [k for k in ("email", "subject_1", "body_1", "body_2", "body_3", "body_4") if not str(ld.get(k, "")).strip()]
        (bad.append({"email": ld.get("email"), "missing": missing}) if missing else ok.append(ld))
    up = upsert_leads(conn, ok, tags="cowork") if ok else {"added": 0, "updated": 0}
    en = enroll(conn, campaign, emails=[x["email"] for x in ok]) if ok else {"enrolled": 0, "skipped": 0}
    return {"imported": up, "enrolled": en, "rejected": bad}
