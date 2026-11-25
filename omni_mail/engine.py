"""Scheduler / sender and inbox processor."""

from __future__ import annotations

import random
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from zoneinfo import ZoneInfo

from .db import get_setting, iso, parse_iso, utcnow
from .render import RenderError, footer, lead_fields, render, unsubscribe_url
from .transport import Transport, TransportError

STOP_LEAD_STATUSES = {"replied", "interested", "not_interested", "meeting", "won", "bounced", "unsubscribed"}


# ---------------------------------------------------------------- helpers

def is_suppressed(conn: sqlite3.Connection, addr: str) -> bool:
    addr = addr.strip().lower()
    domain = "@" + addr.split("@")[-1]
    return conn.execute(
        "SELECT 1 FROM suppression WHERE entry IN (?, ?)", (addr, domain)
    ).fetchone() is not None


def suppress(conn: sqlite3.Connection, entry: str, reason: str, now: datetime | None = None) -> None:
    entry = entry.strip().lower()
    conn.execute(
        "INSERT OR IGNORE INTO suppression(entry, reason, created_at) VALUES (?, ?, ?)",
        (entry, reason, iso(now or utcnow())),
    )
    if entry.startswith("@"):
        rows = conn.execute("SELECT id FROM leads WHERE email LIKE ?", ("%" + entry,)).fetchall()
    else:
        rows = conn.execute("SELECT id FROM leads WHERE email = ?", (entry,)).fetchall()
    for r in rows:
        stop_lead(conn, r["id"], "unsubscribed", lead_status="unsubscribed")


def stop_lead(conn: sqlite3.Connection, lead_id: int, enrollment_status: str,
              lead_status: str | None = None, only_stop_on_reply: bool = False) -> None:
    q = ("UPDATE enrollments SET status = ? WHERE lead_id = ? AND status = 'active'")
    args: list = [enrollment_status, lead_id]
    if only_stop_on_reply:
        q += " AND campaign_id IN (SELECT id FROM campaigns WHERE stop_on_reply = 1)"
    conn.execute(q, args)
    if lead_status:
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (lead_status, lead_id))


def mailbox_effective_limit(mb: sqlite3.Row, now: datetime) -> int:
    limit = int(mb["daily_limit"])
    if int(mb["warmup_increment"]) > 0:
        days = max(0, (now - parse_iso(mb["created_at"])).days)
        limit = min(limit, int(mb["warmup_start"]) + int(mb["warmup_increment"]) * days)
    return limit


def utc_day_start(now: datetime) -> str:
    return iso(datetime.combine(now.date(), time(0), tzinfo=timezone.utc))


def mailbox_sent_today(conn, mailbox_id: int, now: datetime) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction='out' AND mailbox_id=? AND sent_at>=?",
        (mailbox_id, utc_day_start(now)),
    ).fetchone()[0]


def campaign_local_day_start(camp, now: datetime) -> str:
    tz = ZoneInfo(camp["timezone"])
    local = now.astimezone(tz)
    return iso(datetime.combine(local.date(), time(0), tzinfo=tz))


def campaign_sent_today(conn, camp, now: datetime) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction='out' AND kind='sequence' "
        "AND campaign_id=? AND sent_at>=?",
        (camp["id"], campaign_local_day_start(camp, now)),
    ).fetchone()[0]


def in_send_window(camp, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(camp["timezone"]))
    days = {int(d) for d in camp["send_days"].split(",") if d.strip()}
    if local.isoweekday() not in days:
        return False
    start = time.fromisoformat(camp["window_start"])
    end = time.fromisoformat(camp["window_end"])
    return start <= local.time().replace(tzinfo=None) < end


def campaign_mailboxes(conn, camp) -> list[sqlite3.Row]:
    ids = [int(x) for x in camp["mailbox_ids"].split(",") if x.strip()]
    if ids:
        marks = ",".join("?" * len(ids))
        return conn.execute(f"SELECT * FROM mailboxes WHERE active=1 AND id IN ({marks})", ids).fetchall()
    return conn.execute("SELECT * FROM mailboxes WHERE active=1").fetchall()


