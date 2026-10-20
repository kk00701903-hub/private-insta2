"""Open common Meta / Instagram pages in a persistent Chrome profile.

Usage (repo root, venv):
  python scripts/browser_open.py meta
  python scripts/browser_open.py explorer
  python scripts/browser_open.py instagram
  python scripts/browser_open.py url https://example.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from insta_auto.browser import open_url  # noqa: E402

PAGES = {
    "meta": "https://developers.facebook.com/apps/",
    "explorer": "https://developers.facebook.com/tools/explorer/",
    "instagram": "https://www.instagram.com/",
    "business": "https://business.facebook.com/",
    "token": "https://developers.facebook.com/tools/debug/accesstoken/",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Playwright로 페이지 열기 (로그인 수동)")
    parser.add_argument(
        "target",
        help="meta | explorer | instagram | business | token | url",
    )
    parser.add_argument("url", nargs="?", help="target=url 일 때 주소")
    parser.add_argument(
        "--no-chrome",
        action="store_true",
        help="시스템 Chrome 대신 Playwright Chromium 사용",
    )
    args = parser.parse_args()
    channel = None if args.no_chrome else "chrome"

    if args.target == "url":
        if not args.url:
            print("url 인자가 필요합니다.", file=sys.stderr)
            return 2
        open_url(args.url, channel=channel)
        return 0

    key = args.target.lower()
    if key not in PAGES:
        print(f"알 수 없는 target: {args.target}", file=sys.stderr)
        print("가능:", ", ".join(PAGES), file=sys.stderr)
        return 2
    open_url(PAGES[key], channel=channel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
