from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from insta_auto.analyze import AnalysisResult, SlidePlan
from insta_auto.config import AppConfig, Brand
from insta_auto.fonts import ensure_fonts, font_path


def _rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _font(weight: str, size: int, cfg: AppConfig) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(weight, cfg)), size)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            trial = current + ch
            if font.getlength(trial) <= max_width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines or [""]


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    xy: tuple[int, int],
    max_width: int,
    fill: tuple[int, int, int],
    line_gap: int,
    max_lines: int | None = None,
) -> int:
    x, y = xy
    lines = wrap_text(text, font, max_width)
    if max_lines is not None:
        lines = lines[:max_lines]
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = font.getbbox(line or " ")
        y += (bbox[3] - bbox[1]) + line_gap
    return y


def _cover_photo(path: Path | None, size: tuple[int, int]) -> Image.Image:
    w, h = size
    if path is None or not path.exists():
        return Image.new("RGB", size, (40, 40, 40))
    img = Image.open(path).convert("RGB")
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = int(src_w * scale), int(src_h * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _overlay(base: Image.Image, brand: Brand, top_heavy: bool = True) -> Image.Image:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = base.size
    if top_heavy:
        for i in range(h // 2):
            alpha = int(160 * (1 - i / (h / 2)))
            draw.line([(0, i), (w, i)], fill=(0, 0, 0, alpha))
        for i in range(h // 3):
            y = h - 1 - i
            alpha = int(180 * (1 - i / (h / 3)))
            draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
    else:
        for i in range(h // 2):
            y = h - 1 - i
            alpha = int(200 * (1 - i / (h / 2)))
            draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def _render_slide(
    slide: SlidePlan,
    *,
    index: int,
    total: int,
    photo: Path | None,
    cfg: AppConfig,
    size: tuple[int, int],
) -> Image.Image:
    brand = cfg.brand
    margin = 72 if size[1] > 1400 else 64
    base = _cover_photo(photo, size)
    base = _overlay(base, brand, top_heavy=(slide.role == "cover"))
    draw = ImageDraw.Draw(base)
    w, h = size
    max_w = w - margin * 2
    white = (255, 255, 255)
    muted = (220, 220, 220)

    handle_font = _font("medium", 28, cfg)
    draw.text((margin, margin), f"@{cfg.account}", font=handle_font, fill=muted)

    counter_font = _font("medium", 26, cfg)
    label = f"{index + 1}/{total}"
    lw = counter_font.getlength(label)
    draw.text((w - margin - lw, margin), label, font=counter_font, fill=muted)

    if slide.role == "cover":
        title_font = _font("bold", 72, cfg)
        body_font = _font("medium", 36, cfg)
        y = h // 2 - 40
        y = _draw_wrapped(draw, slide.title, title_font, (margin, y), max_w, white, 12, 3)
        y += 28
        _draw_wrapped(draw, slide.body, body_font, (margin, y), max_w, muted, 10, 4)
    elif slide.role == "ending":
        title_font = _font("bold", 56, cfg)
        body_font = _font("medium", 34, cfg)
        y = h - margin - 280
        y = _draw_wrapped(draw, slide.title, title_font, (margin, y), max_w, white, 12, 2)
        y += 24
        _draw_wrapped(draw, slide.body, body_font, (margin, y), max_w, muted, 10, 3)
    else:
        title_font = _font("bold", 48, cfg)
        body_font = _font("medium", 34, cfg)
        y = h - margin - 320
        y = _draw_wrapped(draw, slide.title, title_font, (margin, y), max_w, white, 10, 2)
        y += 20
        _draw_wrapped(draw, slide.body, body_font, (margin, y), max_w, muted, 10, 5)

    return base


def render_cards(
    analysis: AnalysisResult,
    photos: list[Path],
    out_dir: Path,
    *,
    cfg: AppConfig,
    illustrated: dict[int, Path] | None = None,
    for_reel: bool = False,
) -> list[Path]:
    ensure_fonts(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    illustrated = illustrated or {}
    size = (
        (cfg.reel_width, cfg.reel_height)
        if for_reel
        else (cfg.card_width, cfg.card_height)
    )
    prefix = "reel_frame" if for_reel else "card"
    paths: list[Path] = []
    total = len(analysis.slides)

    for i, slide in enumerate(analysis.slides):
        photo: Path | None = None
        if i in illustrated:
            photo = illustrated[i]
        elif slide.photo_index is not None and photos:
            photo = photos[slide.photo_index]
        elif photos:
            photo = photos[min(i, len(photos) - 1)]

        img = _render_slide(slide, index=i, total=total, photo=photo, cfg=cfg, size=size)
        dest = out_dir / f"{prefix}_{i + 1:02d}.png"
        img.save(dest, "PNG", optimize=True)
        paths.append(dest)
    return paths