def build_email(conn, mailbox, lead, subject: str, body: str,
                in_reply_to: str = "", references: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox["from_name"], mailbox["email"]))
    msg["To"] = formataddr(((lead["first_name"] + " " + lead["last_name"]).strip(), lead["email"]))
    msg["Subject"] = subject
    domain = mailbox["email"].split("@")[-1]
    msg["Message-ID"] = make_msgid(domain=domain)
    from email.utils import formatdate
    msg["Date"] = formatdate(localtime=False)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references or in_reply_to).strip()
    unsub = [f"<mailto:{mailbox['email']}?subject=unsubscribe>"]
    url = unsubscribe_url(conn, lead["email"])
    if url.startswith("https://"):
        unsub.insert(0, f"<{url}>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["List-Unsubscribe"] = ", ".join(unsub)
    msg.set_content(body)
    return msg


def render_step(conn, camp, step, lead, mailbox, enrollment_id: int | str = "preview",
                thread_subject: str = "") -> tuple[str, str]:
    """Return (subject, body) for a step. Raises RenderError."""
    fields = lead_fields(lead, mailbox)
    seed = f"{camp['id']}:{lead['id']}:{step['step_number']}"
    if step["subject"].strip():
        subject = render(step["subject"], fields, seed + ":s")
    else:
        base = thread_subject or "(no subject)"
        subject = base if base.lower().startswith("re:") else "Re: " + base
    body = render(step["body"], fields, seed + ":b") + footer(conn, mailbox, lead["email"])
    return subject, body


def log_message(conn, **kw) -> int:
    kw.setdefault("sent_at", iso(utcnow()))
    cols = ",".join(kw)
    cur = conn.execute(f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * len(kw))})",
                       list(kw.values()))
    return cur.lastrowid


# ---------------------------------------------------------------- sending

