# OMNI Mail: operating notes for Claude

This repo is a self-hosted cold email system. The `omni-mail` MCP server (see `.mcp.json`) gives you tools
to run campaigns. The `omni-mail run` worker does the actual sending on a schedule.

## How to run a campaign
1. `get_overview` first: check mailboxes, `physical_address_set`, existing campaigns.
2. Leads: `import_leads_csv` (with a tag) or `add_leads`. Report invalid/suppressed counts back.
3. `create_campaign` (recipient timezone, weekday window, daily limit) then `set_sequence`.
   - Step 1 needs a subject. Follow-ups: empty subject to thread as a reply. Typical delays: 0, 3, 4, 7.
   - Plain text, under ~120 words, one clear ask, no links on step 1, no signature/unsubscribe text (footer adds it).
   - Use fallbacks on merge fields that may be blank: `{{first_name|there}}`.
4. `enroll_leads`, then `preview_campaign` for 2 or 3 different leads and **show the user** the previews.
5. `validate_campaign`, then `start_campaign` only after the user approves the copy.

## Replies
`sync_inbox` -> `list_replies` -> `get_thread`. Draft responses and get the user's OK before `send_reply`
unless they have said to reply on their own. Then `update_lead` with an outcome status
(interested / meeting / not_interested / won). Never contact leads who are unsubscribed or bounced.

## Rules
- Never put passwords in tool calls; mailboxes reference an env var name in `.env`.
- Don't raise a mailbox above ~50/day or skip the warm-up ramp without warning the user about deliverability.
- `run_send_tick` defaults to dry_run; only pass dry_run=false when the user wants to send immediately.
- Tests: `python3 -m pytest -q`.
