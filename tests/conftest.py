from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import insta_auto.pipeline as pipeline
import insta_auto.scheduler as scheduler
import insta_auto.web as web
import insta_auto.worklog as worklog
from insta_auto.config import load_config

SECRET_ENV = (
    "IG_USER_ID",
    "IG_ACCESS_TOKEN",
    "IG_TOKEN_OBTAINED_AT",
    "FB_APP_ID",
    "FB_APP_SECRET",
    "IMGBB_API_KEY",
    "PUBLIC_BASE_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLAUDE_API_KEY",
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated config: output/ and 작업기록.md live in tmp, no real secrets."""
    for key in SECRET_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("insta_auto.config.load_dotenv", lambda *a, **k: False)
    base = load_config()
    c = replace(base, output_dir=tmp_path / "output")
    c.output_dir.mkdir()

    def loader(*_a, **_k):
        # re-read env each call (tests set IG_* via monkeypatch)
        fresh = load_config()
        return replace(fresh, output_dir=c.output_dir)

    for mod in (pipeline, scheduler, web):
        monkeypatch.setattr(mod, "load_config", loader)
    monkeypatch.setattr(worklog, "ROOT", tmp_path)
    scheduler.RUNNING.clear()
    return loader()


def make_job(cfg, job_id: str, *, status: str = "ready", fmt: str = "photo", **extra) -> Path:
    out = cfg.output_dir / job_id
    (out / "cards").mkdir(parents=True)
    (out / "cards" / "card_01.png").write_bytes(b"png")
    data = {
        "job_id": job_id,
        "status": status,
        "topic": f"주제 {job_id}",
        "caption": "캡션 본문",
        "hashtags": ["카페", "가을"],
        "fmt": fmt,
        "out_dir": str(out),
        "card_paths": [f"{job_id}/cards/card_01.png"],
        **extra,
    }
    (out / "job.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return out
