from pathlib import Path

import pytest

from insta_auto import kling_cli, kling_video


def test_image_to_video_args():
    args = kling_cli.build_image_to_video_args(Path("f.png"), "slow push-in", model="kling-video-v4_0_flash", duration=5, poll=600)
    assert args == [
        "image_to_video", "--model", "kling-video-v4_0_flash", "--image", "f.png",
        "--duration", "5", "--prompt", "slow push-in", "--poll=600",
    ]


def test_extract_video_urls_prefers_mp4():
    payload = {"data": {"cover": {"url": "https://x/cover.jpg"}, "videos": [{"url": "https://x/clip.mp4"}]}}
    assert kling_cli._extract_video_urls(payload)[0] == "https://x/clip.mp4"


def test_clip_seconds(monkeypatch):
    monkeypatch.delenv("KLING_VIDEO_DURATIONS", raising=False)
    assert kling_video.clip_seconds(3.5) == 5
    assert kling_video.clip_seconds(7.5) == 10
    assert kling_video.clip_seconds(12) == 10


def test_choose_backend_without_kling(monkeypatch, cfg):
    monkeypatch.setattr(kling_cli, "cli_available", lambda c=None: False)
    with pytest.raises(kling_video.KlingJobError):
        kling_video.choose_backend(cfg)
    cfg.kling_api_key = "not-a-real-key"
    assert kling_video.choose_backend(cfg) == "api"


def test_cli_failure_is_not_retried_on_rest(monkeypatch, cfg):
    calls = []

    def fail(*a, **k):
        calls.append("cli")
        raise kling_cli.KlingCliError("boom")

    monkeypatch.setattr(kling_cli, "image_to_video", fail)
    monkeypatch.setattr(kling_video, "_api_image_to_video", lambda *a, **k: pytest.fail("REST must not be tried"))
    with pytest.raises(kling_video.KlingJobError):
        kling_video.image_to_video_once(Path("f.png"), Path("o.mp4"), cfg=cfg, prompt="p", duration=4, backend="cli")
    assert calls == ["cli"]
