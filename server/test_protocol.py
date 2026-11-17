#!/usr/bin/env python3
"""Self-check for gomovie_sync.py — replays the client's exact WebDAV sequence.

    python3 test_protocol.py      # exits non-zero on the first broken expectation

The order below mirrors CloudSync.syncWebdav(): GET, merge, PUT with If-Match /
If-None-Match, 412 retry, MKCOL for missing dirs. If this passes, the app's sync
engine cannot tell the difference between this server and Nextcloud.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

import gomovie_sync as gs

FAILURES = []


def check(label, actual, expected):
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {actual!r}, want {expected!r}")
        FAILURES.append(label)


def request(port, method, path, body=None, headers=None, auth=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = dict(headers or {})
    if auth:
        import base64

        hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, dict(resp.getheaders()), payload
    finally:
        conn.close()


def main():
    root = Path(tempfile.mkdtemp(prefix="gs-test-"))
    store = gs.Store(root)
    store.config = {
        "secret": "t" * 64,
        "admin_hash": gs.hash_password("adminpw"),
        "public_url": "",
        "max_bytes": 8 * 1024 * 1024,
        "max_versions": 50,
        "retention_days": 30,
    }
    store.save_config()
    store.add_device("tablet", "default", "pw-tablet")
    store.add_device("tv", "default", "pw-tv")
    store.add_device("fresh", "other", "pw-fresh")

    gs.Handler.store = store
    gs.Handler.public_url = "https://sync.example.test"
    server = gs.ThreadingHTTPServer(("127.0.0.1", 0), gs.Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth = ("tablet", "pw-tablet")
    url = "/dav/default/marco-movie-sync.json"
    payload = json.dumps({"schema": "marco-movie-sync", "version": 2, "updatedAt": 1, "watch_history": [{"a": 1}], "favorites": []}).encode()
    payload2 = json.dumps({"schema": "marco-movie-sync", "version": 2, "updatedAt": 2, "watch_history": [{"a": 2}], "favorites": [{"b": 1}]}).encode()

    print("auth")
    check("no header -> 401", request(port, "GET", url)[0], 401)
    status, headers, _ = request(port, "GET", url)
    check("401 carries WWW-Authenticate", "Basic" in headers.get("WWW-Authenticate", ""), True)
    check("wrong password -> 401", request(port, "GET", url, auth=("tablet", "nope"))[0], 401)
    check("unknown device -> 401", request(port, "GET", url, auth=("ghost", "x"))[0], 401)

    print("first sync (file absent)")
    check("GET absent -> 404", request(port, "GET", url, auth=auth)[0], 404)
    status, headers, _ = request(port, "PUT", url, body=payload, headers={"If-None-Match": "*"}, auth=auth)
    check("PUT If-None-Match:* -> 201", status, 201)
    etag1 = headers.get("ETag")
    check("PUT returns strong ETag", bool(etag1 and not etag1.startswith("W/")), True)

    print("second sync (conditional write)")
    status, headers, body = request(port, "GET", url, auth=auth)
    check("GET -> 200", status, 200)
    check("GET body round-trips", body, payload)
    check("GET ETag matches PUT", headers.get("ETag"), etag1)

    status, headers, _ = request(port, "PUT", url, body=payload2, headers={"If-Match": etag1}, auth=auth)
    check("PUT If-Match current -> 204", status, 204)
    etag2 = headers.get("ETag")
    check("ETag changed after write", etag2 != etag1, True)

    check(
        "PUT If-Match stale -> 412",
        request(port, "PUT", url, body=payload2, headers={"If-Match": etag1}, auth=auth)[0],
        412,
    )
    check(
        "PUT If-None-Match:* when exists -> 412",
        request(port, "PUT", url, body=payload, headers={"If-None-Match": "*"}, auth=auth)[0],
        412,
    )
    check("unconditional PUT -> 204", request(port, "PUT", url, body=b'{"x":1}', auth=auth)[0], 204)

    print("dirs / spaces / authz")
    check("MKCOL existing dir -> 405", request(port, "MKCOL", "/dav/default/", auth=auth)[0], 405)
    check("MKCOL new dir -> 201", request(port, "MKCOL", "/dav/other/", auth=("fresh", "pw-fresh"))[0], 201)
    check("MKCOL twice -> 405", request(port, "MKCOL", "/dav/other/", auth=("fresh", "pw-fresh"))[0], 405)
    check("device bound to default cannot touch other -> 403", request(port, "GET", "/dav/other/marco-movie-sync.json", auth=auth)[0], 403)
    check("root-level filename maps to default space", request(port, "GET", "/marco-movie-sync.json", auth=auth)[0], 200)
    check("other device reads same space", request(port, "GET", url, auth=("tv", "pw-tv"))[0], 200)

    print("guards")
    check("non-json body -> 400", request(port, "PUT", url, body=b"not json", auth=auth)[0], 400)
    check("oversized body -> 413", request(port, "PUT", url, body=b"{" + b"0" * (9 * 1024 * 1024) + b"}", auth=auth)[0], 413)
    check("OPTIONS advertises DAV", "1" in request(port, "OPTIONS", url)[1].get("DAV", ""), True)
    check("healthz -> 200", request(port, "GET", "/healthz")[0], 200)

    print("versions + rollback")
    versions = store.versions("default")
    check("snapshots were kept", len(versions) >= 2, True)
    oldest = versions[-1]["id"]
    before = request(port, "GET", url, auth=auth)[2]
    status, headers, _ = request(port, "POST", "/admin/login", body="password=adminpw",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("admin login -> 302", status, 302)
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    check("login sets session cookie", cookie.startswith("gs_session="), True)

    csrf = gs.hmac.new(store.config["secret"].encode(), b"csrf", gs.hashlib.sha256).hexdigest()[:32]
    status, headers, body = request(port, "GET", "/admin", headers={"Cookie": cookie})
    check("dashboard -> 200", status, 200)
    check("dashboard lists device", b"tablet" in body, True)
    check("dashboard shows payload summary", "观看进度".encode() in body, True)

    status, headers, body = request(
        port, "POST", "/admin/device-add", body=f"csrf={csrf}&name=phone&space=default&password=pw-phone",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    check("create device -> 200 with credentials", status, 200)
    check("credentials page shows url", b"/dav/default/marco-movie-sync.json" in body, True)
    check("new device can read", request(port, "GET", url, auth=("phone", "pw-phone"))[0], 200)

    status, headers, body = request(
        port, "POST", "/admin/rollback", body=f"csrf={csrf}&space=default&id={oldest}",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    check("rollback -> 302", status, 302)
    after = request(port, "GET", url, auth=auth)[2]
    restored = json.loads(after)
    check("rollback changed stored payload", after != before, True)
    check("rollback restored an older schema payload", restored.get("schema"), "marco-movie-sync")
    check("csrf rejected without token", request(
        port, "POST", "/admin/device-add", body="name=x&space=default",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})[0], 400)
    check("admin without cookie redirects", request(port, "GET", "/admin")[0], 302)

    print("admin password rotation")
    check("short password rejected", request(
        port, "POST", "/admin/admin-passwd", body=f"csrf={csrf}&password=short",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})[0], 302)
    status, headers, _ = request(
        port, "POST", "/admin/admin-passwd", body=f"csrf={csrf}&password=newadminpass",
        headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    check("rotate -> 302", status, 302)
    check("old password now rejected", b"adminpw" and request(
        port, "POST", "/admin/login", body="password=adminpw",
        headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 200)
    check("new password logs in", request(
        port, "POST", "/admin/login", body="password=newadminpass",
        headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 302)

    print("account expiry")
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
    store.add_device("lapsed", "default", "pw-lapsed", past)
    store.add_device("dated", "default", "pw-dated", future)
    status, _, body = request(port, "GET", url, auth=("lapsed", "pw-lapsed"))
    check("expired account -> 403", status, 403)
    check("expired body names the expiry", b"expired" in body, True)
    check("dated account still works", request(port, "GET", url, auth=("dated", "pw-dated"))[0], 200)

    csrf = gs.hmac.new(store.config["secret"].encode(), b"csrf", gs.hashlib.sha256).hexdigest()[:32]
    status, headers, _ = request(port, "POST", "/admin/login", body="password=newadminpass",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    check("panel login for admin actions", cookie.startswith("gs_session="), True)
    form_headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
    check("extend expired account", request(
        port, "POST", "/admin/device-extend", body=f"csrf={csrf}&name=lapsed&days=30",
        headers=form_headers)[0], 302)
    check("extended account works again", request(port, "GET", url, auth=("lapsed", "pw-lapsed"))[0], 200)
    check("clear expiry", request(
        port, "POST", "/admin/device-expiry", body=f"csrf={csrf}&name=dated&expires=",
        headers=form_headers)[0], 302)
    check("permanent account still works", request(port, "GET", url, auth=("dated", "pw-dated"))[0], 200)
    check("bad expiry date rejected", request(
        port, "POST", "/admin/device-expiry", body=f"csrf={csrf}&name=dated&expires=2026-13-99",
        headers=form_headers)[0], 302)

    print("editing payload from the panel")
    before = request(port, "GET", url, auth=auth)[2]
    edited = json.dumps({"schema": "marco-movie-sync", "version": 2, "updatedAt": 42, "watch_history": [], "favorites": [{"t": "edited"}]})
    status, headers, _ = request(port, "POST", "/admin/edit-save",
                                 body="csrf=" + csrf + "&space=default&content=" + urllib.parse.quote(edited),
                                 headers=form_headers)
    check("edit-save -> 302", status, 302)
    after = request(port, "GET", url, auth=auth)[2]
    check("edited payload is live", json.loads(after)["favorites"], [{"t": "edited"}])
    check("edit kept a version of the old payload", any(store.version_dir("default").glob("*.json")), True)
    check("broken json refused", request(
        port, "POST", "/admin/edit-save", body="csrf=" + csrf + "&space=default&content=" + urllib.parse.quote("{not json"),
        headers=form_headers)[0], 302)
    check("payload untouched after refused save", request(port, "GET", url, auth=auth)[2], after)
    edit_page = request(port, "GET", "/admin/edit?space=default", headers={"Cookie": cookie})[2]
    check("edit page has a textarea", b"<textarea" in edit_page, True)
    check("edit page renders the payload", b"watch_history" in edit_page, True)

    print("editing public config from the panel")
    (store.root / "public").mkdir(parents=True, exist_ok=True)
    (store.root / "public" / "tvbox-demo.json").write_text('{"sites": []}', encoding="utf-8")
    check("config list page", b"tvbox-demo.json" in request(port, "GET", "/admin/configs", headers={"Cookie": cookie})[2], True)
    cfg = '{"sites": [{"key": "x"}], "lives": []}'
    check("config-save -> 302", request(
        port, "POST", "/admin/config-save", body="csrf=" + csrf + "&name=tvbox-demo.json&content=" + urllib.parse.quote(cfg),
        headers=form_headers)[0], 302)
    check("served config updated", request(port, "GET", "/config/tvbox-demo.json")[2], cfg.encode())
    check("config history kept", any((store.root / "public" / ".versions").glob("tvbox-demo.json.*")), True)
    check("bad config json refused", request(
        port, "POST", "/admin/config-save", body="csrf=" + csrf + "&name=tvbox-demo.json&content=" + urllib.parse.quote("[1,2"),
        headers=form_headers)[0], 302)
    check("config untouched after refused save", request(port, "GET", "/config/tvbox-demo.json")[2], cfg.encode())

    print("gateway: config points at us, expiry closes it")
    import http.server as hs

    class Upstream(hs.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"upstream": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = hs.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    priv = store.root / "private"
    priv.mkdir(parents=True, exist_ok=True)
    (priv / "sources.json").write_text(json.dumps({
        "sites": [{"key": "demo", "api": f"http://127.0.0.1:{up_port}/api.php/provide/vod"}],
        "lives": [{"url": f"http://127.0.0.1:{up_port}/live.txt"}],
    }), encoding="utf-8")
    store.load_sources()

    token = store.token_for("tablet")
    lapsed = store.device("lapsed")
    lapsed["expires"] = past  # re-expire it: the panel test above extended it
    store.save_devices()
    expired_token = store.token_for("lapsed")

    status, _, body = request(port, "GET", f"/g/{token}/demo?ac=videolist&t=1&pg=2")
    check("gateway proxies a site call -> 200", status, 200)
    check("gateway forwards the query", b"ac=videolist" in body and b"pg=2" in body, True)
    check("gateway proxies a live list -> 200", request(port, "GET", f"/g/{token}/live0")[0], 200)
    check("expired token -> 403", request(port, "GET", f"/g/{expired_token}/demo?ac=list")[0], 403)
    check("unknown token -> 403", request(port, "GET", "/g/deadbeef/demo?ac=list")[0], 403)
    check("unknown source key -> 404", request(port, "GET", f"/g/{token}/nope?ac=list")[0], 404)
    check("gateway never exposes the upstream host", b"127.0.0.1" not in request(port, "GET", f"/g/{token}/missing")[2], True)
    upstream.shutdown()

    print("client config generation")
    gen = Path(__file__).resolve().parent / "make_client_config.py"
    import subprocess

    env = dict(os.environ, GOMOVIE_SYNC_ROOT=str(store.root))
    proc = subprocess.run([sys.executable, str(gen), "tablet"], capture_output=True, env=env, timeout=60)
    check("generator exits 0", proc.returncode, 0)
    generated = store.root / "public" / "tablet.json"
    check("generator wrote the config", generated.is_file(), True)
    cfg_text = generated.read_text(encoding="utf-8") if generated.is_file() else "{}"
    check("generated config routes sites through the gateway", "/g/" in cfg_text, True)
    check("generated config hides the real upstream", "127.0.0.1" not in cfg_text, True)
    check("generator refuses unknown account", subprocess.run(
        [sys.executable, str(gen), "ghost"], capture_output=True, env=env, timeout=60).returncode, 1)

    print("logging")
    entries = store.recent_log(500)
    methods = {e["method"] for e in entries}
    check("log recorded PUT/GET", {"PUT", "GET"} <= methods, True)
    check("log recorded 412", any(e["status"] == 412 for e in entries), True)
    check("log recorded device name", any(e["device"] == "tablet" for e in entries), True)

    server.shutdown()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all protocol checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
