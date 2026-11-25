"""Tiny unsubscribe endpoint (optional). Supports RFC 8058 one-click POST.

Run behind HTTPS (e.g. Cloudflare Tunnel, Caddy) and set the `unsubscribe_base_url` setting.
"""

from __future__ import annotations

import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import engine
from .db import connect
from .render import unsubscribe_token

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Unsubscribe</title><body style="font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem">
{content}</body>"""


class Handler(BaseHTTPRequestHandler):
    def _params(self):
        q = parse_qs(urlparse(self.path).query)
        return q.get("e", [""])[0], q.get("t", [""])[0]

    def _valid(self, conn, email, token):
        return email and token and hmac.compare_digest(unsubscribe_token(conn, email), token)

    def _send(self, code, content):
        body = PAGE.format(content=content).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path != "/u":
            return self._send(404, "<p>Not found.</p>")
        email, token = self._params()
        conn = connect()
        try:
            if not self._valid(conn, email, token):
                return self._send(400, "<p>This link is invalid.</p>")
        finally:
            conn.close()
        e = html.escape(email)
        self._send(200, f"<p>Unsubscribe <b>{e}</b> from future emails?</p>"
                        f"<form method=post><button style='font-size:1rem;padding:.5rem 1rem'>"
                        f"Unsubscribe</button></form>")

    def do_POST(self):  # noqa: N802
        email, token = self._params()
        conn = connect()
        try:
            if not self._valid(conn, email, token):
                return self._send(400, "<p>This link is invalid.</p>")
            engine.suppress(conn, email, "unsubscribe link")
        finally:
            conn.close()
        self._send(200, "<p>You're unsubscribed. You won't hear from us again.</p>")

    def log_message(self, *a):  # quiet
        pass


def serve(host: str = "127.0.0.1", port: int = 8025) -> None:
    print(f"unsubscribe endpoint on http://{host}:{port}/u")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
