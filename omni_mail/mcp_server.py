"""MCP server: lets Claude run every part of your cold email operation.

Register it with Claude Code (see README / .mcp.json) and Claude gets tools to build
campaigns, import leads, preview, launch, read replies and answer them.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import engine, service
from .db import connect, load_dotenv
from .transport import FakeTransport, RealTransport

load_dotenv()

INSTRUCTIONS = """\
OMNI Mail is a self-hosted cold email system (an Instantly replacement).
Typical flow: get_overview -> add leads (import_leads_csv / add_leads) -> create_campaign ->
set_sequence -> enroll_leads -> preview_campaign -> validate_campaign -> start_campaign.
Sending happens on a schedule (the `omni-mail run` worker); run_send_tick is for manual nudges.
Replies: sync_inbox -> list_replies -> get_thread -> send_reply / update_lead.
Rules: always preview before starting a campaign, never send a reply the user hasn't approved
unless they told you to, and never re-contact unsubscribed or bounced leads.
Merge fields: {{first_name}}, {{company}}, {{first_name|there}} (fallback), any CSV column,
{{sender_first_name}}. Spintax: {Hi|Hey|Hello}. Leave subject empty on follow-ups to reply in-thread.
"""

mcp = _Server("omni-mail", instructions=INSTRUCTIONS)


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def transport():
    return FakeTransport() if os.environ.get("OMNI_MAIL_DRY_RUN") == "1" else RealTransport()


def _out(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


# ------------------------------------------------------------ overview & settings

@mcp.tool()
def get_overview() -> str:
    """Dashboard: campaigns with stats, mailbox usage today, lead counts, unhandled replies."""
    with db() as c:
        return _out(service.overview(c))


@mcp.tool()
def get_settings() -> str:
    """Show global settings (company name, physical address, footer template, unsubscribe URL, keywords)."""
    with db() as c:
        return _out(service.get_settings(c))


@mcp.tool()
def update_setting(key: str, value: str) -> str:
    """Set a global setting. Keys: company_name, physical_address (required before sending),
    unsubscribe_base_url, footer_template, unsubscribe_keywords."""
    with db() as c:
        return _out(service.update_setting(c, key, value))


# ------------------------------------------------------------ mailboxes

@mcp.tool()
def list_mailboxes() -> str:
    """List sending mailboxes with today's sent count and warm-up-adjusted limit."""
    with db() as c:
        rows = service.list_mailboxes(c)
        for r in rows:
            r.pop("imap_last_uid", None)
        return _out(rows)


@mcp.tool()
def add_mailbox(email: str, from_name: str, password_env: str, provider: str = "google",
                daily_limit: int = 30, warmup_start: int = 10, warmup_increment: int = 3,
                signature: str = "", smtp_host: str = "", smtp_port: int = 0,
                imap_host: str = "", imap_port: int = 0) -> str:
    """Add or update a sending mailbox. password_env is the NAME of the environment variable
    (set in .env) that holds the app password; never pass the password itself.
    provider: google | outlook | zoho | custom (then give smtp_host/imap_host)."""
    with db() as c:
        return _out(service.add_mailbox(c, email, from_name, password_env, provider, smtp_host, smtp_port,
                                        imap_host, imap_port, daily_limit=daily_limit,
                                        warmup_start=warmup_start, warmup_increment=warmup_increment,
                                        signature=signature))


@mcp.tool()
def update_mailbox(email: str, from_name: str | None = None, daily_limit: int | None = None,
                   warmup_start: int | None = None, warmup_increment: int | None = None,
                   min_gap_seconds: int | None = None, max_gap_seconds: int | None = None,
                   signature: str | None = None, active: bool | None = None) -> str:
    """Change a mailbox's limits, signature, or pause it (active=false)."""
    with db() as c:
        return _out(service.update_mailbox(c, email, from_name=from_name, daily_limit=daily_limit,
                                           warmup_start=warmup_start, warmup_increment=warmup_increment,
                                           min_gap_seconds=min_gap_seconds, max_gap_seconds=max_gap_seconds,
                                           signature=signature, active=None if active is None else int(active)))


