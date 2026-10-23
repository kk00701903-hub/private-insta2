from datetime import datetime, timedelta, timezone

import pytest

import insta_auto.pipeline as pipeline
from insta_auto.pipeline import cancel_schedule, load_job, schedule_job
from insta_auto.scheduler import MAX_LATE, Scheduler, due_jobs

from .conftest import make_job

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _no_token(*_a, **_k):
    return {"should_refresh": False, "can_refresh": False}


@pytest.fixture
def posted(monkeypatch):
    """Mock Graph API posting — no network."""
    calls = []

    def fake_carousel(cfg, cards, *, caption, hashtags):
        calls.append(("carousel", caption, tuple(hashtags)))
        return f"media_{len(calls)}"

    def fake_reel(cfg, path, *, caption, hashtags):
        calls.append(("reel", caption, tuple(hashtags)))
        return f"reel_{len(calls)}"

    monkeypatch.setattr(pipeline, "publish_carousel_sync", fake_carousel)
    monkeypatch.setattr(pipeline, "publish_reel_sync", fake_reel)
    return calls


def test_schedule_requires_future_time(cfg):
    make_job(cfg, "j1")
    with pytest.raises(ValueError):
        schedule_job("j1", NOW - timedelta(minutes=1), cfg=cfg, now=NOW)
    job = schedule_job("j1", NOW + timedelta(hours=1), cfg=cfg, now=NOW)
    assert job.status == "scheduled"
    assert job.scheduled_at == "2026-10-23T13:00:00+00:00"
    assert job.approved_at == "2026-10-23T12:00:00+00:00"


def test_rejected_job_cannot_be_scheduled(cfg):
    make_job(cfg, "j1", status="rejected")
    with pytest.raises(ValueError):
        schedule_job("j1", NOW + timedelta(hours=1), cfg=cfg, now=NOW)


def test_due_jobs_timing(cfg):
    make_job(cfg, "a")
    make_job(cfg, "b")
    schedule_job("a", NOW + timedelta(minutes=5), cfg=cfg, now=NOW)
    schedule_job("b", NOW + timedelta(minutes=10), cfg=cfg, now=NOW)
    assert [j.job_id for j in due_jobs(cfg, NOW)] == []
    assert [j.job_id for j in due_jobs(cfg, NOW + timedelta(minutes=5))] == ["a"]
    assert [j.job_id for j in due_jobs(cfg, NOW + timedelta(minutes=11))] == ["a", "b"]


def test_tick_posts_only_due_jobs(cfg, posted):
    make_job(cfg, "a")
    make_job(cfg, "b")
    make_job(cfg, "unapproved")  # ready but never approved → never posted
    schedule_job("a", NOW + timedelta(minutes=5), cfg=cfg, now=NOW)
    schedule_job("b", NOW + timedelta(hours=2), cfg=cfg, now=NOW)
    s = Scheduler(status_fn=_no_token)

    assert s.tick(NOW + timedelta(minutes=4))["posted"] == []
    res = s.tick(NOW + timedelta(minutes=5, seconds=1))
    assert res["posted"] == ["a"]
    assert load_job("a", cfg=cfg).status == "published"
    assert load_job("a", cfg=cfg).media_ids == ["media_1"]
    assert load_job("b", cfg=cfg).status == "scheduled"
    assert load_job("unapproved", cfg=cfg).status == "ready"
    # second tick does not repost
    assert s.tick(NOW + timedelta(minutes=6))["posted"] == []
    assert len(posted) == 1


def test_schedules_survive_restart(cfg, posted):
    make_job(cfg, "a")
    schedule_job("a", NOW + timedelta(minutes=5), cfg=cfg, now=NOW)
    first = Scheduler(status_fn=_no_token)
    first.tick(NOW)  # nothing due; "process exits"
    restarted = Scheduler(status_fn=_no_token)  # fresh instance, state only on disk
    assert restarted.tick(NOW + timedelta(minutes=6))["posted"] == ["a"]


def test_too_late_requires_reapproval(cfg, posted):
    make_job(cfg, "a")
    schedule_job("a", NOW + timedelta(minutes=5), cfg=cfg, now=NOW)
    s = Scheduler(status_fn=_no_token)
    res = s.tick(NOW + timedelta(minutes=5) + MAX_LATE + timedelta(minutes=1))
    assert res["failed"] == ["a"] and posted == []
    job = load_job("a", cfg=cfg)
    assert job.status == "failed" and "다시 승인" in job.error


def test_cancelled_schedule_is_not_posted(cfg, posted):
    make_job(cfg, "a")
    schedule_job("a", NOW + timedelta(minutes=5), cfg=cfg, now=NOW)
    cancel_schedule("a", cfg=cfg)
    s = Scheduler(status_fn=_no_token)
    assert s.tick(NOW + timedelta(hours=1))["posted"] == []
    assert load_job("a", cfg=cfg).status == "ready"


def test_failed_post_marks_failed_and_keeps_partial(cfg, monkeypatch):
    make_job(cfg, "a", fmt="both", reel_path="a/reel.mp4")
    (cfg.output_dir / "a" / "reel.mp4").write_bytes(b"mp4")
    monkeypatch.setattr(pipeline, "publish_carousel_sync", lambda *a, **k: "car_1")

    def boom(*a, **k):
        raise RuntimeError("Graph API error: reel failed")

    monkeypatch.setattr(pipeline, "publish_reel_sync", boom)
    schedule_job("a", NOW + timedelta(minutes=1), cfg=cfg, now=NOW)
    res = Scheduler(status_fn=_no_token).tick(NOW + timedelta(minutes=2))
    assert res["failed"] == ["a"]
    job = load_job("a", cfg=cfg)
    assert job.status == "failed" and job.posted_parts == {"carousel": "car_1"}

    # retry (after re-approval) only posts the missing reel
    carousel_calls = []
    monkeypatch.setattr(pipeline, "publish_carousel_sync", lambda *a, **k: carousel_calls.append(1))
    monkeypatch.setattr(pipeline, "publish_reel_sync", lambda *a, **k: "reel_9")
    job = pipeline.publish_job("a", cfg=cfg)
    assert job.status == "published" and job.media_ids == ["car_1", "reel_9"]
    assert carousel_calls == []


def test_token_refresh_triggered_under_10_days(cfg):
    calls = []
    status = {"should_refresh": True, "can_refresh": True, "days_left": 8.5}
    s = Scheduler(status_fn=lambda *a, **k: status, refresher=lambda c: calls.append(1) or {"ok": True})
    assert s.tick(NOW)["token"] == "refreshed"
    # rate-limited: no second attempt within the retry interval
    assert s.tick(NOW + timedelta(minutes=1))["token"] is None
    assert calls == [1]
    status["should_refresh"] = False
    assert s.tick(NOW + timedelta(days=1))["token"] is None


def test_token_refresh_failure_logged_without_secret(cfg, tmp_path):
    from dataclasses import replace

    secret = "EAAG-secret"

    def refresher(c):
        raise RuntimeError(f"bad {secret}")

    s = Scheduler(
        cfg_loader=lambda: replace(cfg, ig_access_token=secret),
        status_fn=lambda *a, **k: {"should_refresh": True, "can_refresh": True, "days_left": 3},
        refresher=refresher,
    )
    assert s.tick(NOW)["token"] == "failed"
    log = (tmp_path / "작업기록.md").read_text(encoding="utf-8")
    assert "토큰 자동 갱신 실패" in log and secret not in log
