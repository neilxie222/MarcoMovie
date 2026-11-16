#!/usr/bin/env python3
"""gomovie-sync — WebDAV-subset sync backend + admin panel for MarcoMovie / Gomovie.

The client (android/src/main/java/com/pilipala/marcomovie/CloudSync.java) only performs:

    GET  <space>/marco-movie-sync.json          -> 200 + strong ETag, or 404
    PUT  ... with If-Match / If-None-Match: *   -> 412 when the precondition fails
    MKCOL <ancestor dirs>                       -> 201 / 405 (optional: PUT creates dirs)
    Basic auth                                  -> 401

That is the whole protocol surface. This file implements exactly that plus an admin
panel that tracks devices, keeps every previous payload as a restorable version, and
logs every request. Standard library only; no database.

    python3 gomovie_sync.py --init     # first run: create config.json, print admin password
    python3 gomovie_sync.py            # serve (systemd unit does this)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FILE_NAME = "marco-movie-sync.json"
LOCKS: dict[str, threading.Lock] = {}
LOCKS_GUARD = threading.Lock()
FAILED_AUTH: dict[str, list] = {}
STATE_GUARD = threading.Lock()


# --------------------------------------------------------------------------- storage


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.logs = root / "logs"
        self.data.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.config = self._load_json(root / "config.json", None)
        self.devices = self._load_json(root / "devices.json", {"devices": {}})
        self.state = self._load_json(root / "state.json", {"devices": {}, "spaces": {}})

    # -- json helpers ------------------------------------------------------
    @staticmethod
    def _load_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path: Path, value) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def save_config(self) -> None:
        self._save_json(self.root / "config.json", self.config)

    def save_devices(self) -> None:
        self._save_json(self.root / "devices.json", self.devices)

    def save_state(self) -> None:
        self._save_json(self.root / "state.json", self.state)

    # -- paths -------------------------------------------------------------
    def space_dir(self, space: str) -> Path:
        return self.data / space

    def space_file(self, space: str) -> Path:
        return self.space_dir(space) / FILE_NAME

    def version_dir(self, space: str) -> Path:
        return self.space_dir(space) / "versions"

    def spaces(self) -> list[str]:
        return sorted(d.name for d in self.data.iterdir() if d.is_dir() and not d.name.startswith("."))

    # -- devices -----------------------------------------------------------
    def add_device(self, name: str, space: str, password: str) -> None:
        self.devices["devices"][name] = {
            "space": space,
            "hash": hash_password(password),
            "enabled": True,
            "created": utc_iso(),
        }
        self.save_devices()

    def device(self, name: str):
        return self.devices["devices"].get(name)

    def device_by_password(self, user: str, password: str):
        dev = self.devices["devices"].get(user)
        if not dev or not dev.get("enabled", True):
            return None
        return dev if verify_password(password, dev["hash"]) else None

    # -- payload versions --------------------------------------------------
    def snapshot(self, space: str, content: bytes) -> None:
        """Store the payload that is about to be replaced, then prune old snapshots."""
        folder = self.version_dir(space)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        (folder / f"{stamp}.json").write_bytes(content)
        self.prune_versions(space)

    def prune_versions(self, space: str) -> None:
        cfg = self.config or {}
        keep_newest = int(cfg.get("max_versions", 50))
        keep_days = int(cfg.get("retention_days", 30))
        folder = self.version_dir(space)
        files = sorted(folder.glob("*.json"))
        by_day: dict[str, Path] = {}
        for path in files:
            by_day[path.name[:8]] = path  # last one wins = newest of that day
        cutoff = time.time() - keep_days * 86400
        protected = set(files[-keep_newest:]) | {
            p for p in by_day.values() if p.stat().st_mtime >= cutoff
        }
        for path in files:
            if path not in protected:
                try:
                    path.unlink()
                except OSError:
                    pass

    def versions(self, space: str) -> list[dict]:
        folder = self.version_dir(space)
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            st = path.stat()
            out.append(
                {
                    "id": path.name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc),
                }
            )
        return out

    # -- request log -------------------------------------------------------
    def log(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        path = self.logs / "access.jsonl"
        with STATE_GUARD:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            if path.stat().st_size > 5 * 1024 * 1024:
                os.replace(path, self.logs / f"access-{int(time.time())}.jsonl")

    def recent_log(self, limit: int = 200, device: str = "") -> list[dict]:
        path = self.logs / "access.jsonl"
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
        out = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if device and item.get("device") != device:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    # -- per-device touch bookkeeping --------------------------------------
    def touch(self, device: str, space: str, action: str, size: int = 0) -> None:
        now = utc_iso()
        with STATE_GUARD:
            rec = self.state["devices"].setdefault(device, {})
            rec["space"] = space
            rec[f"last_{action}"] = now
            if size:
                rec["size"] = size
            self.state["spaces"][space] = {"updated": now, "size": size or rec.get("size", 0)}
            self.save_state()


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def space_lock(space: str) -> threading.Lock:
    with LOCKS_GUARD:
        return LOCKS.setdefault(space, threading.Lock())


def etag_of(content: bytes) -> str:
    return '"' + hashlib.sha256(content).hexdigest()[:32] + '"'


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- html views

CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#101013;color:#f5f5f7;font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:#7cc4ff;text-decoration:none}
a:hover{text-decoration:underline}
header{display:flex;align-items:center;gap:16px;padding:16px 24px;border-bottom:1px solid #26262c;position:sticky;top:0;background:#101013ee;backdrop-filter:blur(8px)}
header b{font-size:17px;letter-spacing:.3px}
nav{display:flex;gap:14px;font-size:14px;margin-left:auto}
main{max-width:1060px;margin:0 auto;padding:24px}
h2{font-size:16px;margin:28px 0 12px;color:#d8d8de}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.card{background:#17171b;border:1px solid #26262c;border-radius:14px;padding:16px}
.card .k{font-size:12px;color:#8b8b95;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:22px;font-weight:600;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #232329;vertical-align:middle}
th{color:#8b8b95;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
code,.mono{font-family:ui-monospace,Consolas,monospace;font-size:13px}
input,select,button{font:inherit;color:inherit}
input[type=text],input[type=password],select{background:#1c1c21;border:1px solid #31313a;border-radius:10px;padding:9px 12px;width:100%}
button,.btn{background:#f5f5f7;color:#101013;border:0;border-radius:10px;padding:9px 16px;font-weight:600;cursor:pointer}
button.ghost,.btn.ghost{background:transparent;border:1px solid #3a3a44;color:#f5f5f7;font-weight:500}
button.danger{background:#3a1d21;color:#ff9aa6;border:1px solid #5a2a30}
form.inline{display:inline}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.cred{background:#0e1a12;border:1px solid #24402c;border-radius:12px;padding:16px;margin:14px 0}
.cred div{margin:5px 0}
.muted{color:#8b8b95}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;background:#23232a}
.ok{background:#16301f;color:#7fe0a0}
.off{background:#301d1d;color:#ff9aa6}
.flash{background:#1b2a1f;border:1px solid #2c4732;border-radius:12px;padding:12px 16px;margin:14px 0}
"""


