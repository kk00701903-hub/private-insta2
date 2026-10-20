from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass
class Brand:
    bg: str = "#F8F6F2"
    accent: str = "#1A1A1A"
    text: str = "#1A1A1A"
    muted: str = "#6B6B6B"


@dataclass
class AppConfig:
    account: str = "top.pick00"
    output_dir: Path = field(default_factory=lambda: ROOT / "output")
    music_dir: Path = field(default_factory=lambda: ROOT / "music")
    fonts_dir: Path = field(default_factory=lambda: ROOT / "assets" / "fonts")
    default_card_count: int = 5
    default_seconds_per_slide: float = 5.0
    default_hashtags: list[str] = field(default_factory=list)
    brand: Brand = field(default_factory=Brand)
    card_width: int = 1080
    card_height: int = 1350
    reel_width: int = 1080
    reel_height: int = 1920
    # secrets / env
    claude_api_key: str | None = None
    claude_model: str = "claude-sonnet-5-5"
    ig_user_id: str | None = None
    ig_access_token: str | None = None
    fb_app_id: str | None = None
    fb_app_secret: str | None = None
    imgbb_api_key: str | None = None
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None
    public_base_url: str | None = None
    public_media_dir: Path | None = None
    kling_api_key: str | None = None
    kling_secret_key: str | None = None
    kling_base_url: str = "https://api-singapore.klingai.com"
    # cli | api | auto (prefer official `kling` CLI, fall back to REST)
    kling_backend: str = "auto"
    kling_cli: str | None = None
    kling_image_model: str | None = None
    kling_aspect_ratio: str = "3:4"
    kling_poll_seconds: int = 120


def _path(value: str | Path | None, default: Path) -> Path:
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(ROOT / ".env")
    path = config_path or (PACKAGE_DIR / "config.yaml")
    raw: dict = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    brand_raw = raw.get("brand") or {}
    brand = Brand(
        bg=brand_raw.get("bg", "#F8F6F2"),
        accent=brand_raw.get("accent", "#1A1A1A"),
        text=brand_raw.get("text", "#1A1A1A"),
        muted=brand_raw.get("muted", "#6B6B6B"),
    )
    card = raw.get("card") or {}
    reel = raw.get("reel_frame") or {}
    public_media = os.getenv("PUBLIC_MEDIA_DIR") or "data/public"

    return AppConfig(
        account=str(raw.get("account") or "top.pick00"),
        output_dir=_path(raw.get("output_dir"), ROOT / "output"),
        music_dir=_path(raw.get("music_dir"), ROOT / "music"),
        fonts_dir=_path(raw.get("fonts_dir"), ROOT / "assets" / "fonts"),
        default_card_count=int(raw.get("default_card_count") or 5),
        default_seconds_per_slide=float(raw.get("default_seconds_per_slide") or 5),
        default_hashtags=list(raw.get("default_hashtags") or []),
        brand=brand,
        card_width=int(card.get("width") or 1080),
        card_height=int(card.get("height") or 1350),
        reel_width=int(reel.get("width") or 1080),
        reel_height=int(reel.get("height") or 1920),
        claude_api_key=os.getenv("CLAUDE_API_KEY") or None,
        claude_model=os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5",
        ig_user_id=os.getenv("IG_USER_ID") or None,
        ig_access_token=os.getenv("IG_ACCESS_TOKEN") or None,
        fb_app_id=os.getenv("FB_APP_ID") or None,
        fb_app_secret=os.getenv("FB_APP_SECRET") or None,
        imgbb_api_key=os.getenv("IMGBB_API_KEY") or None,
        cloudinary_cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME") or None,
        cloudinary_api_key=os.getenv("CLOUDINARY_API_KEY") or None,
        cloudinary_api_secret=os.getenv("CLOUDINARY_API_SECRET") or None,
        public_base_url=os.getenv("PUBLIC_BASE_URL") or None,
        public_media_dir=_path(public_media, ROOT / "data" / "public"),
        kling_api_key=os.getenv("KLING_API_KEY") or None,
        kling_secret_key=os.getenv("KLING_SECRET_KEY") or None,
        kling_base_url=(os.getenv("KLING_BASE_URL") or "https://api-singapore.klingai.com").rstrip("/"),
        kling_backend=(os.getenv("KLING_BACKEND") or "auto").strip().lower(),
        kling_cli=(os.getenv("KLING_CLI") or "").strip() or None,
        kling_image_model=(os.getenv("KLING_IMAGE_MODEL") or "").strip() or None,
        kling_aspect_ratio=(os.getenv("KLING_ASPECT_RATIO") or "3:4").strip(),
        kling_poll_seconds=int(os.getenv("KLING_POLL_SECONDS") or 120),
    )