@mcp.tool()
def test_mailbox(email: str) -> str:
    """Log in to a mailbox's SMTP and IMAP servers to check its credentials. Sends nothing."""
    with db() as c:
        mb = c.execute("SELECT * FROM mailboxes WHERE email=?", (email.lower(),)).fetchone()
        if not mb:
            return f"no mailbox {email}"
        return _out(transport().check(mb))


# ------------------------------------------------------------ leads

@mcp.tool()
def import_leads_csv(path: str = "", csv_text: str = "", tags: str = "") -> str:
    """Import leads from a CSV file path or raw CSV text. Needs an email column; first_name, last_name,
    company, title, phone, website, city are standard; any other column becomes a merge field.
    tags: comma-separated tags to apply (useful for enrolling later)."""
    with db() as c:
        return _out(service.import_csv(c, path or None, csv_text or None, tags))


@mcp.tool()
def add_leads(leads: list[dict], tags: str = "") -> str:
    """Add/update leads from a list of objects, e.g. [{"email": "...", "first_name": "...",
    "company": "...", "any_custom_field": "..."}]."""
    with db() as c:
        return _out(service.upsert_leads(c, leads, tags))


@mcp.tool()
def list_leads(status: str = "", tag: str = "", search: str = "", campaign: str = "",
               limit: int = 50, offset: int = 0) -> str:
    """Search leads. status: new|contacted|replied|interested|not_interested|meeting|won|bounced|unsubscribed."""
    with db() as c:
        return _out(service.list_leads(c, status, tag, search, campaign, limit, offset))


@mcp.tool()
def update_lead(email: str, status: str = "", notes: str | None = None, add_tags: str = "",
                fields: dict | None = None) -> str:
    """Update a lead's status (e.g. interested, meeting, not_interested), notes, tags or merge fields.
    Setting a final status stops their active sequences."""
    with db() as c:
        return _out(service.update_lead(c, email, status or None, notes, add_tags, fields))


@mcp.tool()
def suppress(entry: str, reason: str = "manual") -> str:
    """Add an email or a whole domain ('@example.com') to the do-not-contact list. Stops all sequences."""
    with db() as c:
        engine.suppress(c, entry, reason)
        return f"suppressed {entry}"


@mcp.tool()
def list_suppression(limit: int = 100) -> str:
    """Show the do-not-contact list."""
    with db() as c:
        return _out(service.list_suppression(c, limit))


# ------------------------------------------------------------ campaigns

@mcp.tool()
def create_campaign(name: str, timezone: str = "America/New_York", send_days: str = "1,2,3,4,5",
                    window_start: str = "08:30", window_end: str = "16:30", daily_limit: int = 100,
                    mailboxes: list[str] | None = None, stop_on_reply: bool = True) -> str:
    """Create a draft campaign. send_days uses ISO weekdays (1=Mon). mailboxes: sender emails to rotate
    (default all active). Times are in the campaign timezone (the recipients' timezone)."""
    with db() as c:
        return _out(service.create_campaign(c, name, timezone, send_days, window_start, window_end,
                                            daily_limit, mailboxes, stop_on_reply))


@mcp.tool()
def update_campaign(campaign: str, name: str | None = None, timezone: str | None = None,
                    send_days: str | None = None, window_start: str | None = None,
                    window_end: str | None = None, daily_limit: int | None = None,
                    mailboxes: list[str] | None = None, stop_on_reply: bool | None = None) -> str:
    """Change a campaign's schedule, limits or mailboxes."""
    with db() as c:
        return _out(service.update_campaign(c, campaign, name=name, timezone=timezone, send_days=send_days,
                                            window_start=window_start, window_end=window_end,
                                            daily_limit=daily_limit, mailboxes=mailboxes,
                                            stop_on_reply=stop_on_reply))


@mcp.tool()
def set_sequence(campaign: str, steps: list[dict]) -> str:
    """Replace the campaign's email steps: [{"subject": "...", "body": "...", "delay_days": 0}, ...].
    For an A/B test give a step "variants": [{"label": "A", "subject": "...", "body": "..."}, ...].
    delay_days = wait after the previous step. Empty subject on a follow-up = reply in the same thread.
    Do not include a signature or unsubscribe line; the footer adds both."""
    with db() as c:
        return _out(service.set_sequence(c, campaign, steps))