def send_tick(conn: sqlite3.Connection, transport: Transport, now: datetime | None = None,
              dry_run: bool = False, rng: random.Random | None = None) -> dict:
    """Send whatever is due right now. Each mailbox sends at most one email per tick.

    Run this every minute (cron, `omni-mail run`, or the MCP tool).
    """
    now = now or utcnow()
    rng = rng or random.Random()
    report = {"sent": [], "skipped": [], "errors": [], "dry_run": dry_run}

    if not get_setting(conn, "physical_address").strip():
        report["errors"].append("physical_address setting is empty; CAN-SPAM requires a postal "
                                "address in every commercial email. Set it before sending.")
        return report

    used_mailboxes: set[int] = set()
    campaigns = conn.execute("SELECT * FROM campaigns WHERE status='active'").fetchall()
    for camp in campaigns:
        if not in_send_window(camp, now):
            report["skipped"].append(f"{camp['name']}: outside send window")
            continue
        remaining = int(camp["daily_limit"]) - campaign_sent_today(conn, camp, now)
        if remaining <= 0:
            report["skipped"].append(f"{camp['name']}: campaign daily limit reached")
            continue
        steps = {s["step_number"]: s for s in conn.execute(
            "SELECT * FROM steps WHERE campaign_id=? ORDER BY step_number", (camp["id"],))}
        if not steps:
            continue
        boxes = {m["id"]: m for m in campaign_mailboxes(conn, camp)}
        if not boxes:
            report["errors"].append(f"{camp['name']}: no active mailboxes")
            continue

        due = conn.execute(
            "SELECT e.*, l.email AS lead_email, l.status AS lead_status FROM enrollments e "
            "JOIN leads l ON l.id = e.lead_id "
            "WHERE e.campaign_id=? AND e.status='active' AND e.next_send_at<=? "
            "ORDER BY e.current_step DESC, e.next_send_at LIMIT 200",
            (camp["id"], iso(now)),
        ).fetchall()

        for enr in due:
            if remaining <= 0:
                break
            available = [m for m in boxes.values()
                         if m["id"] not in used_mailboxes
                         and (not m["next_available_at"] or m["next_available_at"] <= iso(now))
                         and mailbox_sent_today(conn, m["id"], now) < mailbox_effective_limit(m, now)]
            if not available:
                break

            lead = conn.execute("SELECT * FROM leads WHERE id=?", (enr["lead_id"],)).fetchone()
            if is_suppressed(conn, lead["email"]):
                stop_lead(conn, lead["id"], "unsubscribed", "unsubscribed")
                continue
            if lead["status"] in STOP_LEAD_STATUSES:
                conn.execute("UPDATE enrollments SET status='stopped' WHERE id=?", (enr["id"],))
                continue

            step_no = enr["current_step"] + 1
            step = steps.get(step_no)
            if step is None:
                conn.execute("UPDATE enrollments SET status='completed' WHERE id=?", (enr["id"],))
                continue

            if enr["mailbox_id"]:
                mb = next((m for m in available if m["id"] == enr["mailbox_id"]), None)
                if mb is None:
                    continue  # its sticky mailbox is busy/capped; try later
            else:
                mb = min(available, key=lambda m: mailbox_sent_today(conn, m["id"], now))

            try:
                subject, body = render_step(conn, camp, step, lead, mb, enr["id"], enr["thread_subject"])
            except RenderError as e:
                conn.execute("UPDATE enrollments SET status='error', last_error=? WHERE id=?",
                             (str(e), enr["id"]))
                report["errors"].append(f"{lead['email']}: {e}")
                continue

            new_thread = bool(step["subject"].strip())
            refs = "" if new_thread else enr["thread_refs"]
            last_ref = refs.split()[-1] if refs else ""
            msg = build_email(conn, mb, lead, subject, body, in_reply_to=last_ref, references=refs)

            if dry_run:
                report["sent"].append({"to": lead["email"], "from": mb["email"], "campaign": camp["name"],
                                       "step": step_no, "subject": subject})
                used_mailboxes.add(mb["id"])
                remaining -= 1
                continue

            try:
                transport.send(mb, msg)
            except TransportError as e:
                err = str(e)
                if "recipient refused" in err:
                    conn.execute("UPDATE leads SET status='bounced' WHERE id=?", (lead["id"],))
                    stop_lead(conn, lead["id"], "bounced")
                    suppress_bounce(conn, lead["email"], now)
                else:
                    conn.execute("UPDATE enrollments SET last_error=? WHERE id=?", (err, enr["id"]))
                    # back the mailbox off for 15 minutes on transport failures
                    conn.execute("UPDATE mailboxes SET next_available_at=? WHERE id=?",
                                 (iso(now + timedelta(minutes=15)), mb["id"]))
                report["errors"].append(f"{lead['email']} via {mb['email']}: {err}")
                used_mailboxes.add(mb["id"])
                continue

            mid = msg["Message-ID"]
            log_message(conn, direction="out", kind="sequence", campaign_id=camp["id"],
                        enrollment_id=enr["id"], lead_id=lead["id"], mailbox_id=mb["id"],
                        step_number=step_no, message_id=mid, in_reply_to=last_ref,
                        from_addr=mb["email"], to_addr=lead["email"], subject=subject, body=body,
                        sent_at=iso(now))
            thread_subject = subject if new_thread else (enr["thread_subject"] or subject)
            thread_refs = (mid if new_thread else (refs + " " + mid)).strip()
            nxt = steps.get(step_no + 1)
            if nxt is None:
                conn.execute("UPDATE enrollments SET current_step=?, mailbox_id=?, status='completed', "
                             "thread_subject=?, thread_refs=?, last_error='' WHERE id=?",
                             (step_no, mb["id"], thread_subject, thread_refs, enr["id"]))
            else:
                when = now + timedelta(days=float(nxt["delay_days"]))
                conn.execute("UPDATE enrollments SET current_step=?, mailbox_id=?, next_send_at=?, "
                             "thread_subject=?, thread_refs=?, last_error='' WHERE id=?",
                             (step_no, mb["id"], iso(when), thread_subject, thread_refs, enr["id"]))
            if lead["status"] == "new":
                conn.execute("UPDATE leads SET status='contacted' WHERE id=?", (lead["id"],))
            gap = rng.randint(int(mb["min_gap_seconds"]), max(int(mb["min_gap_seconds"]), int(mb["max_gap_seconds"])))
            conn.execute("UPDATE mailboxes SET next_available_at=? WHERE id=?",
                         (iso(now + timedelta(seconds=gap)), mb["id"]))
            used_mailboxes.add(mb["id"])
            remaining -= 1
            report["sent"].append({"to": lead["email"], "from": mb["email"], "campaign": camp["name"],
                                   "step": step_no, "subject": subject})

    # Mark campaigns with nothing left to do as completed.
    conn.execute(
        "UPDATE campaigns SET status='completed' WHERE status='active' AND "
        "EXISTS (SELECT 1 FROM enrollments WHERE campaign_id=campaigns.id) AND "
        "NOT EXISTS (SELECT 1 FROM enrollments WHERE campaign_id=campaigns.id AND status='active')"
    )
    return report


