"""Instagram long-lived token: expiry calculation and refresh.

규칙: 토큰 값 자체는 절대 로그·응답·예외 메시지에 넣지 않는다. 값은 `.env`에만 둔다.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from insta_auto.config import ROOT, AppConfig

GRAPH = "https://graph.facebook.com/v22.0"
TOKEN_LIFETIME_DAYS = 60
WARN_DAYS = 7
REFRESH_DAYS = 10
DEBUG_CACHE_SECONDS = 3600

_debug_cache: dict[str, tuple[datetime, datetime | None]] = {}


class TokenRefreshError(RuntimeError):
    pass


def _now(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def redact(text: str, *secrets: str | None) -> str:
    """Remove secret values from a message before it is logged or returned."""
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def parse_obtained_at(value: str | None) -> datetime | None:
    """IG_TOKEN_OBTAINED_AT: 'YYYY-MM-DD' (local date) or an ISO datetime."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            return datetime(d.year, d.month, d.day).astimezone(timezone.utc)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def expiry_from_obtained(obtained_at: str | None) -> datetime | None:
    start = parse_obtained_at(obtained_at)
    return start + timedelta(days=TOKEN_LIFETIME_DAYS) if start else None


def days_left(expires_at: datetime | None, *, now: datetime | None = None) -> float | None:
    if expires_at is None:
        return None
    return (expires_at - _now(now)).total_seconds() / 86400


def debug_token_expiry(
    cfg: AppConfig, *, client: httpx.Client | None = None, now: datetime | None = None
) -> datetime | None:
    """Ask Graph `debug_token` for the real expiry. Cached per token (by hash).

    Returns None when it cannot be determined (no app creds, network error,
    or a token that never expires).
    """
    if not (cfg.ig_access_token and cfg.fb_app_id and cfg.fb_app_secret):
        return None
    key = hashlib.sha256(cfg.ig_access_token.encode()).hexdigest()
    cached = _debug_cache.get(key)
    if cached and (_now(now) - cached[0]).total_seconds() < DEBUG_CACHE_SECONDS:
        return cached[1]
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        r = client.get(
            f"{GRAPH}/debug_token",
            params={
                "input_token": cfg.ig_access_token,
                "access_token": f"{cfg.fb_app_id}|{cfg.fb_app_secret}",
            },
        )
        data = (r.json() or {}).get("data") or {}
    except Exception:
        _debug_cache[key] = (_now(now), None)  # don't hammer the network on every poll
        return None
    finally:
        if own:
            client.close()
    expires = int(data.get("expires_at") or 0)
    result = datetime.fromtimestamp(expires, timezone.utc) if expires > 0 else None
    _debug_cache[key] = (_now(now), result)
    return result


def token_status(
    cfg: AppConfig,
    *,
    now: datetime | None = None,
    use_debug: bool = True,
    client: httpx.Client | None = None,
) -> dict:
    """Expiry info for /api/state. Never contains the token itself."""
    obtained_raw = os.getenv("IG_TOKEN_OBTAINED_AT") or None
    has_token = bool(cfg.ig_access_token)
    expires_at: datetime | None = None
    source = None
    if has_token and use_debug:
        expires_at = debug_token_expiry(cfg, client=client, now=now)
        if expires_at:
            source = "debug_token"
    if has_token and expires_at is None:
        expires_at = expiry_from_obtained(obtained_raw)
        if expires_at:
            source = "obtained_at"
    left = days_left(expires_at, now=now)
    can_refresh = bool(has_token and cfg.fb_app_id and cfg.fb_app_secret)
    return {
        "has_token": has_token,
        "obtained_at": obtained_raw,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_left": round(left, 2) if left is not None else None,
        "source": source,
        "expired": left is not None and left <= 0,
        "warn": left is not None and left < WARN_DAYS,
        "should_refresh": left is not None and 0 < left < REFRESH_DAYS,
        "can_refresh": can_refresh,
        "warn_days": WARN_DAYS,
        "refresh_days": REFRESH_DAYS,
    }


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Replace/append KEY=value lines, keeping every other line. Atomic write."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    keys_done: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            if key in keys_done:
                continue  # drop duplicate definitions
            out.append(f"{key}={updates[key]}")
            keys_done.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in keys_done:
            out.append(f"{key}={val}")
    data = "\n".join(out).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_long_lived_token(
    cfg: AppConfig,
    *,
    env_path: Path | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Exchange the current long-lived token for a fresh one and update `.env`.

    Also updates ``os.environ`` so the running process (which loaded .env once
    without override) uses the new token immediately. Returns metadata only.
    """
    if not (cfg.fb_app_id and cfg.fb_app_secret and cfg.ig_access_token):
        raise TokenRefreshError("FB_APP_ID, FB_APP_SECRET, IG_ACCESS_TOKEN 가 .env에 필요합니다.")
    secrets = (cfg.ig_access_token, cfg.fb_app_secret)
    own = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        r = client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": cfg.fb_app_id,
                "client_secret": cfg.fb_app_secret,
                "fb_exchange_token": cfg.ig_access_token,
            },
        )
        data = r.json()
    except Exception as exc:
        raise TokenRefreshError(redact(f"토큰 갱신 요청 실패: {exc}", *secrets)) from None
    finally:
        if own:
            client.close()
    if not r.is_success or not isinstance(data, dict) or "access_token" not in data:
        err = data.get("error") if isinstance(data, dict) else data
        raise TokenRefreshError(redact(f"토큰 갱신 실패: {err}", *secrets))

    new_token = str(data["access_token"])
    obtained = date.today().isoformat()
    upsert_env(
        env_path or (ROOT / ".env"),
        {"IG_ACCESS_TOKEN": new_token, "IG_TOKEN_OBTAINED_AT": obtained},
    )
    os.environ["IG_ACCESS_TOKEN"] = new_token
    os.environ["IG_TOKEN_OBTAINED_AT"] = obtained
    _debug_cache.clear()
    expires_in = data.get("expires_in")
    return {
        "ok": True,
        "obtained_at": obtained,
        "expires_in_days": int(expires_in) // 86400 if expires_in else None,
    }
