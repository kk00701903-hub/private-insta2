from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECRET_ENV = [
    "COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY", "COUPANG_SUB_ID", "FB_APP_SECRET",
    "META_WEBHOOK_VERIFY_TOKEN", "IG_USER_ID", "IG_ACCESS_TOKEN", "FB_PAGE_ID",
    "FB_PAGE_ACCESS_TOKEN",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """실제 키/데이터를 절대 쓰지 않도록 격리."""
    monkeypatch.setenv("MONETIZE_DATA_DIR", str(tmp_path / "monetize"))
    for k in SECRET_ENV:
        monkeypatch.setenv(k, "")
    # 실제 네트워크 호출 차단: 테스트는 반드시 MockTransport 를 넘겨야 한다
    import httpx

    real_client = httpx.Client

    def guarded(*args, **kwargs):
        if kwargs.get("transport") is None:
            raise AssertionError("real network call attempted in tests")
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", guarded)
    yield


@pytest.fixture
def cfg(tmp_path):
    from insta_auto.config import AppConfig

    c = AppConfig(output_dir=tmp_path / "output")
    c.output_dir.mkdir(parents=True)
    c.default_hashtags = ["추천", "정보"]
    return c


@pytest.fixture
def make_job(cfg):
    def _make(job_id="20261023_120000", status="ready", caption="본문 캡션", hashtags=None, media_ids=None,
              topic="가을 캠핑: 감성 랜턴 추천"):
        d = cfg.output_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "job_id": job_id, "status": status, "topic": topic, "caption": caption,
            "hashtags": hashtags if hashtags is not None else ["캠핑", "추천", "랜턴"],
            "fmt": "photo", "out_dir": str(d), "card_paths": [], "media_ids": media_ids or [],
        }
        (d / "job.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        (d / "analysis.json").write_text(
            json.dumps({"topic": topic, "hashtags": meta["hashtags"], "slides": [{"title": "캠핑 랜턴"}]},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return job_id

    return _make
