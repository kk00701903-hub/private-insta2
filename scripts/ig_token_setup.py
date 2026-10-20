"""Guided Instagram Graph API token setup with Playwright + optional exchange.

Does NOT automate Meta login or captcha. Opens the right pages; you paste the
short-lived token; we exchange it for a long-lived token and update .env.

Usage (repo root, venv):
  python scripts/ig_token_setup.py
  python scripts/ig_token_setup.py --skip-browser
  python scripts/ig_token_setup.py --token <short_token>
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from insta_auto.browser import open_url  # noqa: E402
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


def exchange_long_lived(app_id: str, app_secret: str, short_token: str) -> dict:
    r = httpx.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=30.0,
    )
    data = r.json()
    if not r.is_success or "access_token" not in data:
        raise RuntimeError(f"장기 토큰 교환 실패: {data}")
    return data


def fetch_ig_user_id(token: str) -> str | None:
    """Try common paths to resolve Instagram business account id."""
    # 1) me/accounts → page → instagram_business_account
    r = httpx.get(
        f"{GRAPH}/me/accounts",
        params={"fields": "id,name,access_token,instagram_business_account", "access_token": token},
        timeout=30.0,
    )
    if r.is_success:
        for page in (r.json().get("data") or []):
            ig = page.get("instagram_business_account") or {}
            if ig.get("id"):
                return str(ig["id"])
    # 2) debug token / me
    r2 = httpx.get(
        f"{GRAPH}/me",
        params={"fields": "id,name", "access_token": token},
        timeout=30.0,
    )
    if r2.is_success:
        print("참고: /me =", r2.json())
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Instagram Graph 토큰 발급 도우미")
    parser.add_argument("--skip-browser", action="store_true", help="브라우저를 열지 않음")
    parser.add_argument("--token", default="", help="이미 있는 단기/사용자 토큰")
    parser.add_argument("--no-chrome", action="store_true")
    args = parser.parse_args()
    channel = None if args.no_chrome else "chrome"

    print("=== Instagram Graph API 토큰 준비 ===")
    print("로그인·캡차는 브라우저에서 직접 하세요. 이 스크립트는 페이지를 열고 .env만 갱신합니다.\n")

    if not args.skip_browser:
        print("1) Meta 앱 목록을 엽니다. 앱이 없으면 생성하세요.")
        open_url("https://developers.facebook.com/apps/", channel=channel)
        print("2) Graph API Explorer를 엽니다.")
        print("   - 앱 선택 → 권한에 instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement 추가")
        print("   - Generate Access Token")
        open_url("https://developers.facebook.com/tools/explorer/", channel=channel)

    cfg = load_config()
    short = (args.token or "").strip() or input("\nExplorer에서 받은 Access Token을 붙여넣으세요:\n> ").strip()
    if not short:
        print("토큰이 없습니다.", file=sys.stderr)
        return 1

    app_id = cfg.fb_app_id or input("FB_APP_ID (Meta 앱 ID): ").strip()
    app_secret = cfg.fb_app_secret or input("FB_APP_SECRET: ").strip()
    if not app_id or not app_secret:
        print("FB_APP_ID / FB_APP_SECRET가 필요합니다. .env에 넣거나 입력하세요.", file=sys.stderr)
        return 1

    print("\n장기 토큰으로 교환 중…")
    payload = exchange_long_lived(app_id, app_secret, short)
    long_token = payload["access_token"]
    expires = payload.get("expires_in")
    if expires:
        print(f"expires_in ≈ {int(expires) // 86400}일")

    ig_id = fetch_ig_user_id(long_token)
    if not ig_id:
        print("IG_USER_ID를 자동으로 못 찾았습니다. 비즈니스 계정·페이지 연결을 확인하세요.")
        ig_id = input("Instagram 사용자 ID (숫자)를 직접 입력 (엔터=건너뛰기): ").strip() or None

    updates = {
        "FB_APP_ID": app_id,
        "FB_APP_SECRET": app_secret,
        "IG_ACCESS_TOKEN": long_token,
        "IG_TOKEN_OBTAINED_AT": date.today().isoformat(),
    }
    if ig_id:
        updates["IG_USER_ID"] = ig_id

    env_path = ROOT / ".env"
    _upsert_env(env_path, updates)
    print(f"\n.env 갱신 완료: {env_path}")
    print("저장된 키:", ", ".join(updates.keys()), "(값은 출력하지 않음)")
    print("\n다음: IMGBB_API_KEY 또는 PUBLIC_BASE_URL 도 캐러셀 게시에 필요합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
