"""AI 릴스 orchestration: free mode end to end, paid-mode confirmation and no auto-retry."""

import shutil

import pytest

from insta_auto import aireels, kling_video, photocraft
from insta_auto.pipeline import load_job
from insta_auto.reel_builder import make_kenburns_clip

from .conftest import SAMPLES


@pytest.fixture(autouse=True)
def _pillow_only(monkeypatch):
    # Deterministic and fast on any machine; the real CLI has its own test.
    monkeypatch.setattr(photocraft, "available", lambda: False)


def test_free_mode_end_to_end(cfg, _no_worklog):
    state = aireels.create_project(
        "가을 카페 추천\n창가 자리", SAMPLES, cfg=cfg, mode="free", target_seconds=7, offline=True, background=False
    )
    assert state["status"] == "ready", state.get("error")
    assert [state["stages"][s]["status"] for s in aireels.STAGES] == ["done"] * 4
    assert state["estimate"]["total_jobs"] == 0
    assert any("Pillow" in w for w in state["warnings"])  # visible PhotoCraft fallback warning
    assert state["reel_info"]["video"]["width"] == 1080

    # The result is a normal job awaiting approval (never auto-posted).
    import dataclasses

    job = load_job(state["job_id"], cfg=cfg)
    assert job is not None and job.status == "ready" and job.fmt == "reel" and not job.media_ids
    assert (cfg.output_dir / job.reel_path).exists()
    assert _no_worklog and "승인 대기" in _no_worklog[0]["notes"]
    assert dataclasses.asdict(job)["caption"]


def test_kling_mode_requires_confirmation_and_never_retries(monkeypatch, cfg):
    calls = []

    def fake_i2v(image, dest, *, cfg, prompt, duration, backend):
        calls.append(image)
        if len(calls) == 2:
            raise kling_video.KlingJobError("simulated Kling failure")
        return make_kenburns_clip(image, dest, duration=duration)

    monkeypatch.setattr(kling_video, "choose_backend", lambda c: "cli")
    monkeypatch.setattr(kling_video, "image_to_video_once", fake_i2v)
    monkeypatch.setattr(kling_video, "text_to_image_once", lambda *a, **k: pytest.fail("no generate shots expected"))

    state = aireels.create_project(
        "카페 소개", SAMPLES, cfg=cfg, mode="kling", target_seconds=7, offline=True, background=False
    )
    assert state["status"] == "awaiting_confirm"
    est = state["estimate"]
    assert (est["image_jobs"], est["video_jobs"], est["total_jobs"]) == (0, 3, 3)
    assert abs(sum(est["clip_seconds"]) - 7) < 0.05
    assert not calls  # nothing paid before confirmation

    with pytest.raises(aireels.AiReelError, match="3건"):
        aireels.start_render(state["job_id"], cfg=cfg, confirm_jobs=0, background=False)
    assert not calls

    state = aireels.start_render(state["job_id"], cfg=cfg, confirm_jobs=3, background=False)
    assert state["status"] == "error"
    assert len(calls) == 2  # stopped at the failure: no automatic retry
    assert "자동 재시도하지 않았습니다" in state["error"]
    assert state["shots"][1]["error"] and state["shots"][0]["clip"]
    assert state["estimate"]["total_jobs"] == 2  # only the remaining clips

    # User retries: must confirm the new count; finished clips are reused.
    state = aireels.start_render(state["job_id"], cfg=cfg, confirm_jobs=2, background=False)
    assert state["status"] == "ready", state.get("error")
    assert len(calls) == 4
    assert state["credits_used"]["video_jobs"] == 4


def test_failed_kling_can_finish_in_free_mode(monkeypatch, cfg):
    monkeypatch.setattr(kling_video, "choose_backend", lambda c: "cli")

    def always_fail(*a, **k):
        raise kling_video.KlingJobError("quota")

    monkeypatch.setattr(kling_video, "image_to_video_once", always_fail)
    state = aireels.create_project("x", SAMPLES[:1], cfg=cfg, mode="kling", target_seconds=7, offline=True, background=False)
    state = aireels.start_render(state["job_id"], cfg=cfg, confirm_jobs=3, background=False)
    assert state["status"] == "error"
    state = aireels.start_render(state["job_id"], cfg=cfg, mode="free", background=False)
    assert state["status"] == "ready", state.get("error")
    assert {s["clip_engine"] for s in state["shots"]} == {"ffmpeg-kenburns"}


def test_api_routes(monkeypatch, cfg):
    from fastapi.testclient import TestClient

    from insta_auto import aireels_api
    from insta_auto.web import create_app

    monkeypatch.setattr(aireels_api, "load_config", lambda: cfg)
    monkeypatch.setattr(aireels, "load_config", lambda: cfg)
    client = TestClient(create_app())

    s = client.get("/api/aireels/status").json()
    assert {"photocraft", "looks", "music", "modes"} <= set(s)
    assert client.get("/api/aireels/nope_404").status_code == 404
    assert client.post("/api/aireels", data={"request": "x", "mode": "bad"}).status_code == 400

    monkeypatch.setattr(aireels, "_start", lambda c, job_id, fn, background, *a: None)  # don't run stages
    with open(SAMPLES[0], "rb") as f:
        r = client.post(
            "/api/aireels",
            data={"request": "가을 카페", "mode": "free", "offline": "true"},
            files=[("photos", ("a.jpg", f, "image/jpeg"))],
        )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert client.get(f"/api/aireels/{job_id}").json()["photos"]
    shutil.rmtree(cfg.output_dir, ignore_errors=True)
