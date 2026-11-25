import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from omni_mail import engine, service
from omni_mail.db import connect
from omni_mail.render import unsubscribe_token, unsubscribe_url
from omni_mail.web import Handler


def test_unsubscribe_endpoint(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    monkeypatch.setenv("OMNI_MAIL_DB", str(db))
    c = connect(db)
    service.upsert_leads(c, [{"email": "a@b.test", "first_name": "A"}])
    service.update_setting(c, "unsubscribe_base_url", "https://mail.example.com/")
    assert unsubscribe_url(c, "a@b.test").startswith("https://mail.example.com/u?e=a%40b.test&t=")
    tok = unsubscribe_token(c, "a@b.test")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/u?e={quote('a@b.test')}"
    try:
        assert b"Unsubscribe" in urllib.request.urlopen(base + "&t=" + tok).read()
        try:
            urllib.request.urlopen(base + "&t=bad")
            raise AssertionError("bad token accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        r = urllib.request.urlopen(urllib.request.Request(base + "&t=" + tok, data=b"List-Unsubscribe=One-Click"))
        assert b"unsubscribed" in r.read()
    finally:
        srv.shutdown()
    assert engine.is_suppressed(c, "a@b.test")
    assert service.list_leads(c)["leads"][0]["status"] == "unsubscribed"
