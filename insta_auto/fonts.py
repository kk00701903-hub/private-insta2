from __future__ import annotations

from pathlib import Path

import httpx

from insta_auto.config import AppConfig, load_config

_NOTO = {
    "NotoSansKR-Regular.otf": (
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
        "Sans/SubsetOTF/KR/NotoSansKR-Regular.otf"
    ),
    "NotoSansKR-Medium.otf": (
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
        "Sans/SubsetOTF/KR/NotoSansKR-Medium.otf"
    ),
    "NotoSansKR-Bold.otf": (
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
        "Sans/SubsetOTF/KR/NotoSansKR-Bold.otf"
    ),
}

_WEIGHT_FILES = {
    "regular": "NotoSansKR-Regular.otf",
    "medium": "NotoSansKR-Medium.otf",
    "bold": "NotoSansKR-Bold.otf",
}


def font_path(weight: str = "regular", cfg: AppConfig | None = None) -> Path:
    cfg = cfg or load_config()
    name = _WEIGHT_FILES.get(weight, _WEIGHT_FILES["regular"])
    path = cfg.fonts_dir / name
    if path.exists():
        return path
    # Medium may be missing on some mirrors — fall back to Regular
    if weight == "medium":
        fallback = cfg.fonts_dir / _WEIGHT_FILES["regular"]
        if fallback.exists():
            return fallback
    return path


def ensure_fonts(cfg: AppConfig | None = None) -> None:
    cfg = cfg or load_config()
    cfg.fonts_dir.mkdir(parents=True, exist_ok=True)
    missing = [name for name in _NOTO if not (cfg.fonts_dir / name).exists()]
    if not missing:
        return
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for name in missing:
            dest = cfg.fonts_dir / name
            try:
                response = client.get(_NOTO[name])
                response.raise_for_status()
                dest.write_bytes(response.content)
            except Exception:
                if name.endswith("Medium.otf"):
                    continue
                raise
