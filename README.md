# OMNI Mail — self-hosted cold email that Claude runs

A replacement for Instantly-style tools. It sends multi-step cold email sequences through **your own mailboxes**
(Google Workspace, Outlook, Zoho, or any SMTP/IMAP), rotates senders, ramps volume, threads follow-ups,
catches replies/bounces/unsubscribes, and exposes everything to **Claude over MCP** so you can say
*"build a 3-step campaign for Louisville apartment managers from this CSV and launch it Monday"*.

No subscription. One SQLite file. Python standard library + the `mcp` package.

## What it does

| Instantly feature | Here |
|---|---|
| Unlimited mailboxes + rotation | `add_mailbox`, round-robin by least-used, follow-ups stick to the same sender |
| Sequences with delays | Steps with `delay_days`; empty subject = reply in the same thread (`Re:` + `In-Reply-To`) |
| Personalization / spintax | `{{first_name\|there}}`, any CSV column as a field, `{Hi\|Hey\|Hello}` |
| Sending schedule | Per-campaign timezone, weekdays, time window, daily cap |
| Throttling / warm-up ramp | Per-mailbox daily limit, ramp (`warmup_start` + `warmup_increment`/day), random 2–7 min gaps |
| Unibox | `sync_inbox` + `list_replies` + `get_thread` + `send_reply` (threaded, from the right mailbox) |
| Auto-stop on reply | Yes; auto-replies (out of office) do **not** stop the sequence |
| Bounce + unsubscribe handling | Bounces and "unsubscribe" replies suppress the lead everywhere; optional one-click link (RFC 8058) |
| Blocklist | Emails or whole domains (`@competitor.com`) |
| Analytics | Sent, contacted, reply rate, bounce rate, per-step counts |
| AI | Claude, via 29 MCP tools |

**Not included, on purpose:** open/click tracking pixels (they hurt deliverability for cold email) and a
peer-to-peer "warm-up network" (Google treats those as manipulation). Lead *databases* aren't included either;
bring CSVs from Apollo, Google Maps scrapes, etc.

## Setup (≈15 minutes)

```bash
git clone <this repo> && cd omni
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # installs the `omni-mail` command and the mcp package
cp .env.example .env        # put mailbox app passwords here (never commit it)
```

### 1. Legal footer (required: it won't send without it)
```bash
omni-mail setup --company "OMNI" --address "Your real postal address, City, ST ZIP"
```
CAN-SPAM requires a valid postal address and a working opt-out in every commercial email. The footer adds
both automatically, plus your signature.

### 2. Mailboxes
For Google Workspace: turn on 2-Step Verification for the user, create an **App Password**
(myaccount.google.com → Security → App passwords), and put it in `.env`:
```
MAILBOX_PW_RYAN=abcd efgh ijkl mnop
```
Then:
```bash
omni-mail add-mailbox ryan@your-sending-domain.com --name "Ryan Bear" --password-env MAILBOX_PW_RYAN \
  --provider google --daily-limit 40 --signature $'Ryan Bear\nOMNI | jointheomni.com'
omni-mail test-mailbox ryan@your-sending-domain.com
```
If your Workspace admin has disabled app passwords, use an SMTP relay or another provider for the
sending domains.

### 3. Run the worker (this is what actually sends)
```bash
omni-mail run              # sends due emails every 60s, checks inboxes every 5 min
omni-mail run --dry-run    # logs what it would do without sending
```
Keep it running on an always-on machine: a $5 VPS, a Raspberry Pi, or your desktop. A systemd unit is in
`examples/omni-mail.service`. Cron also works: `* * * * * cd /path/omni && .venv/bin/omni-mail tick` and
`*/5 * * * * … omni-mail sync`.

### 4. Hand it to Claude
`.mcp.json` in this repo already registers the server, so opening Claude Code in this folder gives Claude the
tools (approve the `omni-mail` server when asked). For Claude Desktop, add to `claude_desktop_config.json`:
```json
{ "mcpServers": { "omni-mail": { "command": "/path/omni/.venv/bin/omni-mail", "args": ["mcp"],
  "env": { "OMNI_MAIL_DB": "/path/omni/data/omni_mail.db", "OMNI_MAIL_ENV": "/path/omni/.env" } } } }
```
Set `OMNI_MAIL_DRY_RUN=1` in that env block if you want Claude to be unable to really send.

Then just talk to it:
- "Import `~/Downloads/louisville_apartments.csv` tagged `apts-nov`."
- "Write a 3-step sequence for property managers pitching OMNI's managed resident car care. Preview it for 2 leads."
- "Start it. Max 60/day, 9–4 Eastern, weekdays."
- "Any replies? Draft answers for the interested ones and show me before sending."
- "Mark Jane as `meeting`, and suppress `@competitor.com`."

## Deliverability (read this; it matters more than the software)
1. **Don't send cold email from your main domain.** Buy 2–3 lookalike domains (e.g. `get<brand>.com`, `try<brand>.com`),
   redirect them to your site, and create 2–3 mailboxes on each.
2. **Authenticate every sending domain:** SPF (`v=spf1 include:_spf.google.com ~all`), DKIM (Google Admin →
   Apps → Gmail → Authenticate email), and DMARC (`v=DMARC1; p=none; rua=mailto:you@yourdomain`).
3. **Ramp slowly.** The defaults start each new mailbox at 10/day, +3/day, capped at `daily_limit` (30).
   Keep established mailboxes at ≤ 30–50 cold emails/day. Scale by adding mailboxes, not volume.
4. **Use mailboxes like a human** for 2 weeks before cold sending: send and receive some real mail.
5. **Plain text, short, one link max.** The system sends plain text only.
6. **Clean lists.** Verify emails before import. Bounce rate over 3% means stop and clean the list.
7. Watch Google Postmaster Tools for each sending domain.

## Optional one-click unsubscribe link
Run `omni-mail serve-unsubscribe --port 8025` behind HTTPS (Cloudflare Tunnel or Caddy), then
`omni-mail setup --unsubscribe-url https://unsub.yourdomain.com`. Emails then carry a link and a
`List-Unsubscribe-Post` one-click header. Without it, recipients unsubscribe by replying
"unsubscribe" (detected automatically) or through the `mailto:` List-Unsubscribe header.

## Templates
```
Subject: {Quick|Short} question about {{company}}
Hi {{first_name|there}},

I run OMNI here in {{city|Louisville}}. We wash and protect residents' cars on-site, so {{company}} gets
an amenity residents notice without adding staff work. Worth a 10-minute call?

{{sender_first_name}}
```
If a lead lacks a field with no fallback (`{{company}}` but blank), that lead is skipped with an error
instead of sending a broken email. `validate_campaign` lists these before launch.

## Layout
```
omni_mail/db.py          schema + settings
omni_mail/render.py      merge fields, spintax, footer, unsubscribe tokens
omni_mail/engine.py      scheduler/sender, inbox processing, threaded replies
omni_mail/service.py     campaigns, leads, mailboxes, stats (used by CLI + MCP)
omni_mail/mcp_server.py  Claude's tools
omni_mail/cli.py         `omni-mail` command + worker
omni_mail/web.py         unsubscribe endpoint
tests/                   pytest suite (fake SMTP/IMAP)
```
Run tests: `pip install -e .[dev] && pytest`.
