"""Playwright browser automation helpers.

Login and captcha are NEVER automated — the user signs in in the opened window.
A persistent profile keeps the session for later runs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from insta_auto.config import ROOT

PROFILE_DIR = Path(
    os.getenv("BROWSER_PROFILE_DIR") or (ROOT / "data" / "browser_profile")
)


def ensure_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "playwright가 없습니다. `.venv\\Scripts\\pip install playwright` 후 "
            "`playwright install chromium` 을 실행하세요."
        ) from exc


@contextmanager
def launch_persistent(
    *,
    headless: bool = False,
    channel: str | None = None,
) -> Iterator[tuple]:
    """Yield (playwright, context, page) with a durable user profile.

    channel='chrome' uses installed Google Chrome when available.
    """
    ensure_playwright()
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        kwargs: dict = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": headless,
            "viewport": {"width": 1280, "height": 900},
            "locale": "ko-KR",
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            context = p.chromium.launch_persistent_context(**kwargs)
        except Exception:
            # Fall back to bundled Chromium if system Chrome is missing
            kwargs.pop("channel", None)
            context = p.chromium.launch_persistent_context(**kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield p, context, page
        finally:
            context.close()


def open_url(url: str, *, channel: str | None = "chrome", wait: bool = True) -> None:
    """Open a URL and optionally wait until the user presses Enter in the terminal."""
    with launch_persistent(headless=False, channel=channel) as (_p, _ctx, page):
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        print(f"열린 주소: {url}")
        print(f"프로필: {PROFILE_DIR}")
        if wait:
            input("브라우저에서 작업이 끝나면 여기로 돌아와 Enter… ")