def suppress_bounce(conn, addr: str, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO suppression(entry, reason, created_at) VALUES (?, 'bounced', ?)",
                 (addr.lower(), iso(now)))


# ---------------------------------------------------------------- inbox

AUTO_SUBJECT_RE = re.compile(r"^(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|"
                             r"away from|on vacation|i am out|i'm out)", re.I)
BOUNCE_FROM_RE = re.compile(r"(mailer-daemon|postmaster|mail delivery)", re.I)
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def message_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return str(msg.get_payload())
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply."""
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:\s*$", line) or line.strip() == "-----Original Message-----":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _find_lead_for_bounce(conn, msg, text: str):
    candidates = []
    for h in ("X-Failed-Recipients", "Final-Recipient", "Original-Recipient"):
        if msg.get(h):
            candidates += EMAIL_RE.findall(msg.get(h))
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "message/delivery-status":
                candidates += EMAIL_RE.findall(str(part))
    candidates += EMAIL_RE.findall(text)
    for c in candidates:
        lead = conn.execute("SELECT * FROM leads WHERE email=?", (c.lower(),)).fetchone()
        if lead:
            return lead
    return None


def process_incoming(conn, mailbox, msg, now: datetime) -> dict | None:
    from_name, from_addr = parseaddr(msg.get("From", ""))
    from_addr = from_addr.lower()
    subject = str(msg.get("Subject", ""))
    text = message_text(msg)
    mid = (msg.get("Message-ID") or "").strip()
    if mid and conn.execute("SELECT 1 FROM messages WHERE message_id=? AND direction='in'", (mid,)).fetchone():
        return None

    # Bounces
    is_report = msg.get_content_type() == "multipart/report"
    if is_report or BOUNCE_FROM_RE.search(from_addr) or BOUNCE_FROM_RE.search(from_name):
        lead = _find_lead_for_bounce(conn, msg, text)
        if lead:
            conn.execute("UPDATE leads SET status='bounced' WHERE id=?", (lead["id"],))
            stop_lead(conn, lead["id"], "bounced")
            suppress_bounce(conn, lead["email"], now)
            log_message(conn, direction="in", kind="bounce", lead_id=lead["id"], mailbox_id=mailbox["id"],
                        message_id=mid, from_addr=from_addr, to_addr=mailbox["email"], subject=subject,
                        body=text[:4000], sent_at=iso(now), handled=1)
            return {"type": "bounce", "lead": lead["email"]}
        return None

    # Match to a lead: thread headers first, then sender address.
    refs = " ".join(filter(None, [msg.get("In-Reply-To", ""), msg.get("References", "")]))
    ref_ids = re.findall(r"<[^>]+>", refs)
    lead = None
    camp_id = None
    for rid in reversed(ref_ids):
        row = conn.execute("SELECT lead_id, campaign_id FROM messages WHERE message_id=? AND direction='out'",
                           (rid,)).fetchone()
        if row:
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
            camp_id = row["campaign_id"]
            break
    if lead is None:
        lead = conn.execute("SELECT * FROM leads WHERE email=?", (from_addr,)).fetchone()
    if lead is None:
        return None  # not campaign related; leave it alone

    new_text = strip_quoted(text)
    auto = (msg.get("Auto-Submitted", "no").lower() not in ("", "no")
            or msg.get("X-Autoreply") or msg.get("X-Autorespond")
            or (msg.get("Precedence", "").lower() in ("auto_reply", "bulk", "junk"))
            or AUTO_SUBJECT_RE.search(re.sub(r"^(re|fwd?):\s*", "", subject, flags=re.I)))
    keywords = [k.strip().lower() for k in get_setting(conn, "unsubscribe_keywords").split(",") if k.strip()]
    first_chunk = (subject + "\n" + new_text[:300]).lower()
    unsub = any(k in first_chunk for k in keywords)

    if unsub:
        kind = "unsubscribe"
        suppress(conn, lead["email"], "replied unsubscribe", now)
    elif auto:
        kind = "auto_reply"
    else:
        kind = "reply"
        stop_lead(conn, lead["id"], "replied", only_stop_on_reply=True)
        if lead["status"] in ("new", "contacted"):
            conn.execute("UPDATE leads SET status='replied' WHERE id=?", (lead["id"],))

    log_message(conn, direction="in", kind=kind, campaign_id=camp_id, lead_id=lead["id"],
                mailbox_id=mailbox["id"], message_id=mid, in_reply_to=msg.get("In-Reply-To", ""),
                from_addr=from_addr, to_addr=mailbox["email"], subject=subject, body=new_text[:20000],
                sent_at=iso(now), handled=0 if kind == "reply" else 1)
    return {"type": kind, "lead": lead["email"], "subject": subject}


def sync_inbox(conn, transport: Transport, now: datetime | None = None) -> dict:
    now = now or utcnow()
    report = {"processed": [], "errors": []}
    for mb in conn.execute("SELECT * FROM mailboxes WHERE active=1 AND imap_host!=''").fetchall():
        try:
            items = transport.fetch_new(mb, int(mb["imap_last_uid"]))
        except TransportError as e:
            report["errors"].append(str(e))
            continue
        max_uid = int(mb["imap_last_uid"])
        for uid, msg in items:
            max_uid = max(max_uid, uid)
            res = process_incoming(conn, mb, msg, now)
            if res:
                report["processed"].append(res)
        conn.execute("UPDATE mailboxes SET imap_last_uid=? WHERE id=?", (max_uid, mb["id"]))
    return report


def send_manual_reply(conn, transport: Transport, lead_email: str, body: str,
                      now: datetime | None = None) -> dict:
    """Reply to a lead in the existing thread, from the mailbox they were emailed from."""
    now = now or utcnow()
    lead = conn.execute("SELECT * FROM leads WHERE email=?", (lead_email.lower(),)).fetchone()
    if not lead:
        raise ValueError(f"no lead {lead_email}")
    if is_suppressed(conn, lead["email"]) and lead["status"] in ("unsubscribed", "bounced"):
        raise ValueError(f"{lead_email} is suppressed ({lead['status']}); not sending")
    last_in = conn.execute("SELECT * FROM messages WHERE lead_id=? AND direction='in' AND kind IN "
                           "('reply','auto_reply') ORDER BY id DESC LIMIT 1", (lead["id"],)).fetchone()
    last_out = conn.execute("SELECT * FROM messages WHERE lead_id=? AND direction='out' ORDER BY id DESC LIMIT 1",
                            (lead["id"],)).fetchone()
    anchor = last_in or last_out
    if anchor is None:
        raise ValueError("no existing thread with this lead; enroll them in a campaign instead")
    mb = conn.execute("SELECT * FROM mailboxes WHERE id=?", (anchor["mailbox_id"],)).fetchone()
    subject = anchor["subject"] if anchor["subject"].lower().startswith("re:") else "Re: " + anchor["subject"]
    refs = " ".join(r["message_id"] for r in conn.execute(
        "SELECT message_id FROM messages WHERE lead_id=? AND message_id!='' ORDER BY id", (lead["id"],)))
    sig = mb["signature"] or mb["from_name"]
    full = body.rstrip() + ("\n\n" + sig if sig and sig not in body else "")
    msg = build_email(conn, mb, lead, subject, full, in_reply_to=anchor["message_id"], references=refs)
    transport.send(mb, msg)
    log_message(conn, direction="out", kind="manual", campaign_id=anchor["campaign_id"], lead_id=lead["id"],
                mailbox_id=mb["id"], message_id=msg["Message-ID"], in_reply_to=anchor["message_id"],
                from_addr=mb["email"], to_addr=lead["email"], subject=subject, body=full, sent_at=iso(now))
    conn.execute("UPDATE messages SET handled=1 WHERE lead_id=? AND direction='in'", (lead["id"],))
    return {"sent_to": lead["email"], "from": mb["email"], "subject": subject}
