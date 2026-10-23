from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from insta_auto.caption import check_caption, full_caption, normalize_hashtags
from insta_auto.config import ROOT, AppConfig, load_config
from insta_auto.ig_token import token_status
from insta_auto.illustrate import kling_ready
from insta_auto.pipeline import (
    EDITABLE_STATUSES,
    IMAGE_EXTS,
    JobResult,
    cancel_schedule,
    create_job,
    list_jobs,
    load_job,
    publish_job,
    regenerate_job,
    reject_job,
    result_files,
    schedule_job,
    update_caption,
)
from insta_auto.scheduler import RUNNING, Scheduler, claim, release, scheduled_jobs


class CaptionBody(BaseModel):
    caption: str = ""
    hashtags: list[str] | str | None = None


class ApproveBody(BaseModel):
    # 비우면 즉시 게시, 값이 있으면 그 시각에 예약 게시 (예약 = 승인)
    scheduled_at: str | None = None
    caption: str | None = None
    hashtags: list[str] | str | None = None


def _check_publish_ready(c: AppConfig, job: JobResult | None) -> None:
    if not (c.ig_user_id and c.ig_access_token):
        raise HTTPException(400, "IG_USER_ID / IG_ACCESS_TOKEN 이 .env에 필요합니다.")
    if not (
        c.imgbb_api_key
        or c.public_base_url
        or (c.cloudinary_cloud_name and c.cloudinary_api_key)
    ):
        # reel-only may not need image host
        if job and job.fmt in {"photo", "both"}:
            raise HTTPException(
                400,
                "이미지 호스트가 필요합니다 (IMGBB_API_KEY 또는 PUBLIC_BASE_URL).",
            )


def _publish_now(job_id: str, c: AppConfig) -> dict:
    if not claim(job_id, "업로드 중"):
        raise HTTPException(409, "이미 업로드 중인 작업입니다.")
    try:
        return publish_job(job_id, cfg=c).to_dict()
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        release(job_id)


