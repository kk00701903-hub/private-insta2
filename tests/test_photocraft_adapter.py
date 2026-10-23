import json
from pathlib import Path

import pytest
from PIL import Image

from insta_auto import photocraft as pc

from .conftest import SAMPLES

# Command ids confirmed in `photocraft-cli commands --json` (v0.14.0).
KNOWN_COMMANDS = {
    "image.crop",
    "filter.blur.gaussianBlur",
    "image.adjustments.brightnessContrast",
    "image.adjustments.vibrance",
    "image.adjustments.curves",
    "image.adjustments.photoFilter",
    "image.adjustments.exposure",
    "image.adjustments.hueSaturation",
    "image.adjustments.blackWhite",
    "filter.noise.addNoise",
    "file.placeEmbedded",
    "layer.flattenImage",
    "type.create",
    "layer.layerStyle.dropShadow",
}


def test_presets_load_and_use_known_commands():
    presets = pc.load_presets()
    assert {"natural", "warm_film", "mono"} <= set(presets)
    for p in presets.values():
        assert p.label and p.steps
        for cid, params in p.steps:
            assert cid in KNOWN_COMMANDS, cid
            assert isinstance(params, dict)


def test_parse_steps_accepts_photocraft_forms():
    steps = pc._parse_steps(
        [["a.b", {"x": 1}], {"command": "c.d", "params": {"y": 2}}, {"id": "e.f"}, "g.h"], "t"
    )
    assert steps == [("a.b", {"x": 1}), ("c.d", {"y": 2}), ("e.f", {}), ("g.h", {})]
    with pytest.raises(pc.PhotoCraftError):
        pc._parse_steps([42], "t")


def test_cover_box_and_fill_mode():
    assert pc.cover_box(1080, 1350) == (160, 0, 759, 1350)  # wider than 9:16 -> crop sides
    assert pc.cover_box(1080, 2400) == (0, 240, 1080, 1920)  # taller -> crop top/bottom
    assert pc.fill_mode(1080, 1920) == "cover"
    assert pc.fill_mode(4000, 3000) == "blur"
    assert pc.fill_mode(4000, 3000, mode="cover") == "cover"


def test_build_still_commands_blur_fill(tmp_path):
    src = tmp_path / "a.jpg"
    look = [("image.adjustments.vibrance", {"vibrance": 10})]
    cmds = pc.build_still_commands(src, (4000, 3000), look)
    ids = [c for c, _ in cmds]
    assert ids == [
        "image.crop",
        "filter.blur.gaussianBlur",
        "image.adjustments.brightnessContrast",
        "file.placeEmbedded",
        "layer.flattenImage",
        "image.adjustments.vibrance",
    ]
    crop = cmds[0][1]
    assert (crop["targetWidth"], crop["targetHeight"], crop["resolution"]) == (1080, 1920, 72)
    place = cmds[3][1]
    assert place["path"] == str(src.resolve()) and place["scale"] == 27.0  # 1080/4000


def test_build_still_commands_cover_only():
    cmds = pc.build_still_commands(Path("p.png"), (1080, 1920), [])
    assert [c for c, _ in cmds] == ["image.crop"]


def test_build_run_args_and_overlay(cfg):
    blocks = pc.layout_text("가을 카페 추천", "창가 자리", position="bottom", fonts_dir=cfg.fonts_dir)
    assert len(blocks) == 2
    x0, y0, x1, y1 = pc.SAFE_ZONE
    for b in blocks:
        x, y, w, h = b.box
        assert x >= x0 and x + w <= x1 and y >= y0 and y + h <= y1  # inside the Reels safe zone
    new_doc, cmds = pc.build_overlay_commands(blocks)
    assert new_doc["background"] == "transparent"
    assert [c for c, _ in cmds] == ["type.create", "layer.layerStyle.dropShadow"] * 2
    assert cmds[0][1]["font"] == "Noto Sans KR" and cmds[0][1]["fontStyle"] == "Bold"

    args = pc.build_run_args(None, cmds, Path("o.png"), new_doc=new_doc)
    assert args[:2] == ["run", "--new"] and json.loads(args[2])["width"] == 1080
    assert args[-2:] == ["--out", "o.png"]
    i = args.index("--params")
    assert json.loads(args[i + 1])["text"].startswith("가을")

    args = pc.build_run_args(Path("in.jpg"), [("layer.flattenImage", {})], Path("o.png"))
    assert args == ["run", "in.jpg", "--cmd", "layer.flattenImage", "--out", "o.png"]


def test_resolve_cli_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOTOCRAFT_CLI", str(tmp_path / "missing.exe"))
    with pytest.raises(pc.PhotoCraftNotInstalled):
        pc.resolve_cli()
    fake = tmp_path / "photocraft-cli.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("PHOTOCRAFT_CLI", str(fake))
    assert pc.resolve_cli() == fake


def _check_outputs(shot):
    assert shot.still.exists() and shot.preview.exists() and shot.overlay.exists()
    assert Image.open(shot.still).size == (1080, 1920)
    ov = Image.open(shot.overlay)
    assert ov.mode == "RGBA" and ov.getextrema()[3][1] > 0  # some opaque text pixels


def test_fallback_to_pillow_when_not_installed(monkeypatch, tmp_path, cfg):
    def missing():
        raise pc.PhotoCraftNotInstalled("not installed (test)")

    monkeypatch.setattr(pc, "resolve_cli", missing)
    assert pc.available() is False
    shot = pc.prepare_shot(SAMPLES[0], tmp_path, 0, look="warm_film", title="제목", subtitle="부제", cfg=cfg)
    assert shot.engine == "pillow"
    assert any("Pillow" in w for w in shot.warnings)  # visible warning
    _check_outputs(shot)


def test_fallback_to_pillow_when_cli_fails(monkeypatch, tmp_path, cfg):
    def boom(*a, **k):
        raise pc.PhotoCraftError("PhotoCraft CLI exit 1: error: `image.crop`: bad")

    monkeypatch.setattr(pc, "run_cli", boom)
    shot = pc.prepare_shot(None, tmp_path, 1, look="mono", title="배경만", cfg=cfg)
    assert shot.engine == "pillow"
    assert "image.crop" in shot.warnings[0]
    _check_outputs(shot)


@pytest.mark.skipif(not pc.available(), reason="PhotoCraft CLI not installed (npm run photocraft:install)")
def test_real_photocraft_cli(tmp_path, cfg):
    shot = pc.prepare_shot(SAMPLES[1], tmp_path, 0, look="warm_film", title="실제 PhotoCraft", subtitle="테스트", cfg=cfg)
    assert shot.engine == "photocraft", shot.warnings
    _check_outputs(shot)
