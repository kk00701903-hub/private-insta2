"""Refresh a long-lived Facebook/Instagram access token and update .env.

Usage:
  python scripts/refresh_token.py

The logic lives in ``insta_auto.ig_token.refresh_long_lived_token`` so the
API's background scheduler can reuse it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from insta_auto.config import load_config  # noqa: E402
from insta_auto.ig_token import (  # noqa: E402
    TokenRefreshError,
    refresh_long_lived_token,
)


def main() -> int:
    cfg = load_config()
    try:
        result = refresh_long_lived_token(cfg, env_path=ROOT / ".env")
    except TokenRefreshError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(".env의 IG_ACCESS_TOKEN 갱신 완료")
    if result.get("expires_in_days"):
        print(f"expires_in ≈ {result['expires_in_days']}일")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
