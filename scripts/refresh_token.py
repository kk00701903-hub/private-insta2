"""Refresh a long-lived Facebook/Instagram access token and update .env.

Usage:
  python scripts/refresh_token.py
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from insta_auto.config import load_config  # noqa: E402

GRAPH = "https://graph.facebook.com/v22.0"


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    keys_done: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            keys_done.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in keys_done:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    cfg = load_config()
    if not (cfg.fb_app_id and cfg.fb_app_secret and cfg.ig_access_token):
        print("FB_APP_ID, FB_APP_SECRET, IG_ACCESS_TOKEN 가 .env에 필요합니다.", file=sys.stderr)
        return 1
    r = httpx.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": cfg.fb_app_id,
            "client_secret": cfg.fb_app_secret,
            "fb_exchange_token": cfg.ig_access_token,
        },
        timeout=30.0,
    )
    data = r.json()
    if not r.is_success or "access_token" not in data:
        print(f"실패: {data}", file=sys.stderr)
        return 1
    token = data["access_token"]
    _upsert_env(
        ROOT / ".env",
        {
            "IG_ACCESS_TOKEN": token,
            "IG_TOKEN_OBTAINED_AT": date.today().isoformat(),
        },
    )
    expires = data.get("expires_in")
    print(".env의 IG_ACCESS_TOKEN 갱신 완료")
    if expires:
        print(f"expires_in ≈ {int(expires) // 86400}일")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
