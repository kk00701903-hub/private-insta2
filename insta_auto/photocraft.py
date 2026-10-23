"""PhotoCraft CLI adapter (https://github.com/storytold/photocraft, Apache-2.0).

PhotoCraft is an image editor; we use its headless `photocraft-cli` to prepare reel stills:
grade (look preset), crop/resize to 1080x1920 with smart fill, and render title/caption text
layers with the project's Noto Sans KR fonts, exported as PNG.

CLI syntax used here (checked against apps/photocraft-cli/src/lib.rs and `commands --json`
of v0.14.0):

    photocraft-cli run <file> --cmd <id> [--params <json>] ... --out <file.png>
    photocraft-cli run --new '{"width":..,"height":..,"background":"transparent"}' --cmd ... --out ...
    photocraft-cli batch --actions <look.json> --in <dir> --out <dir>
    photocraft-cli --version

Look presets live in insta_auto/photocraft_presets/*.json as {"label", "steps": [[id, params], ...],
"pillow": {...}}; "steps" is PhotoCraft's own action-step format, "pillow" is the approximate
look used by the Pillow fallback when the CLI is not installed.

If PhotoCraft is missing (or fails), every operation falls back to Pillow and returns a
visible warning instead of failing the whole reel.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from insta_auto.config import PACKAGE_DIR, ROOT, AppConfig

PRESETS_DIR = PACKAGE_DIR / "photocraft_presets"
TOOLS_DIR = ROOT / "tools" / "photocraft"
DEFAULT_LOOK = "natural"
REEL_SIZE = (1080, 1920)
FONT_FAMILY = "Noto Sans KR"

# Instagram Reels UI covers the top bar, the right-hand action column and the caption/footer.
# Text is kept inside this box (x0, y0, x1, y1) on a 1080x1920 frame.
SAFE_ZONE = (90, 260, 950, 1500)

Step = tuple[str, dict[str, Any]]


class PhotoCraftError(RuntimeError):
    pass


class PhotoCraftNotInstalled(PhotoCraftError):
    pass


# --------------------------------------------------------------------------------------------
# CLI discovery / invocation
# --------------------------------------------------------------------------------------------


def _binary_name() -> str:
    return "photocraft-cli.exe" if sys.platform == "win32" else "photocraft-cli"


def resolve_cli() -> Path:
    """PHOTOCRAFT_CLI env > tools/photocraft (npm run photocraft:install) > PATH."""
    env = (os.getenv("PHOTOCRAFT_CLI") or "").strip().strip('"')
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            return p
        raise PhotoCraftNotInstalled(f"PHOTOCRAFT_CLI={env} 경로에 파일이 없습니다.")

    marker = TOOLS_DIR / "current.json"
    if marker.exists():
        try:
            rel = json.loads(marker.read_text(encoding="utf-8")).get("cli")
            if rel and (ROOT / rel).is_file():
                return ROOT / rel
        except (OSError, ValueError):
            pass
    if TOOLS_DIR.exists():
        hits = sorted(p for p in TOOLS_DIR.rglob(_binary_name()) if p.is_file())
        if hits:
            return hits[-1]
    which = shutil.which("photocraft-cli")
    if which:
        return Path(which)
    raise PhotoCraftNotInstalled(
        "PhotoCraft CLI가 설치되어 있지 않습니다. `npm run photocraft:install` 을 실행하거나 "
        ".env 에 PHOTOCRAFT_CLI 경로를 지정하세요."
    )


def available() -> bool:
    try:
        resolve_cli()
        return True
    except PhotoCraftNotInstalled:
        return False


def _timeout() -> int:
    try:
        return int(os.getenv("PHOTOCRAFT_TIMEOUT") or 180)
    except ValueError:
        return 180


def font_env(fonts_dir: Path) -> dict[str, str]:
    """Extra env so the headless CLI also sees assets/fonts (it only scans platform font dirs).

    PhotoCraft (crates/text/src/fonts.rs, system_font_dirs) scans %LOCALAPPDATA%\\Microsoft\\
    Windows\\Fonts on Windows and $XDG_DATA_HOME/fonts on Linux, so we point those at a private
    folder holding copies of the project's Noto Sans KR files. macOS: fonts must be installed.
    """
    fonts = sorted(Path(fonts_dir).glob("*.otf")) + sorted(Path(fonts_dir).glob("*.ttf"))
    if not fonts:
        return {}
    base = TOOLS_DIR / "fontenv"
    if sys.platform == "win32":
        dest = base / "Microsoft" / "Windows" / "Fonts"
        key = "LOCALAPPDATA"
    elif platform.system() == "Linux":
        dest = base / "fonts"
        key = "XDG_DATA_HOME"
    else:
        return {}
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for f in fonts:
            target = dest / f.name
            if not target.exists() or target.stat().st_size != f.stat().st_size:
                shutil.copy2(f, target)
    except OSError:
        return {}
    return {key: str(base)}


def run_cli(
    args: list[str],
    *,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cli = resolve_cli()
    env = {**os.environ, **(extra_env or {})}
    try:
        proc = subprocess.run(
            [str(cli), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or _timeout(),
            env=env,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        raise PhotoCraftError(f"PhotoCraft CLI 시간 초과 ({timeout or _timeout()}s): {args[:2]}") from exc
    except OSError as exc:
        raise PhotoCraftError(f"PhotoCraft CLI 실행 실패: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise PhotoCraftError(f"PhotoCraft CLI exit {proc.returncode}: {tail}")
    return proc


def version() -> str:
    return run_cli(["--version"], timeout=30).stdout.strip()


def status() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False, "path": None, "version": None, "error": None}
    try:
        info["path"] = str(resolve_cli())
        info["available"] = True
        info["version"] = version()
    except PhotoCraftError as exc:
        info["error"] = str(exc)
    return info


# --------------------------------------------------------------------------------------------
# Look presets
# --------------------------------------------------------------------------------------------


@dataclass
class LookPreset:
    name: str
    label: str
    steps: list[Step]
    pillow: dict[str, Any] = field(default_factory=dict)
    description: str = ""


def _parse_steps(raw: Any, name: str) -> list[Step]:
    """Accept PhotoCraft's action-step forms: [id, params], {"command"|"id": id, "params"}, "id"."""
    if isinstance(raw, dict):
        raw = raw.get("steps") or raw.get("actions") or []
    if not isinstance(raw, list):
        raise PhotoCraftError(f"look '{name}': steps must be a list")
    steps: list[Step] = []
    for item in raw:
        if isinstance(item, str):
            steps.append((item, {}))
        elif isinstance(item, list) and item and isinstance(item[0], str):
            params = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            steps.append((item[0], params))
        elif isinstance(item, dict) and (item.get("command") or item.get("id")):
            steps.append((str(item.get("command") or item.get("id")), dict(item.get("params") or {})))
        else:
            raise PhotoCraftError(f"look '{name}': bad step {item!r}")
    return steps