@mcp.tool()
def ab_report(campaign: str, min_sent: int = 30) -> str:
    """Reply rate per A/B variant with a verdict (needs min_sent first emails per variant to call a winner)."""
    with db() as c:
        return _out(service.ab_report(c, campaign, min_sent))


@mcp.tool()
def add_variant(campaign: str, step: int, label: str, subject: str, body: str) -> str:
    """Add or replace an A/B variant on a step; new leads are split evenly across active variants."""
    with db() as c:
        return _out(service.add_variant(c, campaign, step, label, subject, body))


@mcp.tool()
def retire_variant(campaign: str, step: int, label: str) -> str:
    """Stop giving new leads this variant (the loser of a test). Existing leads keep it for follow-ups."""
    with db() as c:
        return _out(service.retire_variant(c, campaign, step, label))


@mcp.tool()
def enroll_leads(campaign: str, emails: list[str] | None = None, tag: str = "", status: str = "") -> str:
    """Add leads to a campaign by explicit emails, a tag, and/or a status (e.g. status='new').
    Suppressed, bounced, replied and unsubscribed leads are skipped automatically."""
    with db() as c:
        return _out(service.enroll(c, campaign, emails, tag, status))


@mcp.tool()
def remove_from_campaign(campaign: str, emails: list[str]) -> str:
    """Stop the sequence for specific leads in one campaign."""
    with db() as c:
        return _out(service.unenroll(c, campaign, emails))


@mcp.tool()
def preview_campaign(campaign: str, lead_email: str = "", step: int = 0) -> str:
    """Render the exact emails a lead will receive (default: first enrolled lead, all steps)."""
    with db() as c:
        return _out(service.preview(c, campaign, lead_email or None, step or None))


@mcp.tool()
def validate_campaign(campaign: str) -> str:
    """Pre-flight check: steps, mailboxes, legal footer, merge-field errors per lead, daily capacity."""
    with db() as c:
        return _out(service.validate_campaign(c, campaign))


@mcp.tool()
def start_campaign(campaign: str) -> str:
    """Activate a campaign (validates first). The background worker then sends within its window."""
    with db() as c:
        return _out(service.set_status(c, campaign, "active"))


@mcp.tool()
def pause_campaign(campaign: str) -> str:
    """Pause a campaign. Nothing more is sent until it is started again."""
    with db() as c:
        return _out(service.set_status(c, campaign, "paused"))


@mcp.tool()
def get_campaign(campaign: str) -> str:
    """Full campaign detail: settings, steps, mailboxes and stats."""
    with db() as c:
        return _out(service.campaign_detail(c, campaign))


@mcp.tool()
def run_send_tick(dry_run: bool = True) -> str:
    """Send whatever is due right now (one email per mailbox). dry_run=true shows what WOULD send.
    Normally the `omni-mail run` worker does this every minute."""
    with db() as c:
        return _out(engine.send_tick(c, transport(), dry_run=dry_run))


# ------------------------------------------------------------ inbox

@mcp.tool()
def sync_inbox() -> str:
    """Fetch new mail from every mailbox; detect replies, bounces, auto-replies and unsubscribes,
    and stop sequences for anyone who replied."""
    with db() as c:
        return _out(engine.sync_inbox(c, transport()))


@mcp.tool()
def list_replies(unhandled_only: bool = True, limit: int = 25) -> str:
    """Replies from leads (newest first). unhandled_only=true shows ones nobody has answered yet."""
    with db() as c:
        return _out(service.list_replies(c, unhandled_only, limit))


@mcp.tool()
def get_thread(lead_email: str) -> str:
    """Everything sent to and received from a lead, plus their record."""
    with db() as c:
        return _out(service.get_thread(c, lead_email))


@mcp.tool()
def send_reply(lead_email: str, body: str) -> str:
    """Reply to a lead in their existing thread from the mailbox that emailed them.
    The mailbox signature is appended. Confirm the text with the user before sending."""
    with db() as c:
        return _out(engine.send_manual_reply(c, transport(), lead_email, body))


@mcp.tool()
def mark_replies_handled(message_ids: list[int]) -> str:
    """Mark replies as handled without answering (e.g. after updating the lead status)."""
    with db() as c:
        return _out(service.mark_handled(c, message_ids))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
