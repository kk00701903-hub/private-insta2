import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import insta_auto.pipeline as pipeline
import insta_auto.web as web
from insta_auto.pipeline import load_job

from .conftest import make_job


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "1789")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "EAAG-test-token")
    monkeypatch.setenv("IMGBB_API_KEY", "imgbb")
    monkeypatch.setenv("IG_TOKEN_OBTAINED_AT", (datetime.now(timezone.utc) - timedelta(days=55)).isoformat())
    monkeypatch.setattr(web, "kling_ready", lambda c: {"ready": False})
    monkeypatch.setattr(pipeline, "publish_carousel_sync", lambda *a, **k: "media_1")
    return TestClient(web.create_app(start_scheduler=False))


def test_state_has_token_expiry_but_not_token(client):
    s = client.get("/api/state").json()
    assert s["token"]["warn"] is True
    assert 4 < s["token"]["days_left"] < 6
    assert "EAAG-test-token" not in str(s)
    assert s["scheduled_count"] == 0


def test_caption_edit_then_approve_posts_edited_caption(client, cfg, monkeypatch):
    make_job(cfg, "j1")
    seen = {}

    def fake(c, cards, *, caption, hashtags):
        seen.update(caption=caption, hashtags=hashtags)
        return "media_7"

    monkeypatch.setattr(pipeline, "publish_carousel_sync", fake)
    r = client.post("/api/jobs/j1/caption", json={"caption": "새 캡션", "hashtags": "#a b"})
    assert r.status_code == 200
    assert r.json()["caption_check"]["chars"] == len("새 캡션\n\n#a #b")
    r = client.post("/api/jobs/j1/approve", json={"caption": "최종 캡션"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert seen == {"caption": "최종 캡션", "hashtags": ["a", "b"]}


def test_approve_rejects_over_limit_caption(client, cfg):
    make_job(cfg, "j1")
    r = client.post("/api/jobs/j1/approve", json={"caption": "x" * 2300})
    assert r.status_code == 400
    assert load_job("j1", cfg=cfg).status == "ready"


def test_schedule_cancel_and_post_now(client, cfg):
    make_job(cfg, "j1")
    when = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    r = client.post("/api/jobs/j1/approve", json={"scheduled_at": when})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "scheduled"
    assert [j["job_id"] for j in client.get("/api/schedule").json()["jobs"]] == ["j1"]
    # editing a scheduled job is blocked
    assert client.post("/api/jobs/j1/caption", json={"caption": "x"}).status_code == 400
    assert client.post("/api/jobs/j1/schedule/cancel").json()["status"] == "ready"
    # post-now only for approved (scheduled) jobs
    assert client.post("/api/jobs/j1/post-now").status_code == 400
    client.post("/api/jobs/j1/approve", json={"scheduled_at": when})
    r = client.post("/api/jobs/j1/post-now")
    assert r.status_code == 200 and r.json()["status"] == "published"


def test_history_filter_and_download(client, cfg):
    make_job(cfg, "20261001_000001", status="published")
    make_job(cfg, "20261001_000002", status="failed", error="boom")
    make_job(cfg, "20261001_000003")
    jobs = client.get("/api/jobs", params={"status": "failed"}).json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["20261001_000002"]
    jobs = client.get("/api/jobs", params={"status": "created,posted"}).json()["jobs"]
    assert {j["job_id"] for j in jobs} == {"20261001_000001", "20261001_000003"}
    r = client.get("/api/jobs/20261001_000001/download")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "cards/card_01.png" in names and "caption.txt" in names


def test_retry_failed_requires_approval(client, cfg):
    make_job(cfg, "j1", status="failed", error="boom")
    assert client.post("/api/jobs/j1/post-now").status_code == 400
    r = client.post("/api/jobs/j1/approve")
    assert r.status_code == 200 and r.json()["status"] == "published"


def test_regenerate_old_job_without_inputs(client, cfg):
    make_job(cfg, "j1")
    r = client.post("/api/jobs/j1/regenerate")
    assert r.status_code == 400


def test_create_then_regenerate_offline(client, cfg, monkeypatch):
    from insta_auto.config import ROOT

    monkeypatch.setattr(pipeline, "generate_illustrations_sync", lambda *a, **k: {})
    photos = [ROOT / "samples" / "photo_1.jpg", ROOT / "samples" / "photo_2.jpg"]
    job = pipeline.create_job(photos, "가을 카페", cfg=cfg, fmt="photo", count=2, offline=True)
    assert len(job.inputs["photos"]) == 2
    r = client.post(f"/api/jobs/{job.job_id}/regenerate")
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["job_id"] != job.job_id
    assert new["regenerated_from"] == job.job_id and new["status"] == "ready"
    assert new["inputs"]["comment"] == "가을 카페"
