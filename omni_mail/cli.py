"""Command line: `omni-mail <command>` (or `python -m omni_mail <command>`)."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from . import engine, service
from .db import connect, load_dotenv, utcnow
from .transport import FakeTransport, RealTransport


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_run(args) -> None:
    """Worker loop: send tick every --tick seconds, inbox sync every --sync seconds."""
    transport = FakeTransport() if args.dry_run else RealTransport()
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    last_sync = 0.0
    print(f"omni-mail worker started ({'DRY RUN' if args.dry_run else 'live'}); Ctrl+C to stop", flush=True)
    while not stop:
        conn = connect()
        try:
            if time.time() - last_sync >= args.sync:
                r = engine.sync_inbox(conn, transport)
                for p in r["processed"]:
                    print(f"{utcnow()} inbox {p['type']}: {p['lead']}", flush=True)
                for e in r["errors"]:
                    print(f"{utcnow()} inbox error: {e}", flush=True)
                last_sync = time.time()
            r = engine.send_tick(conn, transport, dry_run=args.dry_run)
            for s in r["sent"]:
                print(f"{utcnow()} sent step {s['step']} [{s['campaign']}] {s['from']} -> {s['to']}", flush=True)
            for e in r["errors"]:
                print(f"{utcnow()} error: {e}", flush=True)
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            print(f"{utcnow()} worker error: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()
        for _ in range(args.tick):
            if stop:
                break
            time.sleep(1)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="omni-mail", description="Self-hosted cold email campaigns")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="set company name / physical address / unsubscribe URL")
    s.add_argument("--company"); s.add_argument("--address"); s.add_argument("--unsubscribe-url")

    s = sub.add_parser("add-mailbox", help="add a sending mailbox")
    s.add_argument("email"); s.add_argument("--name", required=True)
    s.add_argument("--password-env", required=True, help="env var name holding the app password")
    s.add_argument("--provider", default="google", choices=["google", "outlook", "zoho", "custom"])
    s.add_argument("--smtp-host", default=""); s.add_argument("--smtp-port", type=int, default=0)
    s.add_argument("--imap-host", default=""); s.add_argument("--imap-port", type=int, default=0)
    s.add_argument("--daily-limit", type=int, default=30)
    s.add_argument("--warmup-start", type=int, default=10)
    s.add_argument("--warmup-increment", type=int, default=3)
    s.add_argument("--signature", default="")

    s = sub.add_parser("test-mailbox", help="check SMTP/IMAP login"); s.add_argument("email")
    sub.add_parser("mailboxes", help="list mailboxes")

    s = sub.add_parser("import", help="import leads from CSV")
    s.add_argument("csv"); s.add_argument("--tags", default="")

    sub.add_parser("status", help="overview of everything")
    s = sub.add_parser("preview", help="preview a campaign"); s.add_argument("campaign")
    s.add_argument("--lead", default=None)
    s = sub.add_parser("start", help="start a campaign"); s.add_argument("campaign")
    s = sub.add_parser("pause", help="pause a campaign"); s.add_argument("campaign")

    s = sub.add_parser("tick", help="send what is due now, once"); s.add_argument("--dry-run", action="store_true")
    sub.add_parser("sync", help="process inbox once")

    s = sub.add_parser("run", help="run the background worker")
    s.add_argument("--tick", type=int, default=60, help="seconds between send ticks")
    s.add_argument("--sync", type=int, default=300, help="seconds between inbox syncs")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("serve-unsubscribe", help="run the unsubscribe web endpoint")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8025)

    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude)")

    args = p.parse_args(argv)

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()
    if args.cmd == "serve-unsubscribe":
        from .web import serve
        return serve(args.host, args.port)
    if args.cmd == "run":
        return cmd_run(args)

    conn = connect()
    try:
        if args.cmd == "setup":
            for key, val in (("company_name", args.company), ("physical_address", args.address),
                             ("unsubscribe_base_url", args.unsubscribe_url)):
                if val is not None:
                    service.update_setting(conn, key, val)
            _print(service.get_settings(conn))
        elif args.cmd == "add-mailbox":
            _print(service.add_mailbox(conn, args.email, args.name, args.password_env, args.provider,
                                       args.smtp_host, args.smtp_port, args.imap_host, args.imap_port,
                                       daily_limit=args.daily_limit, warmup_start=args.warmup_start,
                                       warmup_increment=args.warmup_increment, signature=args.signature))
        elif args.cmd == "test-mailbox":
            mb = conn.execute("SELECT * FROM mailboxes WHERE email=?", (args.email.lower(),)).fetchone()
            if not mb:
                sys.exit(f"no mailbox {args.email}")
            _print(RealTransport().check(mb))
        elif args.cmd == "mailboxes":
            _print(service.list_mailboxes(conn))
        elif args.cmd == "import":
            _print(service.import_csv(conn, args.csv, tags=args.tags))
        elif args.cmd == "status":
            _print(service.overview(conn))
        elif args.cmd == "preview":
            for m in service.preview(conn, args.campaign, args.lead):
                print(f"\n=== step {m['step']} ===")
                if "error" in m:
                    print("ERROR:", m["error"]); continue
                print(f"From: {m['from']}\nTo: {m['to']}\nSubject: {m['subject']}\n\n{m['body']}")
        elif args.cmd == "start":
            _print(service.set_status(conn, args.campaign, "active"))
        elif args.cmd == "pause":
            _print(service.set_status(conn, args.campaign, "paused"))
        elif args.cmd == "tick":
            _print(engine.send_tick(conn, FakeTransport() if args.dry_run else RealTransport(),
                                    dry_run=args.dry_run))
        elif args.cmd == "sync":
            _print(engine.sync_inbox(conn, RealTransport()))
    except ValueError as e:
        sys.exit(f"error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