def create_app(*, start_scheduler: bool | None = None) -> FastAPI:
    cfg = load_config()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if start_scheduler is None:
        start_scheduler = os.getenv("INSTA2_SCHEDULER", "1").lower() not in {"0", "false", "off"}
    scheduler = Scheduler()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(title="insta2 Studio", version="0.2.0", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/state")
    def state():
        c = load_config()
        kling = kling_ready(c)
        return {
            "account": c.account,
            "has_claude": bool(c.claude_api_key),
            "has_ig": bool(c.ig_user_id and c.ig_access_token),
            "has_image_host": bool(
                c.imgbb_api_key
                or c.public_base_url
                or (c.cloudinary_cloud_name and c.cloudinary_api_key)
            ),
            "has_kling": bool(kling.get("ready")),
            "kling": kling,
            "default_card_count": c.default_card_count,
            "default_seconds": c.default_seconds_per_slide,
            "running": dict(RUNNING),
            # 만료 정보만 — 토큰 값은 절대 포함하지 않는다
            "token": token_status(c),
            "scheduled_count": len(scheduled_jobs(c)),
            "scheduler": scheduler.info(),
        }

    @app.get("/api/kling/status")
    def kling_status():
        return kling_ready(load_config())

    @app.get("/api/jobs")
    def jobs(limit: int = 40, status: str = "", q: str = ""):
        return {
            "jobs": [
                j.to_dict() for j in list_jobs(limit=limit or None, status=status or None, query=q)
            ]
        }

    @app.get("/api/schedule")
    def schedule_queue():
        return {"jobs": [j.to_dict() for j in scheduled_jobs(load_config())]}

    @app.post("/api/caption/check")
    def caption_check(body: CaptionBody):
        tags = normalize_hashtags(body.hashtags)
        return {**check_caption(body.caption, tags), "text": full_caption(body.caption, tags)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        job = load_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job.to_dict()

    @app.post("/api/jobs/create")
    async def jobs_create(
        comment: str = Form(...),
        fmt: str = Form("photo"),
        count: int = Form(5),
        seconds: float = Form(5),
        mood: str = Form(""),
        offline: str = Form("false"),
        photos: list[UploadFile] = File(...),
        music: UploadFile | None = File(None),
    ):
        if fmt not in {"photo", "reel", "both"}:
            raise HTTPException(400, "fmt must be photo|reel|both")
        if not photos:
            raise HTTPException(400, "photos required")
        offline_flag = str(offline).lower() in {"1", "true", "yes", "on"}

        c = load_config()
        tmp = Path(tempfile.mkdtemp(prefix="insta2_upload_"))
        try:
            photo_paths: list[Path] = []
            for i, up in enumerate(photos):
                suffix = Path(up.filename or f"photo_{i}.jpg").suffix.lower() or ".jpg"
                if suffix not in IMAGE_EXTS:
                    suffix = ".jpg"
                dest = tmp / f"photo_{i:02d}{suffix}"
                dest.write_bytes(await up.read())
                photo_paths.append(dest)

            music_path: Path | None = None
            if music and music.filename:
                music_path = tmp / Path(music.filename).name
                music_path.write_bytes(await music.read())

            job = create_job(
                photo_paths,
                comment,
                cfg=c,
                fmt=fmt,
                count=count,
                seconds=seconds,
                mood=mood,
                music=music_path,
                offline=offline_flag or not c.claude_api_key,
            )
            return job.to_dict()
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @app.post("/api/jobs/{job_id}/caption")
    def jobs_caption(job_id: str, body: CaptionBody):
        try:
            return update_caption(job_id, body.caption, body.hashtags).to_dict()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/approve")
    def jobs_approve(job_id: str, body: ApproveBody | None = Body(None)):
        """승인. scheduled_at 이 있으면 예약 승인, 없으면 즉시 게시.
        caption/hashtags 를 함께 보내면 먼저 저장한 뒤 그 내용으로 게시한다."""
        c = load_config()
        job = load_job(job_id, cfg=c)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.status not in EDITABLE_STATUSES:
            raise HTTPException(400, f"이 상태({job.status})에서는 승인할 수 없습니다.")
        _check_publish_ready(c, job)
        body = body or ApproveBody()
        try:
            if body.caption is not None or body.hashtags is not None:
                job = update_caption(
                    job_id,
                    body.caption if body.caption is not None else job.caption,
                    body.hashtags if body.hashtags is not None else job.hashtags,
                    cfg=c,
                )
            check = check_caption(job.caption, job.hashtags)
            if not check["ok"]:
                raise ValueError(" ".join(check["warnings"]))
            if body.scheduled_at:
                return schedule_job(job_id, body.scheduled_at, cfg=c).to_dict()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _publish_now(job_id, c)

    @app.post("/api/jobs/{job_id}/schedule/cancel")
    def jobs_schedule_cancel(job_id: str):
        try:
            return cancel_schedule(job_id).to_dict()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/post-now")
    def jobs_post_now(job_id: str):
        """예약(=이미 승인된) 작업을 지금 바로 게시."""
        c = load_config()
        job = load_job(job_id, cfg=c)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.status != "scheduled":
            raise HTTPException(400, "예약된 작업만 바로 게시할 수 있습니다. 먼저 승인하세요.")
        _check_publish_ready(c, job)
        return _publish_now(job_id, c)

    @app.post("/api/jobs/{job_id}/reject")
    def jobs_reject(job_id: str):
        try:
            return reject_job(job_id).to_dict()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/regenerate")
    def jobs_regenerate(job_id: str):
        try:
            return regenerate_job(job_id).to_dict()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/download")
    def jobs_download(job_id: str):
        """카드 PNG + reel.mp4 + caption.txt 를 zip 으로."""
        c = load_config()
        job = load_job(job_id, cfg=c)
        if not job:
            raise HTTPException(404, "Job not found")
        files = result_files(job, cfg=c)
        if not files:
            raise HTTPException(404, "결과 파일이 없습니다.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                arc = f.relative_to(Path(job.out_dir)).as_posix() if f.is_relative_to(Path(job.out_dir)) else f.name
                zf.write(f, arc)
            zf.writestr("caption.txt", full_caption(job.caption, job.hashtags))
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{job.job_id}.zip"'},
        )

    @app.get("/media/{rest:path}")
    def media(rest: str):
        c = load_config()
        path = (c.output_dir / rest).resolve()
        if not str(path).startswith(str(c.output_dir.resolve())):
            raise HTTPException(403, "Forbidden")
        if not path.exists() or not path.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(path)

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="static")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "insta_auto.web:create_app",
        factory=True,
        host="127.0.0.1",
        port=8787,
        reload=False,
    )


if __name__ == "__main__":
    main()