def page(title: str, body: str, nav: str = "") -> bytes:
    return (
        "<!doctype html><html lang=zh-CN><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style>"
        f"<header><b>Gomovie Sync</b><nav>{nav}</nav></header><main>{body}</main></html>"
    ).encode("utf-8")


def fmt_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return iso


def fmt_size(n: int) -> str:
    if not n:
        return "—"
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB"


# --------------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "gomovie-sync/1.0"
    protocol_version = "HTTP/1.1"
    store: Store = None  # set by main()
    public_url: str = ""

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # silence default stderr noise; we log ourselves
        pass

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def respond(self, code: int, body: bytes = b"", headers: dict | None = None, ctype: str = "") -> None:
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if code != 204:  # 204 has no body by definition; a Content-Length there upsets strict clients
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def drain(self) -> None:
        """Read and discard a request body so keep-alive stays in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def read_body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise ValueError("chunked bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length > limit:
            raise OverflowError(length)
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def note(self, status: int, device: str, size: int = 0) -> None:
        self.store.log(
            {
                "ts": utc_iso(),
                "ip": self.client_ip(),
                "device": device,
                "method": self.command,
                "path": self.path,
                "status": status,
                "bytes": size,
                "ua": self.headers.get("User-Agent", ""),
            }
        )

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/dav/") or path.endswith(FILE_NAME):
            return self.dav("GET", path)
        if path == "/admin" or path.startswith("/admin/"):
            return self.admin("GET", path)
        if path == "/healthz":
            return self.respond(200, b"ok", ctype="text/plain")
        if path in ("/", ""):
            return self.respond(302, b"", headers={"Location": "/admin"})
        return self.respond(404, b"not found", ctype="text/plain")

    def do_HEAD(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/dav/") or path.endswith(FILE_NAME):
            return self.dav("HEAD", path)
        return self.respond(404)

    def do_PUT(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/dav/") or path.endswith(FILE_NAME):
            return self.dav("PUT", path)
        self.drain()
        return self.respond(405)

    def do_DELETE(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/dav/") or path.endswith(FILE_NAME):
            return self.dav("DELETE", path)
        return self.respond(405)

    def do_MKCOL(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        return self.dav("MKCOL", path)

    def do_OPTIONS(self):
        self.drain()
        return self.respond(
            200,
            b"",
            headers={
                "DAV": "1, 2",
                "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL",
                "MS-Author-Via": "DAV",
            },
        )

    def do_POST(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/admin"):
            return self.admin("POST", path)
        self.drain()
        return self.respond(405)

    # -- webdav ------------------------------------------------------------
    def dav(self, method: str, path: str) -> None:
        segments = [s for s in path.split("/") if s]
        if method == "MKCOL":
            return self.mkcol(segments)

        if not (segments and segments[-1] == FILE_NAME):
            if method == "PUT":
                self.drain()
            return self.respond(404, b"expecting " + FILE_NAME.encode(), ctype="text/plain")

        prefix = segments[:-1]
        if prefix and prefix[0] == "dav":
            prefix = prefix[1:]
        space = prefix[-1] if prefix else "default"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", space):
            return self.respond(400, b"bad space", ctype="text/plain")

        device = self.authenticate()
        if device is None:
            return
        name, record = device
        if record["space"] != space:
            self.note(403, name)
            self.drain()
            return self.respond(403, b"device not bound to this space", ctype="text/plain")
        return self.dav_space(method, name, space)

    def mkcol(self, segments: list[str]) -> None:
        """Accept any collection path: the client probes ancestors before its first PUT."""
        self.drain()
        device = self.authenticate()
        if device is None:
            return
        name = device[0]
        parts = segments[1:] if segments and segments[0] == "dav" else segments
        if not all(re.fullmatch(r"[A-Za-z0-9._-]{1,40}", part) for part in parts):
            self.note(400, name)
            return self.respond(400, b"bad collection name", ctype="text/plain")
        folder = self.store.data.joinpath(*parts) if parts else self.store.data
        existed = folder.is_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self.note(405 if existed else 201, name)
        return self.respond(405 if existed else 201)

    def authenticate(self):
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("basic "):
            self.respond(
                401,
                b"auth required",
                headers={"WWW-Authenticate": 'Basic realm="gomovie-sync", charset="UTF-8"'},
                ctype="text/plain",
            )
            return None
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8")
            user, _, password = raw.partition(":")
        except (ValueError, UnicodeDecodeError):
            self.drain()
            self.respond(400, b"bad auth header", ctype="text/plain")
            return None
        ip = self.client_ip()
        with STATE_GUARD:
            fails = [t for t in FAILED_AUTH.get(ip, []) if time.time() - t < 300]
            FAILED_AUTH[ip] = fails
            if len(fails) >= 15:
                self.drain()
                self.note(429, user)
                self.respond(429, b"too many failed attempts", ctype="text/plain")
                return None
        record = self.store.device_by_password(user, password)
        if record is None:
            with STATE_GUARD:
                FAILED_AUTH.setdefault(ip, []).append(time.time())
            self.drain()
            self.note(401, user)
            self.respond(401, b"invalid credentials", headers={"WWW-Authenticate": 'Basic realm="gomovie-sync"'}, ctype="text/plain")
            return None
        return user, record

    def dav_space(self, method: str, device: str, space: str) -> None:
        target = self.store.space_file(space)
        if method == "OPTIONS":
            self.drain()
            return self.respond(200, b"", headers={"DAV": "1, 2", "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL"})

        if method == "GET" or method == "HEAD":
            if not target.is_file():
                self.note(404, device)
                return self.respond(404, b"not found", ctype="text/plain")
            content = target.read_bytes()
            etag = etag_of(content)
            self.note(200, device, len(content))
            self.store.touch(device, space, "get", len(content))
            return self.respond(
                200,
                content,
                headers={
                    "ETag": etag,
                    "Last-Modified": self.date_time_string(target.stat().st_mtime),
                    "Cache-Control": "no-store",
                },
                ctype="application/json; charset=utf-8",
            )

        if method == "DELETE":
            self.drain()
            if target.is_file():
                with space_lock(space):
                    self.store.snapshot(space, target.read_bytes())
                    target.unlink()
            self.note(204, device)
            return self.respond(204)

        # PUT
        try:
            body = self.read_body(int((self.store.config or {}).get("max_bytes", 8 * 1024 * 1024)))
        except OverflowError:
            self.drain()
            self.note(413, device)
            return self.respond(413, b"payload too large", ctype="text/plain")
        except ValueError as exc:
            self.note(400, device)
            return self.respond(400, str(exc).encode(), ctype="text/plain")

        try:
            json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.note(400, device)
            return self.respond(400, b"body is not utf-8 json", ctype="text/plain")

        if_match = (self.headers.get("If-Match") or "").strip()
        if_none_match = (self.headers.get("If-None-Match") or "").strip()

        with space_lock(space):
            current = target.read_bytes() if target.is_file() else None
            current_etag = etag_of(current) if current is not None else None
            if if_none_match == "*" and current is not None:
                self.note(412, device)
                return self.respond(412, b"file already exists", ctype="text/plain")
            if if_match and if_match != current_etag:
                self.note(412, device)
                return self.respond(412, b"etag mismatch", ctype="text/plain")
            if current != body:
                if current is not None:
                    self.store.snapshot(space, current)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(body)
                os.replace(tmp, target)

        self.note(201 if current is None else 204, device, len(body))
        self.store.touch(device, space, "put", len(body))
        return self.respond(
            201 if current is None else 204,
            b"",
            headers={"ETag": etag_of(body), "Last-Modified": self.date_time_string()},
        )

    # -- admin -------------------------------------------------------------
    def admin(self, method: str, path: str) -> None:
        if method == "GET":
            return self.admin_get(path)
        return self.admin_post(path)

    def session(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "gs_session":
                return value
        return ""

    def session_valid(self, token: str) -> bool:
        secret = (self.store.config or {}).get("secret", "")
        if not token or "." not in token or not secret:
            return False
        stamp, _, sig = token.partition(".")
        expected = hmac.new(secret.encode(), stamp.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            return time.time() - int(stamp) < 12 * 3600
        except ValueError:
            return False

    def csrf(self) -> str:
        secret = (self.store.config or {}).get("secret", "")
        return hmac.new(secret.encode(), b"csrf", hashlib.sha256).hexdigest()[:32]

    def flash(self, query: dict) -> str:
        message = query.get("ok", [""])[0]
        return f"<div class=flash>{html.escape(message)}</div>" if message else ""

    def admin_get(self, path: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        token = self.session()

        if path == "/admin/login":
            return self.respond(200, self.login_page(query), ctype="text/html; charset=utf-8")
        if path == "/admin/logout":
            return self.respond(302, b"", headers={"Location": "/admin/login", "Set-Cookie": "gs_session=; Path=/; Max-Age=0"})
        if not self.session_valid(token):
            return self.respond(302, b"", headers={"Location": "/admin/login"})

        nav = (
            '<a href="/admin">概览</a><a href="/admin/versions">版本</a>'
            '<a href="/admin/log">日志</a><a href="/admin/logout">退出</a>'
        )
        if path == "/admin":
            return self.respond(200, page("Gomovie Sync", self.dashboard(query), nav), ctype="text/html; charset=utf-8")
        if path == "/admin/versions":
            return self.respond(200, page("版本", self.versions_page(query), nav), ctype="text/html; charset=utf-8")
        if path == "/admin/versions/download":
            space = query.get("space", ["default"])[0]
            vid = query.get("id", [""])[0]
            if not re.fullmatch(r"[\dTZ.:-]+\.json", vid):
                return self.respond(400, b"bad id", ctype="text/plain")
            target = self.store.version_dir(space) / vid
            if not target.is_file():
                return self.respond(404, b"no such version", ctype="text/plain")
            return self.respond(
                200,
                target.read_bytes(),
                headers={"Content-Disposition": f'attachment; filename="{vid}"'},
                ctype="application/json; charset=utf-8",
            )
        if path == "/admin/log":
            return self.respond(200, page("日志", self.log_page(query), nav), ctype="text/html; charset=utf-8")
        return self.respond(404, b"not found", ctype="text/plain")

    def admin_post(self, path: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
        except ValueError:
            body = ""
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

        parsed = urllib.parse.urlsplit(self.path)
        if path == "/admin/login":
            password = form.get("password", "")
            config = self.store.config or {}
            if not verify_password(password, config.get("admin_hash", "")):
                return self.respond(200, self.login_page({"err": ["管理员密码不正确"]}), ctype="text/html; charset=utf-8")
            stamp = str(int(time.time()))
            sig = hmac.new(config["secret"].encode(), stamp.encode(), hashlib.sha256).hexdigest()
            cookie = f"gs_session={stamp}.{sig}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200"
            return self.respond(302, b"", headers={"Location": "/admin", "Set-Cookie": cookie})

        if not self.session_valid(self.session()):
            return self.respond(302, b"", headers={"Location": "/admin/login"})
        if form.get("csrf", "") != self.csrf():
            return self.respond(400, b"bad csrf token", ctype="text/plain")

        action = parsed.path.rsplit("/", 1)[-1]
        if action == "device-add":
            name = (form.get("name") or "").strip()[:32]
            space = (form.get("space") or "default").strip()[:40]
            password = form.get("password") or generate_password()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name or ""):
                return self.redirect("/admin", "设备名只能用字母数字 . _ -")
            if name in self.store.devices["devices"]:
                return self.redirect("/admin", "设备名已存在")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", space):
                return self.redirect("/admin", "空间名不合法")
            self.store.add_device(name, space, password)
            self.store.space_dir(space).mkdir(parents=True, exist_ok=True)
            return self.respond(
                200,
                page(
                    "设备已创建",
                    self.credential_block(name, space, password),
                    '<a href="/admin">概览</a>',
                ),
                ctype="text/html; charset=utf-8",
            )
        if action == "device-token":
            name = form.get("name", "")
            record = self.store.device(name)
            if not record:
                return self.redirect("/admin", "设备不存在")
            password = generate_password()
            record["hash"] = hash_password(password)
            self.store.save_devices()
            return self.respond(
                200,
                page("密码已重置", self.credential_block(name, record["space"], password), '<a href="/admin">概览</a>'),
                ctype="text/html; charset=utf-8",
            )
        if action == "device-toggle":
            record = self.store.device(form.get("name", ""))
            if record:
                record["enabled"] = not record.get("enabled", True)
                self.store.save_devices()
            return self.redirect("/admin", "状态已切换")
        if action == "device-delete":
            self.store.devices["devices"].pop(form.get("name", ""), None)
            self.store.save_devices()
            return self.redirect("/admin", "设备已删除")
        if action == "admin-passwd":
            password = form.get("password", "")
            if len(password) < 10:
                return self.redirect("/admin", "管理员密码至少 10 位")
            self.store.config["admin_hash"] = hash_password(password)
            self.store.save_config()
            return self.redirect("/admin", "管理员密码已更新")
        if action == "rollback":
            space = form.get("space", "default")
            vid = form.get("id", "")
            if not re.fullmatch(r"[\dTZ.:-]+\.json", vid):
                return self.redirect("/admin/versions", "版本号不合法")
            source = self.store.version_dir(space) / vid
            if not source.is_file():
                return self.redirect("/admin/versions", "版本不存在")
            target = self.store.space_file(space)
            with space_lock(space):
                if target.is_file():
                    self.store.snapshot(space, target.read_bytes())
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(source.read_bytes())
                os.replace(tmp, target)
            return self.redirect("/admin/versions", f"已回滚到 {vid}，各设备下次同步即生效")
        return self.respond(404, b"unknown action", ctype="text/plain")

    def redirect(self, location: str, message: str) -> None:
        target = f"{location}?ok={urllib.parse.quote(message)}"
        return self.respond(302, b"", headers={"Location": target})

    # -- admin views -------------------------------------------------------
    def login_page(self, query: dict) -> bytes:
        error = query.get("err", [""])[0]
        banner = f"<div class='flash' style='background:#2a1b1b;border-color:#4a2a2a'>{html.escape(error)}</div>" if error else ""
        return page(
            "登录",
            f"<div style='max-width:360px;margin:8vh auto'><h2>管理员登录</h2>{banner}"
            "<form method=post action=/admin/login>"
            "<input type=password name=password placeholder=管理员密码 autofocus>"
            "<div style='height:12px'></div><button style='width:100%'>登录</button></form></div>",
        )

    def credential_block(self, name: str, space: str, password: str) -> str:
        url = f"{self.public_url}/dav/{space}/{FILE_NAME}"
        return (
            f"<h2>设备「{html.escape(name)}」的配置</h2>"
            "<div class=cred><div class=muted>密码只显示这一次，丢了就点「重置密码」。</div>"
            f"<div>地址：<code>{html.escape(url)}</code></div>"
            f"<div>账号：<code>{html.escape(name)}</code></div>"
            f"<div>密码：<code>{html.escape(password)}</code></div></div>"
            "<p class=muted>在 app 里：设置 → 多端网络同步 → WebDAV，把上面三项填进去。</p>"
        )

    def dashboard(self, query: dict) -> str:
        devices = self.store.devices["devices"]
        state = self.store.state
        spaces = sorted({d["space"] for d in devices.values()} | set(self.store.spaces()))
        space = query.get("space", [spaces[0] if spaces else "default"])[0]

        cards = [
            ("设备", str(len(devices))),
            ("空间", str(len(spaces))),
            ("当前文件", self.current_size(space)),
            ("最后同步", fmt_time((state["spaces"].get(space) or {}).get("updated", ""))),
        ]
        card_html = "".join(
            f"<div class=card><div class=k>{k}</div><div class=v>{html.escape(v)}</div></div>"
            for k, v in cards
        )

        rows = []
        for name, rec in sorted(devices.items()):
            res = state["devices"].get(name, {})
            badge = "<span class='pill ok'>启用</span>" if rec.get("enabled", True) else "<span class='pill off'>停用</span>"
            rows.append(
                f"<tr><td><code>{html.escape(name)}</code></td><td>{html.escape(rec['space'])}</td>"
                f"<td>{badge}</td><td>{fmt_time(res.get('last_get', ''))}</td>"
                f"<td>{fmt_time(res.get('last_put', ''))}</td><td>{fmt_size(res.get('size', 0))}</td>"
                "<td><form class=inline method=post action=/admin/device-token>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='ghost'>重置密码</button></form> "
                "<form class=inline method=post action=/admin/device-toggle>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='ghost'>启/停</button></form> "
                "<form class=inline method=post action=/admin/device-delete onsubmit=\"return confirm('删除设备?')\">"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='danger'>删除</button></form></td></tr>"
            )
        table = (
            "<table><tr><th>设备</th><th>空间</th><th>状态</th><th>最后读取</th><th>最后写入</th><th>大小</th><th>操作</th></tr>"
            + ("".join(rows) or "<tr><td colspan=7 class=muted>还没有设备，先在下面建一个。</td></tr>")
            + "</table>"
        )

        options = "".join(f"<option value='{html.escape(s)}'>{html.escape(s)}</option>" for s in spaces or ["default"])
        add_form = (
            "<h2>新增设备</h2><div class=card><form method=post action=/admin/device-add>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            "<div class=row><div style='flex:1;min-width:160px'><input type=text name=name placeholder='设备名，如 ipad' required></div>"
            f"<div style='flex:1;min-width:160px'><select name=space>{options}</select></div>"
            "<div style='flex:1;min-width:200px'><input type=text name=password placeholder='密码（留空自动生成）'></div>"
            "<div><button>创建设备</button></div></div></form></div>"
        )

        return (
            self.flash(query)
            + f"<div class=cards>{card_html}</div>"
            + f"<h2>空间 {html.escape(space)} 的内容</h2><div class=card>{self.payload_summary(space)}</div>"
            + "<h2>设备</h2>" + table
            + add_form
            + "<h2>安全</h2><div class=card><form method=post action=/admin/admin-passwd class=row>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            "<div style='flex:1;min-width:240px'><input type=password name=password placeholder='新的管理员密码（至少 10 位）'></div>"
            "<div><button class=ghost>改管理员密码</button></div></form>"
            "<p class=muted style='margin:12px 0 0'>只影响面板登录；设备同步用的是各自的账号密码。</p></div>"
        )

    def current_size(self, space: str) -> str:
        target = self.store.space_file(space)
        return fmt_size(target.stat().st_size) if target.is_file() else "空"

    def payload_summary(self, space: str) -> str:
        target = self.store.space_file(space)
        if not target.is_file():
            return "<span class=muted>还没有任何设备同步过来。</span>"
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            return "<span class=muted>文件不是合法 JSON。</span>"
        updated = data.get("updatedAt") or data.get("updated") or 0
        try:
            stamp = datetime.fromtimestamp(int(updated) / 1000, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M") if updated else "—"
        except (ValueError, OSError):
            stamp = "—"
        counts = [
            ("观看进度", len(data.get("watch_history") or [])),
            ("收藏", len(data.get("favorites") or [])),
            ("订阅源", len(data.get("subscriptions") or data.get("subs") or [])),
        ]
        chips = "".join(f"<span class=pill style='margin-right:8px'>{k} {v}</span>" for k, v in counts)
        return (
            f"<div class=muted>数据更新于 {stamp} · schema {html.escape(str(data.get('schema', '?')))} v{html.escape(str(data.get('version', '?')))}</div>"
            f"<div style='margin-top:10px'>{chips}</div>"
        )

    def versions_page(self, query: dict) -> str:
        spaces = sorted(set(self.store.spaces()) | set(self.store.state["spaces"]))
        space = query.get("space", [spaces[0] if spaces else "default"])[0]
        options = "".join(
            f"<option value='{html.escape(s)}' {'selected' if s == space else ''}>{html.escape(s)}</option>"
            for s in spaces or ["default"]
        )
        rows = []
        for item in self.store.versions(space):
            rows.append(
                f"<tr><td class=mono>{html.escape(item['id'][:-5])}</td>"
                f"<td>{item['mtime'].astimezone().strftime('%Y-%m-%d %H:%M:%S')}</td>"
                f"<td>{fmt_size(item['size'])}</td>"
                f"<td><a href='/admin/versions/download?space={urllib.parse.quote(space)}&id={urllib.parse.quote(item['id'])}'>下载</a> "
                "<form class=inline method=post action=/admin/rollback onsubmit=\"return confirm('把当前数据回滚到这个版本?')\">"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=space value='{html.escape(space)}'>"
                f"<input type=hidden name=id value='{html.escape(item['id'])}'><button class='ghost'>回滚</button></form></td></tr>"
            )
        table = (
            "<table><tr><th>版本</th><th>时间</th><th>大小</th><th>操作</th></tr>"
            + ("".join(rows) or "<tr><td colspan=4 class=muted>还没有历史版本。</td></tr>")
            + "</table>"
        )
        return (
            self.flash(query)
            + f"<h2>历史版本 · 空间 <code>{html.escape(space)}</code></h2>"
            + f"<form method=get action=/admin/versions class=row><select name=space onchange=this.form.submit()>{options}</select></form>"
            "<div class=card style='margin-top:14px'>" + table + "</div>"
            "<p class=muted>每次设备成功上传新数据前，旧内容都会存成一份快照。回滚会把选中的版本重新写成当前数据，各设备下次同步就会拿到它。</p>"
        )

    def log_page(self, query: dict) -> str:
        limit = min(int(query.get("limit", ["200"])[0] or 200), 500)
        device = query.get("device", [""])[0]
        rows = []
        for item in self.store.recent_log(limit, device):
            rows.append(
                f"<tr><td class=mono>{html.escape(fmt_time(item.get('ts', '')))}</td>"
                f"<td>{html.escape(str(item.get('device') or '-'))}</td>"
                f"<td class=mono>{html.escape(item.get('method', ''))}</td>"
                f"<td>{html.escape(str(item.get('status', '')))}</td>"
                f"<td>{fmt_size(item.get('bytes', 0))}</td>"
                f"<td class=mono>{html.escape(item.get('ip', ''))}</td>"
                f"<td class=muted style='max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{html.escape(item.get('ua', ''))}</td></tr>"
            )
        table = (
            "<table><tr><th>时间</th><th>设备</th><th>方法</th><th>状态</th><th>大小</th><th>IP</th><th>UA</th></tr>"
            + ("".join(rows) or "<tr><td colspan=7 class=muted>暂无记录。</td></tr>")
            + "</table>"
        )
        return f"<h2>访问日志（最近 {limit} 条）</h2><div class=card>{table}</div>"


# --------------------------------------------------------------------------- bootstrap


def generate_password(length: int = 16) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def init_store(root: Path, public_url: str) -> Store:
    store = Store(root)
    if store.config is None:
        password = generate_password(14)
        store.config = {
            "secret": secrets.token_hex(32),
            "admin_hash": hash_password(password),
            "public_url": public_url,
            "max_bytes": 8 * 1024 * 1024,
            "max_versions": 50,
            "retention_days": 30,
        }
        store.save_config()
        print("=" * 62)
        print("  管理员密码（只显示这一次，请立刻保存）：", password)
        print(f"  面板地址：{public_url}/admin")
        print("=" * 62)
    return store


def main(argv: list[str]) -> int:
    root = Path(os.environ.get("GOMOVIE_SYNC_ROOT") or Path(__file__).resolve().parent / "data-root")
    public_url = os.environ.get("GOMOVIE_SYNC_URL", "http://127.0.0.1:8765").rstrip("/")
    listen = os.environ.get("GOMOVIE_SYNC_LISTEN", "0.0.0.0")
    port = int(os.environ.get("GOMOVIE_SYNC_PORT", "8765"))

    if "--init" in argv:
        init_store(root, public_url)
        return 0

    store = init_store(root, public_url)
    Handler.store = store
    Handler.public_url = (store.config or {}).get("public_url", public_url).rstrip("/")
    httpd = ThreadingHTTPServer((listen, port), Handler)
    httpd.daemon_threads = True
    print(f"gomovie-sync listening on {listen}:{port}  root={root}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