def load_presets(presets_dir: Path | None = None) -> dict[str, LookPreset]:
    d = presets_dir or PRESETS_DIR
    out: dict[str, LookPreset] = {}
    for path in sorted(Path(d).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = path.stem
        out[name] = LookPreset(
            name=name,
            label=str(data.get("label") or name),
            steps=_parse_steps(data.get("steps", []), name),
            pillow=dict(data.get("pillow") or {}),
            description=str(data.get("description") or ""),
        )
    return out


def get_preset(name: str | None) -> LookPreset:
    presets = load_presets()
    if name and name in presets:
        return presets[name]
    if DEFAULT_LOOK in presets:
        return presets[DEFAULT_LOOK]
    return LookPreset(name=DEFAULT_LOOK, label="기본", steps=[])


# --------------------------------------------------------------------------------------------
# Command construction (pure functions, unit-tested)
# --------------------------------------------------------------------------------------------


def cover_box(src_w: int, src_h: int, size: tuple[int, int] = REEL_SIZE) -> tuple[int, int, int, int]:
    """Centered crop (x, y, w, h) of the source with the target aspect ratio."""
    tw, th = size
    target = tw / th
    if src_w / src_h > target:
        ch = src_h
        cw = max(1, round(src_h * target))
        return ((src_w - cw) // 2, 0, cw, ch)
    cw = src_w
    ch = max(1, round(src_w / target))
    return (0, (src_h - ch) // 2, cw, ch)


def fill_mode(src_w: int, src_h: int, size: tuple[int, int] = REEL_SIZE, mode: str = "auto") -> str:
    """'cover' when the photo is close to 9:16 (crop loses little), else 'blur' (blurred backdrop)."""
    if mode in {"cover", "blur"}:
        return mode
    ratio = (src_w / src_h) / (size[0] / size[1])
    return "cover" if 0.8 <= ratio <= 1.25 else "blur"


def build_still_commands(
    src: Path,
    src_size: tuple[int, int],
    look_steps: list[Step],
    *,
    size: tuple[int, int] = REEL_SIZE,
    fill: str = "auto",
) -> list[Step]:
    src_w, src_h = src_size
    x, y, w, h = cover_box(src_w, src_h, size)
    tw, th = size
    cmds: list[Step] = [
        (
            "image.crop",
            {
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "targetWidth": tw,
                "targetHeight": th,
                "resolution": 72,  # 72 ppi: type sizes in pt == px
            },
        )
    ]
    if fill_mode(src_w, src_h, size, fill) == "blur":
        scale = round(min(tw / src_w, th / src_h) * 100, 3)
        cmds += [
            ("filter.blur.gaussianBlur", {"radius": 48}),
            ("image.adjustments.brightnessContrast", {"brightness": -45, "contrast": -10}),
            (
                "file.placeEmbedded",
                {"path": str(Path(src).resolve()), "scale": scale, "fit": False, "smartObject": False},
            ),
            ("layer.flattenImage", {}),
        ]
    cmds += list(look_steps)
    return cmds


@dataclass
class TextBlock:
    text: str
    size: int
    weight: str  # bold | medium | regular
    box: tuple[int, int, int, int]  # x, y, w, h
    lines: list[str]


_WEIGHT_STYLE = {"bold": "Bold", "medium": "Medium", "regular": "Regular"}


def _pil_font(fonts_dir: Path, weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = {"bold": "NotoSansKR-Bold.otf", "medium": "NotoSansKR-Medium.otf"}.get(
        weight, "NotoSansKR-Regular.otf"
    )
    for candidate in (fonts_dir / name, fonts_dir / "NotoSansKR-Regular.otf"):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _wrap(text: str, font: Any, max_width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        current = ""
        for ch in paragraph:
            trial = current + ch
            if font.getlength(trial) <= max_width or not current:
                current = trial
            else:
                lines.append(current.rstrip())
                current = ch.lstrip()
        if current:
            lines.append(current)
    return lines[:max_lines]


def layout_text(
    title: str,
    subtitle: str = "",
    *,
    position: str = "center",
    fonts_dir: Path,
    size: tuple[int, int] = REEL_SIZE,
    safe_zone: tuple[int, int, int, int] = SAFE_ZONE,
) -> list[TextBlock]:
    """Wrap and place title/subtitle inside the Reels safe zone. Same layout for both engines."""
    x0, y0, x1, y1 = safe_zone
    sx, sy = size[0] / REEL_SIZE[0], size[1] / REEL_SIZE[1]
    x0, x1, y0, y1 = int(x0 * sx), int(x1 * sx), int(y0 * sy), int(y1 * sy)
    width = x1 - x0
    blocks: list[tuple[str, int, str, int]] = []
    if title.strip():
        blocks.append((title.strip(), int(76 * sy), "bold", 3))
    if subtitle.strip():
        blocks.append((subtitle.strip(), int(42 * sy), "medium", 3))
    laid: list[tuple[str, int, str, list[str], int]] = []
    total_h = 0
    gap = int(24 * sy)
    for text, fsize, weight, max_lines in blocks:
        font = _pil_font(fonts_dir, weight, fsize)
        lines = _wrap(text, font, width, max_lines)
        h = int(len(lines) * fsize * 1.35)
        laid.append((text, fsize, weight, lines, h))
        total_h += h
    total_h += gap * max(0, len(laid) - 1)
    if position == "top":
        y = y0
    elif position == "bottom":
        y = y1 - total_h
    else:
        y = y0 + (y1 - y0 - total_h) // 2
    y = max(y0, min(y, y1 - total_h))
    out: list[TextBlock] = []
    for text, fsize, weight, lines, h in laid:
        out.append(TextBlock(text="\n".join(lines), size=fsize, weight=weight, box=(x0, y, width, h), lines=lines))
        y += h + gap
    return out


def build_overlay_commands(blocks: list[TextBlock], *, size: tuple[int, int] = REEL_SIZE) -> tuple[dict, list[Step]]:
    """`run --new <json>` document + type layers with a soft drop shadow (transparent PNG)."""
    new_doc = {"width": size[0], "height": size[1], "background": "transparent", "resolution": 72}
    cmds: list[Step] = []
    for b in blocks:
        x, y, w, h = b.box
        cmds.append(
            (
                "type.create",
                {
                    "text": b.text,
                    "box": [x, y, w, h + b.size],  # paragraph text; extra room for descenders
                    "align": "center",
                    "font": FONT_FAMILY,
                    "fontStyle": _WEIGHT_STYLE.get(b.weight, "Regular"),
                    "size": b.size,
                    "color": "#ffffff",
                    "name": b.text[:20],
                },
            )
        )
        cmds.append(
            (
                "layer.layerStyle.dropShadow",
                {"color": "#000000", "opacity": 70, "angle": 90, "distance": 3, "size": 14},
            )
        )
    return new_doc, cmds


def cmd_args(cmds: list[Step]) -> list[str]:
    args: list[str] = []
    for cid, params in cmds:
        args += ["--cmd", cid]
        if params:
            args += ["--params", json.dumps(params, ensure_ascii=False)]
    return args


def build_run_args(src: Path | None, cmds: list[Step], out: Path, *, new_doc: dict | None = None) -> list[str]:
    head = ["run", str(src)] if src is not None else ["run", "--new", json.dumps(new_doc or {})]
    return [*head, *cmd_args(cmds), "--out", str(out)]


# --------------------------------------------------------------------------------------------
# Pillow fallback (same layout, approximate looks)
# --------------------------------------------------------------------------------------------


def pillow_still(src: Path, out: Path, look: LookPreset, *, size: tuple[int, int] = REEL_SIZE, fill: str = "auto") -> Path:
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    sw, sh = img.size
    x, y, w, h = cover_box(sw, sh, size)
    base = img.crop((x, y, x + w, y + h)).resize(size, Image.Resampling.LANCZOS)
    if fill_mode(sw, sh, size, fill) == "blur":
        base = base.filter(ImageFilter.GaussianBlur(48))
        base = ImageEnhance.Brightness(base).enhance(0.6)
        scale = min(size[0] / sw, size[1] / sh)
        fg = img.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.Resampling.LANCZOS)
        base.paste(fg, ((size[0] - fg.width) // 2, (size[1] - fg.height) // 2))
    base = apply_pillow_look(base, look.pillow)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out, "PNG")
    return out


def apply_pillow_look(img: Image.Image, p: dict[str, Any]) -> Image.Image:
    if not p:
        return img
    if p.get("grayscale"):
        img = ImageOps.grayscale(img).convert("RGB")
    for key, enh in (
        ("brightness", ImageEnhance.Brightness),
        ("contrast", ImageEnhance.Contrast),
        ("color", ImageEnhance.Color),
    ):
        if key in p:
            img = enh(img).enhance(float(p[key]))
    warmth = float(p.get("warmth") or 0)
    fade = int(p.get("fade") or 0)
    if warmth or fade:
        r, g, b = img.split()
        if warmth:
            r = r.point(lambda v: min(255, int(v * (1 + warmth))))
            b = b.point(lambda v: min(255, int(v * (1 - warmth))))
        img = Image.merge("RGB", (r, g, b))
        if fade:
            img = img.point(lambda v: int(fade + v * (255 - fade) / 255))
    return img


def pillow_overlay(blocks: list[TextBlock], out: Path, *, fonts_dir: Path, size: tuple[int, int] = REEL_SIZE) -> Path:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    sdraw = ImageDraw.Draw(shadow)
    for b in blocks:
        font = _pil_font(fonts_dir, b.weight, b.size)
        x, y, w, _ = b.box
        for i, line in enumerate(b.lines):
            lw = font.getlength(line)
            lx = x + (w - lw) / 2
            ly = y + i * b.size * 1.35
            sdraw.text((lx, ly + 3), line, font=font, fill=(0, 0, 0, 190))
            draw.text((lx, ly), line, font=font, fill=(255, 255, 255, 255))
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    merged = Image.alpha_composite(shadow, layer)
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.save(out, "PNG")
    return out


def gradient_base(out: Path, *, size: tuple[int, int] = REEL_SIZE, top: str = "#2b5bff", bottom: str = "#0b1220") -> Path:
    """Backdrop for a shot with no photo (free mode without photos)."""
    def rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    t, b = rgb(top), rgb(bottom)
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for yy in range(size[1]):
        k = yy / max(1, size[1] - 1)
        draw.line([(0, yy), (size[0], yy)], fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


@dataclass
class PreparedShot:
    still: Path  # graded 1080x1920, no text (input for Kling / Ken Burns)
    overlay: Path | None  # transparent PNG with title/caption text, composited at assembly
    preview: Path  # still + overlay, for the storyboard preview
    engine: str  # photocraft | pillow
    warnings: list[str] = field(default_factory=list)


def _photocraft_still(src: Path, out: Path, look: LookPreset, size: tuple[int, int], fill: str) -> None:
    with Image.open(src) as raw:
        rotated = raw.getexif().get(0x0112, 1) not in (1, None)
        exotic = raw.format not in {"JPEG", "PNG"}
        src_size = raw.size
        if rotated or exotic:
            # EXIF-rotated or unusual formats: hand PhotoCraft an upright PNG.
            upright_img = ImageOps.exif_transpose(raw).convert("RGB")
            src_size = upright_img.size
            src = out.with_name(out.stem + "_src.png")
            upright_img.save(src, "PNG")
    cmds = build_still_commands(src, src_size, look.steps, size=size, fill=fill)
    run_cli(build_run_args(src, cmds, out))
    if not out.exists():
        raise PhotoCraftError(f"PhotoCraft did not write {out}")


def _photocraft_overlay(blocks: list[TextBlock], out: Path, size: tuple[int, int], fonts_dir: Path) -> list[str]:
    new_doc, cmds = build_overlay_commands(blocks, size=size)
    proc = run_cli(build_run_args(None, cmds, out, new_doc=new_doc), extra_env=font_env(fonts_dir))
    if not out.exists():
        raise PhotoCraftError(f"PhotoCraft did not write {out}")
    # "N layer(s) flattened" is expected when exporting type layers to PNG; keep the rest
    # (e.g. a missing font) visible.
    return [
        line
        for line in (proc.stderr or "").splitlines()
        if line.startswith("warning:") and "flattened" not in line
    ]


def compose_preview(still: Path, overlay: Path | None, out: Path) -> Path:
    base = Image.open(still).convert("RGBA")
    if overlay and overlay.exists():
        ov = Image.open(overlay).convert("RGBA")
        if ov.size != base.size:
            ov = ov.resize(base.size)
        base = Image.alpha_composite(base, ov)
    base.convert("RGB").save(out, "JPEG", quality=88)
    return out


def prepare_shot(
    src: Path | None,
    out_dir: Path,
    index: int,
    *,
    look: str | None,
    title: str = "",
    subtitle: str = "",
    position: str = "center",
    cfg: AppConfig,
    size: tuple[int, int] = REEL_SIZE,
    fill: str = "auto",
    use_photocraft: bool = True,
) -> PreparedShot:
    out_dir.mkdir(parents=True, exist_ok=True)
    preset = get_preset(look)
    stem = f"shot_{index + 1:02d}"
    still = out_dir / f"{stem}.png"
    overlay = out_dir / f"{stem}_text.png"
    preview = out_dir / f"{stem}_preview.jpg"
    warnings: list[str] = []
    if src is None:
        src = gradient_base(out_dir / f"{stem}_bg.png", size=size)

    blocks = layout_text(title, subtitle, position=position, fonts_dir=cfg.fonts_dir, size=size)
    engine = "photocraft"
    try:
        if not use_photocraft:
            raise PhotoCraftNotInstalled("PhotoCraft 사용 안 함")
        _photocraft_still(Path(src), still, preset, size, fill)
        if blocks:
            warnings += _photocraft_overlay(blocks, overlay, size, cfg.fonts_dir)
    except PhotoCraftError as exc:
        engine = "pillow"
        if use_photocraft:
            warnings.append(f"PhotoCraft 대신 Pillow로 처리했습니다: {exc}")
        pillow_still(Path(src), still, preset, size=size, fill=fill)
        if blocks:
            pillow_overlay(blocks, overlay, fonts_dir=cfg.fonts_dir, size=size)
    ov = overlay if blocks else None
    compose_preview(still, ov, preview)
    return PreparedShot(still=still, overlay=ov, preview=preview, engine=engine, warnings=warnings)
