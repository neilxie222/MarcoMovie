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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
        (root / "public").mkdir(parents=True, exist_ok=True)
        self.config = self._load_json(root / "config.json", None)
        self.devices = self._load_json(root / "devices.json", {"devices": {}})
        self.state = self._load_json(root / "state.json", {"devices": {}, "spaces": {}})
        self.site_targets: dict[str, str] = {}
        self.live_targets: dict[str, str] = {}
        self.load_sources()
        self._devices_mtime = self._devices_mtime_now()
        cfg = self.config or {}
        self.cache = GatewayCache(int(cfg.get("gateway_cache_bytes", 64 * 1024 * 1024)))
        self.limiter = RateLimiter(int(cfg.get("gateway_rate_per_min", 120)))
        self.cache_ttl = float(cfg.get("gateway_cache_ttl", 30))
        self.live_cache_ttl = float(cfg.get("gateway_live_cache_ttl", 300))

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
        self._devices_mtime = self._devices_mtime_now()

    def _devices_mtime_now(self) -> float:
        try:
            return (self.root / "devices.json").stat().st_mtime
        except OSError:
            return 0.0

    def _refresh_devices(self) -> None:
        """Pick up devices.json edits made by other processes (e.g. make_client_config.py)."""
        mtime = self._devices_mtime_now()
        if mtime and mtime != self._devices_mtime:
            self._devices_mtime = mtime
            self.devices = self._load_json(self.root / "devices.json", {"devices": {}})

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
    def add_device(self, name: str, space: str, password: str, expires: str = "", note: str = "") -> None:
        self.devices["devices"][name] = {
            "space": space,
            "hash": hash_password(password),
            "enabled": True,
            "created": utc_iso(),
            "expires": expires,  # "" = never expires; otherwise a YYYY-MM-DD day (UTC, inclusive)
            "note": note,
        }
        self.save_devices()

    def device(self, name: str):
        self._refresh_devices()
        return self.devices["devices"].get(name)

    def device_by_password(self, user: str, password: str):
        """Match name+password only; enable/expiry policy lives in the request handler."""
        self._refresh_devices()
        dev = self.devices["devices"].get(user)
        if not dev:
            return None
        return dev if verify_password(password, dev.get("hash", "")) else None

    # -- gateway tokens ----------------------------------------------------
    def token_for(self, name: str) -> str:
        """Stable per-account token embedded in that account's generated config."""
        rec = self.device(name)
        if rec is None:
            raise KeyError(name)
        token = rec.get("token")
        if not token:
            token = secrets.token_hex(12)
            rec["token"] = token
            self.save_devices()
        return token

    def device_by_token(self, token: str):
        if not token:
            return None
        self._refresh_devices()
        for name, rec in self.devices["devices"].items():
            if rec.get("token") and hmac.compare_digest(rec["token"], token):
                return name, rec
        return None

    # -- upstream sources behind the gateway -------------------------------
    def load_sources(self) -> None:
        """Read ROOT/private/sources.json (the real, unpublished config) into key->url maps."""
        self.site_targets = {}
        self.live_targets = {}
        path = self.root / "private" / "sources.json"
        try:
            master = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for site in master.get("sites") or []:
            key = (site.get("key") or "").strip()
            api = (site.get("api") or "").strip()
            if key and api.startswith(("http://", "https://")):
                self.site_targets[key] = api
        for index, live in enumerate(master.get("lives") or []):
            url = (live.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                self.live_targets[f"live{index}"] = url

    def source_target(self, key: str) -> str:
        return self.site_targets.get(key) or self.live_targets.get(key) or ""

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


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def expiry_state(expires: str) -> tuple[str, int]:
    """Return ("none" | "expired" | "soon" | "ok", days_left) for a YYYY-MM-DD expiry."""
    if not expires:
        return "none", 0
    try:
        end = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return "none", 0
    left = (end.date() - datetime.now(timezone.utc).date()).days
    if left < 0:
        return "expired", left
    return ("soon" if left <= 7 else "ok"), left


def account_state(record: dict) -> str:
    """"ok" when the account may be used, otherwise the reason to refuse it."""
    if not record.get("enabled", True):
        return "disabled"
    expires = record.get("expires") or ""
    if expires and expires < today_utc():
        return "expired"
    return "ok"


def extend_expiry(current: str, days: int) -> str:
    """Push an expiry out by `days`, counting from today when it already lapsed."""
    base = datetime.now(timezone.utc).date()
    if current:
        try:
            parsed = datetime.strptime(current, "%Y-%m-%d").date()
            if parsed > base:
                base = parsed
        except ValueError:
            pass
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def space_lock(space: str) -> threading.Lock:
    with LOCKS_GUARD:
        return LOCKS.setdefault(space, threading.Lock())


class GatewayCache:
    """Small in-process response cache in front of the upstreams.

    Keys are `<source key>?<query>`; identical requests from different accounts share one entry
    because the upstream payload does not depend on who asked. Only 200 responses are stored —
    errors must reach the client so an expiry or upstream outage is never masked by a stale hit.
    """

    def __init__(self, max_bytes: int):
        self.max_bytes = max(1024 * 1024, max_bytes)
        self.items: dict[str, tuple[float, int, str, bytes]] = {}
        self.bytes = 0
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[int, str, bytes] | None:
        now = time.time()
        with self.lock:
            entry = self.items.get(key)
            if entry is None or entry[0] < now:
                if entry is not None:
                    self.bytes -= len(entry[3])
                    self.items.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return entry[1], entry[2], entry[3]

    def put(self, key: str, ttl: float, status: int, ctype: str, body: bytes) -> None:
        if ttl <= 0 or status != 200 or len(body) > self.max_bytes // 4:
            return
        now = time.time()
        with self.lock:
            old = self.items.pop(key, None)
            if old is not None:
                self.bytes -= len(old[3])
            while self.bytes + len(body) > self.max_bytes and self.items:
                victim = min(self.items.items(), key=lambda kv: kv[1][0])[0]
                self.bytes -= len(self.items.pop(victim)[3])
            self.items[key] = (now + ttl, status, ctype, body)
            self.bytes += len(body)

    def stats(self) -> dict:
        with self.lock:
            return {"hits": self.hits, "misses": self.misses, "entries": len(self.items), "bytes": self.bytes}


class RateLimiter:
    """Token bucket per account: `per_minute` sustained, the same number as a burst ceiling."""

    def __init__(self, per_minute: int):
        self.capacity = float(max(5, per_minute))
        self.rate = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()
        self.limited = 0

    def allow(self, name: str) -> bool:
        now = time.time()
        with self.lock:
            tokens, last = self.buckets.get(name, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self.buckets[name] = (tokens, now)
                self.limited += 1
                return False
            self.buckets[name] = (tokens - 1.0, now)
            return True

    def retry_after(self, name: str) -> int:
        with self.lock:
            tokens, _ = self.buckets.get(name, (0.0, 0.0))
        return max(1, int((1.0 - tokens) / self.rate) + 1)


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

    def note(self, status: int, device: str, size: int = 0, extra: dict | None = None) -> None:
        entry = {
            "ts": utc_iso(),
            "ip": self.client_ip(),
            "device": device,
            "method": self.command,
            "path": self.path,
            "status": status,
            "bytes": size,
            "ua": self.headers.get("User-Agent", ""),
        }
        if extra:
            entry.update(extra)
        self.store.log(entry)

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/g/"):
            return self.gateway(path)
        if path.startswith("/config/"):
            return self.serve_config(path)
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

    # -- gateway: /g/<token>/<key>?<query> -> the real upstream API --------
    GATEWAY_UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36"
    GATEWAY_MAX = 8 * 1024 * 1024

    def gateway(self, path: str) -> None:
        """Proxy one upstream API call for an account, so expiry can actually revoke access.

        The generated client config points every site (and live list) here; the account's token
        decides whether the upstream is reached at all.
        """
        rest = path[len("/g/"):]
        token, _, key = rest.partition("/")
        hit = self.store.device_by_token(token)
        if hit is None:
            self.note(403, "gateway")
            return self.respond(403, b"unknown token", ctype="text/plain")
        name, record = hit
        state = account_state(record)
        if state != "ok":
            self.note(403, name)
            reason = "account expired on " + (record.get("expires") or "") if state == "expired" else "account disabled"
            return self.respond(403, reason.encode(), ctype="text/plain")
        key = key.strip("/")
        target = self.store.source_target(key)
        if not target:
            self.note(404, name)
            return self.respond(404, b"unknown source key", ctype="text/plain")
        if not self.store.limiter.allow(name):
            wait = self.store.limiter.retry_after(name)
            self.note(429, name, extra={"limited": True})
            return self.respond(
                429,
                b"too many requests, retry in %ds" % wait,
                headers={"Retry-After": str(wait)},
                ctype="text/plain",
            )
        query = urllib.parse.urlsplit(self.path).query
        cache_key = key + "?" + query
        cached = self.store.cache.get(cache_key)
        if cached is not None:
            status, ctype, body = cached
            self.note(status, name, len(body), {"cache": "hit"})
            return self.respond(status, body, headers={"Cache-Control": "no-store", "X-Cache": "hit"}, ctype=ctype)
        url = target + (("&" if "?" in target else "?") + query if query else "")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.GATEWAY_UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(self.GATEWAY_MAX)
                status = resp.status
                ctype = resp.headers.get("Content-Type") or "application/json; charset=utf-8"
        except urllib.error.HTTPError as exc:
            body = exc.read(self.GATEWAY_MAX)
            status = exc.code
            ctype = exc.headers.get("Content-Type") or "text/plain"
        except Exception:  # timeouts, DNS, TLS: never leak the upstream host in the response
            self.note(502, name)
            return self.respond(502, b"upstream unreachable", ctype="text/plain")
        ttl = self.store.live_cache_ttl if key.startswith("live") else self.store.cache_ttl
        self.store.cache.put(cache_key, ttl, status, ctype, body)
        self.note(status, name, len(body), {"cache": "miss"})
        return self.respond(status, body, headers={"Cache-Control": "no-store", "X-Cache": "miss"}, ctype=ctype)

    # -- static config files (app "导入配置地址" targets) -------------------
    def serve_config(self, path: str) -> None:
        """Serve ROOT/public/<name> so clients can import a config by URL. No auth: content only."""
        name = path[len("/config/"):]
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,60}\.json", name or ""):
            return self.respond(404, b"not found", ctype="text/plain")
        target = self.store.root / "public" / name
        if not target.is_file():
            return self.respond(404, b"not found", ctype="text/plain")
        body = target.read_bytes()
        self.note(200, "config", len(body))
        return self.respond(
            200,
            body,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            ctype="application/json; charset=utf-8",
        )

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
        if not record.get("enabled", True):
            self.drain()
            self.note(403, user)
            self.respond(403, b"account disabled", ctype="text/plain")
            return None
        state = account_state(record)
        if state == "expired":
            # Expired on purpose, not a bad password: say so in the body and in the log.
            self.drain()
            self.note(403, user)
            expires = (record.get("expires") or "").encode()
            self.respond(403, b"account expired on " + expires, ctype="text/plain")
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
            '<a href="/admin">概览</a><a href="/admin/edit">同步数据</a>'
            '<a href="/admin/configs">配置文件</a><a href="/admin/versions">版本</a>'
            '<a href="/admin/log">日志</a><a href="/admin/logout">退出</a>'
        )
        if path == "/admin":
            return self.respond(200, page("Gomovie Sync", self.dashboard(query), nav), ctype="text/html; charset=utf-8")
        if path == "/admin/edit":
            return self.respond(200, page("编辑同步数据", self.edit_page(query), nav), ctype="text/html; charset=utf-8")
        if path == "/admin/configs":
            return self.respond(200, page("配置文件", self.configs_page(query), nav), ctype="text/html; charset=utf-8")
        if path == "/admin/config-edit":
            return self.respond(200, page("编辑配置", self.config_edit_page(query), nav), ctype="text/html; charset=utf-8")
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
            expires = (form.get("expires") or "").strip()
            note = (form.get("note") or "").strip()[:60]
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name or ""):
                return self.redirect("/admin", "账号名只能用字母数字 . _ -")
            if name in self.store.devices["devices"]:
                return self.redirect("/admin", "账号名已存在")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", space):
                return self.redirect("/admin", "空间名不合法")
            if expires and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires):
                return self.redirect("/admin", "到期日格式应为 YYYY-MM-DD")
            self.store.add_device(name, space, password, expires, note)
            self.store.space_dir(space).mkdir(parents=True, exist_ok=True)
            return self.respond(
                200,
                page(
                    "账号已创建",
                    self.credential_block(name, space, password, expires),
                    '<a href="/admin">概览</a>',
                ),
                ctype="text/html; charset=utf-8",
            )
        if action == "device-token":
            name = form.get("name", "")
            record = self.store.device(name)
            if not record:
                return self.redirect("/admin", "账号不存在")
            password = generate_password()
            record["hash"] = hash_password(password)
            self.store.save_devices()
            return self.respond(
                200,
                page(
                    "密码已重置",
                    self.credential_block(name, record["space"], password, record.get("expires") or ""),
                    '<a href="/admin">概览</a>',
                ),
                ctype="text/html; charset=utf-8",
            )
        if action == "device-extend":
            record = self.store.device(form.get("name", ""))
            if not record:
                return self.redirect("/admin", "账号不存在")
            try:
                days = max(1, min(3650, int(form.get("days", "30"))))
            except ValueError:
                days = 30
            record["expires"] = extend_expiry(record.get("expires") or "", days)
            self.store.save_devices()
            return self.redirect("/admin", f"{form.get('name','')} 到期日已延到 {record['expires']}")
        if action == "device-expiry":
            record = self.store.device(form.get("name", ""))
            if not record:
                return self.redirect("/admin", "账号不存在")
            value = (form.get("expires") or "").strip()
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return self.redirect("/admin", "到期日格式应为 YYYY-MM-DD")
            record["expires"] = value
            self.store.save_devices()
            return self.redirect("/admin", f"{form.get('name','')} 到期日已改为 {value or '永久'}")
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
        if action == "edit-save":
            space = (form.get("space") or "default").strip()
            content = form.get("content", "")
            back = "/admin/edit?space=" + urllib.parse.quote(space)
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", space):
                return self.redirect(back, "空间名不合法")
            try:
                json.loads(content)
            except ValueError:
                return self.redirect(back, "JSON 格式错误，没有保存")
            target = self.store.space_file(space)
            with space_lock(space):
                if target.is_file():
                    self.store.snapshot(space, target.read_bytes())
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, target)
            return self.redirect(back, "已保存，旧内容已存为历史版本")
        if action == "config-save":
            name = form.get("name", "")
            content = form.get("content", "")
            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,60}\.json", name or ""):
                return self.respond(400, b"bad name", ctype="text/plain")
            back = "/admin/config-edit?name=" + urllib.parse.quote(name)
            try:
                json.loads(content)
            except ValueError:
                return self.redirect(back, "JSON 格式错误，没有保存")
            folder = self.store.root / "public"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            if target.is_file():
                history = folder / ".versions"
                history.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                (history / f"{name}.{stamp}").write_bytes(target.read_bytes())
                for old in sorted(history.glob(f"{name}.*"))[:-20]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            tmp = target.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            return self.redirect(back, "配置文件已保存（旧版本留在 public/.versions）")
        return self.respond(404, b"unknown action", ctype="text/plain")

    def redirect(self, location: str, message: str) -> None:
        joiner = "&" if "?" in location else "?"
        target = f"{location}{joiner}ok={urllib.parse.quote(message)}"
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

    def credential_block(self, name: str, space: str, password: str, expires: str = "") -> str:
        url = f"{self.public_url}/dav/{space}/{FILE_NAME}"
        expiry_line = (
            f"<div>到期：<code>{html.escape(expires)}</code>（当天有效，次日零点起该账号地址失效）</div>"
            if expires
            else "<div>到期：<code>永久</code></div>"
        )
        return (
            f"<h2>账号「{html.escape(name)}」的配置</h2>"
            "<div class=cred><div class=muted>密码只显示这一次，丢了就点「重置密码」。</div>"
            f"<div>地址：<code>{html.escape(url)}</code></div>"
            f"<div>账号：<code>{html.escape(name)}</code></div>"
            f"<div>密码：<code>{html.escape(password)}</code></div>"
            f"{expiry_line}</div>"
            "<p class=muted>在 app 里：设置 → 多端网络同步 → WebDAV，把上面三项填进去。</p>"
        )

    def edit_page(self, query: dict) -> str:
        """Textarea editor for the synced payload of a space."""
        spaces = sorted(set(self.store.spaces()) | set(self.store.state["spaces"]) | {"default"})
        space = query.get("space", [spaces[0]])[0]
        if space not in spaces:
            space = spaces[0]
        target = self.store.space_file(space)
        content = ""
        if target.is_file():
            try:
                content = json.dumps(
                    json.loads(target.read_text(encoding="utf-8")), ensure_ascii=False, indent=2
                )
            except ValueError:
                content = target.read_text(encoding="utf-8", errors="replace")
        options = "".join(
            f"<option value='{html.escape(s)}' {'selected' if s == space else ''}>{html.escape(s)}</option>"
            for s in spaces
        )
        return (
            self.flash(query)
            + f"<h2>编辑同步数据 · <code>{html.escape(space)}</code></h2>"
            + "<form method=get action=/admin/edit class=row>"
            f"<select name=space onchange=this.form.submit()>{options}</select>"
            "<span class=muted>切换空间后编辑对应的 marco-movie-sync.json</span></form>"
            "<div class=card style='margin-top:14px'><form method=post action=/admin/edit-save>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            f"<input type=hidden name=space value='{html.escape(space)}'>"
            "<textarea name=content spellcheck=false "
            "style='width:100%;min-height:460px;background:#16161a;color:#e8e8ee;border:1px solid #31313a;"
            "border-radius:12px;padding:12px;font-family:ui-monospace,Consolas,monospace;font-size:13px;"
            f"line-height:1.5'>{html.escape(content)}</textarea>"
            "<div class=row style='margin-top:12px'><button>保存</button>"
            "<span class=muted>保存前会把当前内容存成一份历史版本，改坏了能在「版本」页回滚。</span></div>"
            "</form></div>"
        )

    def configs_page(self, query: dict) -> str:
        """List the public config files the app can import by URL."""
        folder = self.store.root / "public"
        folder.mkdir(parents=True, exist_ok=True)
        rows = []
        for path in sorted(folder.glob("*.json")):
            stat = path.stat()
            stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            url = f"{self.public_url}/config/{urllib.parse.quote(path.name)}"
            rows.append(
                f"<tr><td><code>{html.escape(path.name)}</code></td><td>{fmt_size(stat.st_size)}</td>"
                f"<td>{stamp}</td><td><a href='{html.escape(url)}' target=_blank>打开</a> "
                f"<a href='/admin/config-edit?name={urllib.parse.quote(path.name)}'>编辑</a></td></tr>"
            )
        table = (
            "<table><tr><th>文件</th><th>大小</th><th>更新时间</th><th>操作</th></tr>"
            + ("".join(rows) or "<tr><td colspan=4 class=muted>public/ 下还没有配置文件。</td></tr>")
            + "</table>"
        )
        return (
            self.flash(query)
            + "<h2>配置文件</h2><div class=card>" + table + "</div>"
            "<p class=muted>这些文件通过 <code>/config/&lt;文件名&gt;</code> 公开提供，app 里导入配置时填的就是这个地址。"
            "编辑前会在 public/.versions 留一份旧版本。</p>"
        )

    def config_edit_page(self, query: dict) -> str:
        name = query.get("name", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,60}\.json", name or ""):
            return "<div class=flash>文件名不合法</div>"
        target = self.store.root / "public" / name
        if not target.is_file():
            return f"<div class=flash>文件不存在：<code>{html.escape(name)}</code></div>"
        try:
            content = json.dumps(
                json.loads(target.read_text(encoding="utf-8")), ensure_ascii=False, indent=2
            )
        except ValueError:
            content = target.read_text(encoding="utf-8", errors="replace")
        return (
            self.flash(query)
            + f"<h2>编辑 <code>{html.escape(name)}</code></h2>"
            "<div class=card><form method=post action=/admin/config-save>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            f"<input type=hidden name=name value='{html.escape(name)}'>"
            "<textarea name=content spellcheck=false "
            "style='width:100%;min-height:460px;background:#16161a;color:#e8e8ee;border:1px solid #31313a;"
            "border-radius:12px;padding:12px;font-family:ui-monospace,Consolas,monospace;font-size:13px;"
            f"line-height:1.5'>{html.escape(content)}</textarea>"
            "<div class=row style='margin-top:12px'><button>保存</button>"
            "<a class=btn href='/admin/configs'>返回</a></div></form></div>"
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
            expires = rec.get("expires") or ""
            kind, left = expiry_state(expires)
            if kind == "none":
                expiry_cell = "<span class=muted>永久</span>"
            elif kind == "expired":
                expiry_cell = f"<span class='pill off'>{html.escape(expires)} · 已过期</span>"
            else:
                cls = "pill off" if kind == "soon" else "pill"
                expiry_cell = f"<span class='{cls}'>{html.escape(expires)} · 剩 {left} 天</span>"
            note = rec.get("note") or ""
            rows.append(
                f"<tr><td><code>{html.escape(name)}</code>"
                + (f"<div class=muted style='font-size:12px'>{html.escape(note)}</div>" if note else "")
                + f"</td><td>{html.escape(rec['space'])}</td>"
                f"<td>{badge}</td><td>{expiry_cell}</td><td>{fmt_time(res.get('last_get', ''))}</td>"
                f"<td>{fmt_time(res.get('last_put', ''))}</td><td>{fmt_size(res.get('size', 0))}</td>"
                "<td><form class=inline method=post action=/admin/device-token>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='ghost'>重置密码</button></form> "
                "<form class=inline method=post action=/admin/device-extend>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<input type=hidden name=days value='30'><button class='ghost'>+30天</button></form> "
                "<form class=inline method=post action=/admin/device-expiry>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<input type=hidden name=expires value=''><button class='ghost'>改到期</button></form> "
                "<form class=inline method=post action=/admin/device-toggle>"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='ghost'>启/停</button></form> "
                "<form class=inline method=post action=/admin/device-delete onsubmit=\"return confirm('删除设备?')\">"
                f"<input type=hidden name=csrf value='{self.csrf()}'><input type=hidden name=name value='{html.escape(name)}'>"
                "<button class='danger'>删除</button></form></td></tr>"
            )
        table = (
            "<table><tr><th>设备</th><th>空间</th><th>状态</th><th>到期</th><th>最后读取</th><th>最后写入</th><th>大小</th><th>操作</th></tr>"
            + ("".join(rows) or "<tr><td colspan=8 class=muted>还没有设备，先在下面建一个。</td></tr>")
            + "</table>"
        )

        options = "".join(f"<option value='{html.escape(s)}'>{html.escape(s)}</option>" for s in spaces or ["default"])
        add_form = (
            "<h2>新增账号</h2><div class=card><form method=post action=/admin/device-add>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            "<div class=row><div style='flex:1;min-width:150px'><input type=text name=name placeholder='账号名，如 ipad' required></div>"
            "<div style='flex:1;min-width:180px'><input type=text name=password placeholder='密码（留空自动生成）'></div>"
            f"<div style='flex:0 0 130px'><select name=space>{options}</select></div>"
            "<div style='flex:0 0 170px'><input type=date name=expires title='到期日，留空=永久'></div>"
            "<div style='flex:1;min-width:150px'><input type=text name=note placeholder='备注（谁的设备）'></div>"
            "<div><button>创建</button></div></div>"
            "<p class=muted style='margin:12px 0 0'>到期日按 UTC 计算，当天仍可用，次日零点起该账号的地址立刻失效（403 account expired）。</p>"
            "</form></div>"
        )

        return (
            self.flash(query)
            + f"<div class=cards>{card_html}</div>"
            + f"<h2>空间 {html.escape(space)} 的内容</h2><div class=card>{self.payload_summary(space)}</div>"
            + self.gateway_summary()
            + "<h2>账号</h2>" + table
            + add_form
            + "<h2>安全</h2><div class=card><form method=post action=/admin/admin-passwd class=row>"
            f"<input type=hidden name=csrf value='{self.csrf()}'>"
            "<div style='flex:1;min-width:240px'><input type=password name=password placeholder='新的管理员密码（至少 10 位）'></div>"
            "<div><button class=ghost>改管理员密码</button></div></form>"
            "<p class=muted style='margin:12px 0 0'>只影响面板登录；设备同步用的是各自的账号密码。</p></div>"
        )

    def gateway_summary(self) -> str:
        cache = self.store.cache.stats()
        total = cache["hits"] + cache["misses"]
        pct = f"{cache['hits'] * 100 // total}%" if total else "—"
        limiter = self.store.limiter
        return (
            "<div class=card style='margin-top:14px'>"
            f"<div class=muted>网关自上次重启：命中 <b>{cache['hits']}</b> · 未命中 <b>{cache['misses']}</b>"
            f"（命中率 {pct}）· 限流拒绝 <b>{limiter.limited}</b> 次 · "
            f"缓存 {cache['entries']} 条 / {fmt_size(cache['bytes'])}</div>"
            f"<div class=muted style='margin-top:6px'>每账号 {int(limiter.capacity)} 次/分钟；"
            f"站点列表缓存 {int(self.store.cache_ttl)} 秒，直播列表 {int(self.store.live_cache_ttl)} 秒。"
            "改这些值：config.json 里的 gateway_* 字段，改完重启服务。</div></div>"
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
            "gateway_rate_per_min": 120,
            "gateway_cache_ttl": 30,
            "gateway_live_cache_ttl": 300,
            "gateway_cache_bytes": 64 * 1024 * 1024,
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
