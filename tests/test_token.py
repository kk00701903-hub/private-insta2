from datetime import datetime, timedelta, timezone

import httpx
import pytest

from insta_auto import ig_token
from insta_auto.ig_token import (
    TokenRefreshError,
    days_left,
    expiry_from_obtained,
    refresh_long_lived_token,
    token_status,
    upsert_env,
)

TOKEN = "EAAG-old-secret-token"
NEW_TOKEN = "EAAG-new-secret-token"


@pytest.fixture(autouse=True)
def _clear_cache():
    ig_token._debug_cache.clear()
    yield
    ig_token._debug_cache.clear()


def _cfg(cfg, **kw):
    from dataclasses import replace

    return replace(cfg, **kw)


def test_expiry_is_60_days_after_obtained():
    exp = expiry_from_obtained("2026-09-01T00:00:00+00:00")
    assert exp == datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert expiry_from_obtained("") is None
    assert expiry_from_obtained("not a date") is None
    # plain date is accepted (local midnight)
    assert expiry_from_obtained("2026-09-01") is not None


def test_days_left_math():
    exp = datetime(2026, 10, 31, tzinfo=timezone.utc)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert days_left(exp, now=now) == pytest.approx(6.5)
    assert days_left(None, now=now) is None


def test_status_thresholds(cfg, monkeypatch):
    c = _cfg(cfg, ig_access_token=TOKEN)
    monkeypatch.setenv("IG_TOKEN_OBTAINED_AT", "2026-09-01T00:00:00+00:00")
    # expires 2026-10-31
    s = token_status(c, now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert s["days_left"] == 21 and not s["warn"] and not s["should_refresh"]
    s = token_status(c, now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert s["should_refresh"] and not s["warn"]  # 9 days
    s = token_status(c, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert s["warn"] and s["should_refresh"]  # 6 days
    s = token_status(c, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert s["expired"] and not s["should_refresh"]
    assert TOKEN not in str(s)
    assert s["source"] == "obtained_at" and not s["can_refresh"]


def test_status_without_token(cfg):
    s = token_status(cfg)
    assert s["has_token"] is False and s["days_left"] is None and not s["warn"]


def test_debug_token_used_when_app_creds(cfg, monkeypatch):
    monkeypatch.setenv("IG_TOKEN_OBTAINED_AT", "2026-01-01")
    c = _cfg(cfg, ig_access_token=TOKEN, fb_app_id="123", fb_app_secret="appsecret")
    expires = datetime(2026, 12, 1, tzinfo=timezone.utc)
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        assert request.url.path.endswith("/debug_token")
        return httpx.Response(200, json={"data": {"expires_at": int(expires.timestamp())}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    s = token_status(c, now=now, client=client)
    assert s["source"] == "debug_token"
    assert s["days_left"] == 5 and s["warn"]
    token_status(c, now=now, client=client)
    assert len(calls) == 1  # cached


def test_debug_token_failure_falls_back(cfg, monkeypatch):
    monkeypatch.setenv("IG_TOKEN_OBTAINED_AT", "2026-09-01T00:00:00+00:00")
    c = _cfg(cfg, ig_access_token=TOKEN, fb_app_id="123", fb_app_secret="appsecret")

    def handler(request):
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    s = token_status(c, now=datetime(2026, 10, 1, tzinfo=timezone.utc), client=client)
    assert s["source"] == "obtained_at" and s["days_left"] == 30


def test_upsert_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nCLAUDE_API_KEY=abc\nIG_ACCESS_TOKEN=old\nIG_ACCESS_TOKEN=dup\n", encoding="utf-8")
    upsert_env(env, {"IG_ACCESS_TOKEN": "new", "IG_TOKEN_OBTAINED_AT": "2026-10-23"})
    assert env.read_text(encoding="utf-8") == (
        "# comment\nCLAUDE_API_KEY=abc\nIG_ACCESS_TOKEN=new\nIG_TOKEN_OBTAINED_AT=2026-10-23\n"
    )
    assert list(tmp_path.iterdir()) == [env]  # no temp files left


def test_refresh_updates_env_and_never_returns_token(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", TOKEN)  # restored after the test
    monkeypatch.setenv("IG_TOKEN_OBTAINED_AT", "2026-01-01")
    env = tmp_path / ".env"
    env.write_text(f"IG_ACCESS_TOKEN={TOKEN}\nFB_APP_ID=123\n", encoding="utf-8")
    c = _cfg(cfg, ig_access_token=TOKEN, fb_app_id="123", fb_app_secret="appsecret")

    def handler(request: httpx.Request):
        assert request.url.params["grant_type"] == "fb_exchange_token"
        return httpx.Response(200, json={"access_token": NEW_TOKEN, "expires_in": 5184000})

    result = refresh_long_lived_token(
        c, env_path=env, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert result["ok"] and result["expires_in_days"] == 60
    assert NEW_TOKEN not in str(result)
    text = env.read_text(encoding="utf-8")
    assert f"IG_ACCESS_TOKEN={NEW_TOKEN}" in text and "IG_TOKEN_OBTAINED_AT=" in text
    import os

    assert os.environ["IG_ACCESS_TOKEN"] == NEW_TOKEN


def test_refresh_error_is_redacted(cfg, tmp_path):
    c = _cfg(cfg, ig_access_token=TOKEN, fb_app_id="123", fb_app_secret="appsecret")

    def handler(request):
        return httpx.Response(400, json={"error": {"message": f"bad token {TOKEN} / appsecret"}})

    with pytest.raises(TokenRefreshError) as ei:
        refresh_long_lived_token(
            c, env_path=tmp_path / ".env", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
    assert TOKEN not in str(ei.value) and "appsecret" not in str(ei.value)
    assert not (tmp_path / ".env").exists()


def test_refresh_requires_app_creds(cfg):
    with pytest.raises(TokenRefreshError):
        refresh_long_lived_token(_cfg(cfg, ig_access_token=TOKEN))


def test_obtained_date_plus_days(monkeypatch):
    exp = expiry_from_obtained("2026-10-01")
    start = datetime(2026, 10, 1).astimezone(timezone.utc)
    assert exp - start == timedelta(days=60)
