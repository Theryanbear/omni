"""Template rendering: {{merge_fields|fallback}} and {spin|tax}."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import sqlite3

from .db import get_setting

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|([^}]*))?\}\}")
# Single-brace groups with at least one '|', not part of a {{ }} pair.
SPIN_RE = re.compile(r"(?<!\{)\{([^{}]*\|[^{}]*)\}(?!\})")


class RenderError(ValueError):
    pass


def lead_fields(lead: sqlite3.Row | dict, mailbox: sqlite3.Row | dict | None = None) -> dict:
    lead = dict(lead)
    fields = {k: lead.get(k, "") for k in
              ("email", "first_name", "last_name", "company", "title", "phone", "website", "city")}
    try:
        fields.update({str(k): "" if v is None else str(v)
                       for k, v in json.loads(lead.get("custom") or "{}").items()})
    except json.JSONDecodeError:
        pass
    if mailbox:
        mailbox = dict(mailbox)
        name = mailbox.get("from_name", "")
        fields["sender_name"] = name
        fields["sender_first_name"] = name.split(" ")[0] if name else ""
        fields["sender_email"] = mailbox.get("email", "")
    return fields


def spin(text: str, rng: random.Random) -> str:
    # Resolve innermost groups repeatedly so nested spintax works.
    for _ in range(20):
        new = SPIN_RE.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text


def render(template: str, fields: dict, seed: str = "") -> str:
    """Render a template. Raises RenderError if a merge field is empty and has no fallback."""
    rng = random.Random(seed)
    text = spin(template, rng)
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key, fallback = m.group(1), m.group(2)
        val = str(fields.get(key, "") or "").strip()
        if val:
            return val
        if fallback is not None:
            return fallback
        missing.append(key)
        return m.group(0)

    out = VAR_RE.sub(sub, text)
    if missing:
        raise RenderError("missing merge field(s) with no fallback: " + ", ".join(sorted(set(missing))))
    return out


def unsubscribe_token(conn: sqlite3.Connection, email: str) -> str:
    secret = get_setting(conn, "secret").encode()
    return hmac.new(secret, email.lower().encode(), hashlib.sha256).hexdigest()[:32]


def unsubscribe_url(conn: sqlite3.Connection, email: str) -> str:
    base = get_setting(conn, "unsubscribe_base_url").rstrip("/")
    if not base:
        return ""
    from urllib.parse import quote
    return f"{base}/u?e={quote(email)}&t={unsubscribe_token(conn, email)}"


def footer(conn: sqlite3.Connection, mailbox: sqlite3.Row | dict, email: str) -> str:
    mailbox = dict(mailbox)
    address = get_setting(conn, "physical_address").strip()
    url = unsubscribe_url(conn, email)
    tmpl = get_setting(conn, "footer_template")
    return tmpl.format(
        signature=mailbox.get("signature") or mailbox.get("from_name", ""),
        company_name=get_setting(conn, "company_name"),
        address_line=("\n" + address) if address else "",
        business_lines="\n".join(x for x in (get_setting(conn, "company_name").strip(), address) if x),
        unsubscribe_line=(f"\nOr unsubscribe here: {url}") if url else "",
    )
