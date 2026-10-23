"""Background scheduler that runs inside the API process.

- 예약 게시: 사용자가 승인(예약)한 작업만 정해진 시각에 게시한다. 예약 정보는
  각 작업의 job.json 에 저장되므로 서버를 다시 켜도 그대로 이어진다.
- 토큰 자동 갱신: IG 장기 토큰 만료가 REFRESH_DAYS 미만이면 갱신한다.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from insta_auto import ig_token
from insta_auto.config import AppConfig, load_config
from insta_auto.pipeline import (
    JobResult,
    list_jobs,
    mark_failed,
    parse_when,
    publish_job,
)
from insta_auto.worklog import append_event

log = logging.getLogger("insta_auto.scheduler")

TICK_SECONDS = 30
# 서버가 꺼져 있어 예약 시각을 이만큼 넘기면 자동 게시하지 않고 재승인을 요구한다.
MAX_LATE = timedelta(hours=6)
TOKEN_RETRY_INTERVAL = timedelta(hours=6)

# job_id -> status message (shared with web.py for /api/state "running")
RUNNING: dict[str, str] = {}
_LOCK = threading.Lock()


def claim(job_id: str, message: str) -> bool:
    """Mark a job as in-progress. False if someone else is already posting it."""
    with _LOCK:
        if job_id in RUNNING:
            return False
        RUNNING[job_id] = message
        return True


def release(job_id: str) -> None:
    with _LOCK:
        RUNNING.pop(job_id, None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def scheduled_jobs(cfg: AppConfig) -> list[JobResult]:
    jobs = list_jobs(cfg=cfg, limit=None, status="scheduled")
    return sorted(jobs, key=lambda j: j.scheduled_at or "")


def due_jobs(cfg: AppConfig, now: datetime) -> list[JobResult]:
    out = []
    for job in scheduled_jobs(cfg):
        if not job.scheduled_at:
            continue
        try:
            at = parse_when(job.scheduled_at)
        except ValueError:
            continue
        if at <= now:
            out.append(job)
    return out


class Scheduler:
    def __init__(
        self,
        *,
        interval: float = TICK_SECONDS,
        cfg_loader: Callable[[], AppConfig] | None = None,
        publisher: Callable[..., JobResult] = publish_job,
        refresher: Callable[..., dict] = ig_token.refresh_long_lived_token,
        status_fn: Callable[..., dict] = ig_token.token_status,
    ) -> None:
        self.interval = interval
        self.cfg_loader = cfg_loader
        self.publisher = publisher
        self.refresher = refresher
        self.status_fn = status_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: datetime | None = None
        self.last_token_attempt: datetime | None = None
        self.last_token_result: str | None = None

    # ---- lifecycle -------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="insta2-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # first tick right away: reloads pending schedules after a restart
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the loop die
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)

    # ---- work ------------------------------------------------------
    def tick(self, now: datetime | None = None) -> dict:
        now = now or _utcnow()
        self.last_tick = now
        cfg = (self.cfg_loader or load_config)()
        posted, failed, skipped = [], [], []
        for job in due_jobs(cfg, now):
            at = parse_when(job.scheduled_at)  # type: ignore[arg-type]
            if now - at > MAX_LATE:
                hours = int((now - at).total_seconds() // 3600)
                mark_failed(
                    job.job_id,
                    f"예약 시각을 {hours}시간 넘겨 자동 게시하지 않았습니다. 다시 승인하세요.",
                    cfg=cfg,
                )
                failed.append(job.job_id)
                continue
            if not claim(job.job_id, "예약 게시 중"):
                skipped.append(job.job_id)
                continue
            try:
                self.publisher(job.job_id, cfg=cfg)
                posted.append(job.job_id)
                log.info("scheduled post published: %s", job.job_id)
            except Exception as exc:
                # publish_job already marked the job failed
                failed.append(job.job_id)
                log.warning("scheduled post failed: %s (%s)", job.job_id, type(exc).__name__)
            finally:
                release(job.job_id)
        token = self.maybe_refresh_token(cfg, now)
        return {"posted": posted, "failed": failed, "skipped": skipped, "token": token}

    def maybe_refresh_token(self, cfg: AppConfig, now: datetime) -> str | None:
        status = self.status_fn(cfg, now=now)
        if not (status.get("should_refresh") and status.get("can_refresh")):
            return None
        if self.last_token_attempt and now - self.last_token_attempt < TOKEN_RETRY_INTERVAL:
            return None
        self.last_token_attempt = now
        left = status.get("days_left")
        try:
            result = self.refresher(cfg)
        except Exception as exc:
            msg = ig_token.redact(str(exc), cfg.ig_access_token, cfg.fb_app_secret)
            self.last_token_result = f"실패: {msg}"
            append_event("IG 토큰 자동 갱신 실패", [f"만료까지 약 {left}일", f"사유: {msg}"])
            log.warning("token refresh failed")
            return "failed"
        self.last_token_result = "갱신 완료"
        append_event(
            "IG 토큰 자동 갱신",
            [
                f"만료까지 약 {left}일 남아 자동 갱신",
                f"새 발급일: {result.get('obtained_at')}",
                f"새 유효기간: 약 {result.get('expires_in_days') or '?'}일",
                "토큰 값은 .env에만 저장 (기록하지 않음)",
            ],
        )
        log.info("token refreshed")
        return "refreshed"

    def info(self) -> dict:
        return {
            "running": self.running,
            "interval": self.interval,
            "last_tick": self.last_tick.isoformat(timespec="seconds") if self.last_tick else None,
            "last_token_result": self.last_token_result,
        }
