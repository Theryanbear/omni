"""SQLite storage: schema, connection helpers and time utilities."""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
    id                INTEGER PRIMARY KEY,
    email             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    from_name         TEXT NOT NULL DEFAULT '',
    smtp_host         TEXT NOT NULL,
    smtp_port         INTEGER NOT NULL DEFAULT 587,
    smtp_user         TEXT NOT NULL,
    imap_host         TEXT NOT NULL DEFAULT '',
    imap_port         INTEGER NOT NULL DEFAULT 993,
    password_env      TEXT NOT NULL,          -- name of env var holding the password
    daily_limit       INTEGER NOT NULL DEFAULT 30,
    warmup_start      INTEGER NOT NULL DEFAULT 10, -- sends/day on day 0
    warmup_increment  INTEGER NOT NULL DEFAULT 3,  -- extra sends/day per day; 0 = no ramp
    min_gap_seconds   INTEGER NOT NULL DEFAULT 120,
    max_gap_seconds   INTEGER NOT NULL DEFAULT 420,
    signature         TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,
    next_available_at TEXT NOT NULL DEFAULT '',
    imap_last_uid     INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name  TEXT NOT NULL DEFAULT '',
    company    TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    website    TEXT NOT NULL DEFAULT '',
    city       TEXT NOT NULL DEFAULT '',
    custom     TEXT NOT NULL DEFAULT '{}',      -- JSON object of extra merge fields
    tags       TEXT NOT NULL DEFAULT '',        -- comma separated
    status     TEXT NOT NULL DEFAULT 'new',     -- new|contacted|replied|interested|not_interested|meeting|won|bounced|unsubscribed
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    entry      TEXT PRIMARY KEY COLLATE NOCASE, -- full email or '@domain.com'
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status        TEXT NOT NULL DEFAULT 'draft', -- draft|active|paused|completed
    timezone      TEXT NOT NULL DEFAULT 'America/New_York',
    send_days     TEXT NOT NULL DEFAULT '1,2,3,4,5', -- ISO weekdays, 1=Mon
    window_start  TEXT NOT NULL DEFAULT '08:30',
    window_end    TEXT NOT NULL DEFAULT '16:30',
    daily_limit   INTEGER NOT NULL DEFAULT 100,
    mailbox_ids   TEXT NOT NULL DEFAULT '',       -- comma separated; empty = all active
    stop_on_reply INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    delay_days  REAL NOT NULL DEFAULT 0,  -- wait after previous step
    subject     TEXT NOT NULL DEFAULT '', -- empty on follow-ups = reply in same thread
    body        TEXT NOT NULL,
    UNIQUE (campaign_id, step_number)
);

CREATE TABLE IF NOT EXISTS enrollments (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    mailbox_id      INTEGER REFERENCES mailboxes(id),
    current_step    INTEGER NOT NULL DEFAULT 0,   -- number of steps already sent
    next_send_at    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active', -- active|completed|replied|bounced|unsubscribed|stopped|error
    thread_subject  TEXT NOT NULL DEFAULT '',
    thread_refs     TEXT NOT NULL DEFAULT '',     -- space separated Message-IDs
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (campaign_id, lead_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    direction     TEXT NOT NULL,               -- out|in
    kind          TEXT NOT NULL,               -- sequence|manual|reply|auto_reply|bounce|unsubscribe
    campaign_id   INTEGER,
    enrollment_id INTEGER,
    lead_id       INTEGER,
    mailbox_id    INTEGER,
    step_number   INTEGER,
    message_id    TEXT NOT NULL DEFAULT '',
    in_reply_to   TEXT NOT NULL DEFAULT '',
    from_addr     TEXT NOT NULL DEFAULT '',
    to_addr       TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    sent_at       TEXT NOT NULL,
    handled       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_out ON messages(direction, mailbox_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_enroll_due ON enrollments(status, next_send_at);
"""

DEFAULT_SETTINGS = {
    "company_name": "",
    "physical_address": "",  # required by CAN-SPAM before anything sends
    "unsubscribe_base_url": "",  # e.g. https://mail.example.com  (optional one-click link)
    "footer_template": (
        "\n\n{signature}\n\n--\n{business_lines}\n"
        "Not interested? Just reply \"unsubscribe\" and I won't email you again.{unsubscribe_line}"
    ),
    "unsubscribe_keywords": "unsubscribe,remove me,take me off,stop emailing,opt out,opt-out",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def default_db_path() -> Path:
    return Path(os.environ.get("OMNI_MAIL_DB", "data/omni_mail.db"))


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader so mailbox passwords can live outside the database."""
    candidates = [path] if path else [Path(os.environ.get("OMNI_MAIL_ENV", ".env"))]
    for p in candidates:
        if p and p.is_file():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('secret', ?)",
        (secrets.token_hex(32),),
    )
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
