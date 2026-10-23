"""수익화 에이전트 설정.

- 기본값: ``insta_auto/config.yaml`` 의 ``monetize:`` 블록
- 비밀값(키·토큰): ``.env`` 에서만 읽는다 (코드/로그에 남기지 않는다)
- 런타임 스위치(전체 on/off 등): ``data/monetize/settings.json`` (PWA에서 변경)
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from insta_auto.config import PACKAGE_DIR, ROOT

DEFAULT_DISCLOSURE = (
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, "
    "이에 따른 일정액의 수수료를 제공받습니다."
)
DEFAULT_CTA = "댓글에 '링크' 남기면 DM으로 보내드려요"
DEFAULT_KEYWORDS = ["링크", "link", "정보"]
DEFAULT_DM_TEMPLATE = "요청하신 링크입니다.\n{link}\n\n{disclosure}"
DEFAULT_PUBLIC_REPLY = "DM 보냈어요!"

_lock = threading.Lock()


@dataclass
class MonetizeSettings:
    # --- 쿠팡 파트너스 ---
    disclosure_text: str = DEFAULT_DISCLOSURE
    cta_text: str = DEFAULT_CTA
    suggest_count: int = 5
    coupang_cache_minutes: int = 60
    # --- 댓글 → DM ---
    comment_dm_enabled: bool = False  # 전체 스위치 (기본 꺼짐)
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    dm_template: str = DEFAULT_DM_TEMPLATE
    public_reply_enabled: bool = True
    public_reply_text: str = DEFAULT_PUBLIC_REPLY
    max_dm_per_hour: int = 200
    min_seconds_between_dm: float = 3.0
    reply_window_days: int = 7
    poll_enabled: bool = True
    poll_interval_seconds: int = 120
    max_posts_to_poll: int = 20
    # facebook_login: POST graph.facebook.com/{page-id}/messages (이 프로젝트 기본 토큰 방식)
    # instagram_login: POST graph.instagram.com/{ig-user-id}/messages
    messaging_api: str = "facebook_login"

    def to_dict(self) -> dict:
        return asdict(self)


# 런타임(PWA)에서 바꿀 수 있는 항목
RUNTIME_KEYS = {
    "comment_dm_enabled",
    "keywords",
    "public_reply_enabled",
    "public_reply_text",
    "max_dm_per_hour",
    "poll_enabled",
    "poll_interval_seconds",
}

# Meta 문서상 비공개 답장 한도: 계정당 750회/시간. 그보다 확실히 낮게 묶는다.
HARD_MAX_DM_PER_HOUR = 500


def data_dir() -> Path:
    override = (os.getenv("MONETIZE_DATA_DIR") or "").strip()
    d = Path(override) if override else ROOT / "data" / "monetize"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runtime_path() -> Path:
    return data_dir() / "settings.json"


def _yaml_section(config_path: Path | None = None) -> dict:
    path = config_path or (PACKAGE_DIR / "config.yaml")
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return dict(raw.get("monetize") or {})


def _clean_keywords(values) -> list[str]:
    if isinstance(values, str):
        values = [v for v in values.replace("\n", ",").split(",")]
    out: list[str] = []
    for v in values or []:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


def _coerce(s: MonetizeSettings) -> MonetizeSettings:
    # 고지 문구는 비울 수 없다 (비우면 기본 문구로 되돌림)
    if not (s.disclosure_text or "").strip():
        s.disclosure_text = DEFAULT_DISCLOSURE
    s.disclosure_text = s.disclosure_text.strip()
    s.cta_text = (s.cta_text or "").strip()
    s.keywords = _clean_keywords(s.keywords) or list(DEFAULT_KEYWORDS)
    s.max_dm_per_hour = max(1, min(int(s.max_dm_per_hour), HARD_MAX_DM_PER_HOUR))
    s.min_seconds_between_dm = max(0.0, float(s.min_seconds_between_dm))
    # 7일 초과는 Meta 정책 위반이므로 1~7로 고정
    s.reply_window_days = max(1, min(int(s.reply_window_days), 7))
    s.poll_interval_seconds = max(30, int(s.poll_interval_seconds))
    s.suggest_count = max(1, min(int(s.suggest_count), 20))
    s.max_posts_to_poll = max(1, int(s.max_posts_to_poll))
    if s.messaging_api not in {"facebook_login", "instagram_login"}:
        s.messaging_api = "facebook_login"
    return s


def load_settings(config_path: Path | None = None) -> MonetizeSettings:
    base = MonetizeSettings()
    merged = base.to_dict()
    for k, v in _yaml_section(config_path).items():
        if k in merged and v is not None:
            merged[k] = v
    rp = _runtime_path()
    if rp.exists():
        try:
            runtime = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            runtime = {}
        for k, v in runtime.items():
            if k in RUNTIME_KEYS and v is not None:
                merged[k] = v
    return _coerce(MonetizeSettings(**merged))


def update_runtime_settings(patch: dict) -> MonetizeSettings:
    with _lock:
        rp = _runtime_path()
        current: dict = {}
        if rp.exists():
            try:
                current = json.loads(rp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        for k, v in (patch or {}).items():
            if k in RUNTIME_KEYS:
                current[k] = _clean_keywords(v) if k == "keywords" else v
        tmp = rp.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(rp)
    return load_settings()


@dataclass
class MonetizeSecrets:
    coupang_access_key: str | None = None
    coupang_secret_key: str | None = None
    coupang_sub_id: str | None = None
    fb_app_secret: str | None = None
    webhook_verify_token: str | None = None
    ig_user_id: str | None = None
    ig_access_token: str | None = None
    fb_page_id: str | None = None
    fb_page_access_token: str | None = None

    @property
    def has_coupang(self) -> bool:
        return bool(self.coupang_access_key and self.coupang_secret_key)

    def public_status(self) -> dict:
        """키 존재 여부만 반환 (값은 절대 반환하지 않음)."""
        return {
            "has_coupang_keys": self.has_coupang,
            "has_coupang_sub_id": bool(self.coupang_sub_id),
            "has_ig": bool(self.ig_user_id and self.ig_access_token),
            "has_page_token": bool(self.fb_page_id and self.fb_page_access_token),
            "has_webhook_verify_token": bool(self.webhook_verify_token),
            "has_app_secret": bool(self.fb_app_secret),
        }

    def secret_values(self) -> list[str]:
        return [
            v
            for v in (
                self.coupang_secret_key,
                self.coupang_access_key,
                self.fb_app_secret,
                self.webhook_verify_token,
                self.ig_access_token,
                self.fb_page_access_token,
            )
            if v
        ]


def load_secrets() -> MonetizeSecrets:
    load_dotenv(ROOT / ".env")

    def env(name: str) -> str | None:
        return (os.getenv(name) or "").strip() or None

    return MonetizeSecrets(
        coupang_access_key=env("COUPANG_ACCESS_KEY"),
        coupang_secret_key=env("COUPANG_SECRET_KEY"),
        coupang_sub_id=env("COUPANG_SUB_ID"),
        fb_app_secret=env("FB_APP_SECRET"),
        webhook_verify_token=env("META_WEBHOOK_VERIFY_TOKEN"),
        ig_user_id=env("IG_USER_ID"),
        ig_access_token=env("IG_ACCESS_TOKEN"),
        fb_page_id=env("FB_PAGE_ID"),
        fb_page_access_token=env("FB_PAGE_ACCESS_TOKEN"),
    )
