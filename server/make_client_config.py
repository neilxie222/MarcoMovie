#!/usr/bin/env python3
"""Generate one client config per account, with every site/live URL pointed at the gateway.

    GOMOVIE_SYNC_ROOT=/var/lib/gomovie-sync python3 make_client_config.py kpad

Reads ROOT/private/sources.json (the real endpoints — never published), rewrites each site's
`api` and each live list's `url` to https://<public_url>/g/<token>/<key>, and writes
ROOT/public/<account>.json. Import that file's URL in the app: 设置 → 导入配置地址.

Why the indirection: the client keeps whatever config it imports, forever. Only traffic that
still hits our server can be revoked, so the config must never contain the real endpoints.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gomovie_sync as gs  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法: python3 make_client_config.py <账号名>", file=sys.stderr)
        return 2
    account = argv[0]
    root = Path(os.environ.get("GOMOVIE_SYNC_ROOT") or Path(__file__).resolve().parent / "data-root")
    store = gs.Store(root)
    record = store.device(account)
    if record is None:
        print(f"账号不存在: {account}", file=sys.stderr)
        return 1
    master_path = root / "private" / "sources.json"
    try:
        master = json.loads(master_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"读不到配置母版: {master_path}", file=sys.stderr)
        return 1

    public_url = (store.config or {}).get("public_url", "").rstrip("/")
    token = store.token_for(account)
    rewrote = 0

    kept_sites = []
    for site in master.get("sites") or []:
        if site.get("disabled"):
            continue
        key = (site.get("key") or "").strip()
        if key:
            site["api"] = f"{public_url}/g/{token}/{key}"
            rewrote += 1
        kept_sites.append(site)
    master["sites"] = kept_sites

    # Keep each live list's original index: the gateway path is /g/<token>/live<index>.
    kept_lives = []
    for index, live in enumerate(master.get("lives") or []):
        if live.get("disabled"):
            continue
        if (live.get("url") or "").startswith(("http://", "https://")):
            live["url"] = f"{public_url}/g/{token}/live{index}"
            rewrote += 1
        kept_lives.append(live)
    master["lives"] = kept_lives

    out = root / "public" / f"{account}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {out}  ({rewrote} 条地址指向网关)")
    print(f"app 里填: {public_url}/config/{account}.json")
    print(f"账号 {account} 到期日: {record.get('expires') or '永久'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
