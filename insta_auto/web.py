from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from insta_auto.aireels_api import router as aireels_router
from insta_auto.config import ROOT, load_config
from insta_auto.illustrate import kling_ready
from insta_auto.pipeline import (
    IMAGE_EXTS,
    create_job,
    list_jobs,
    load_job,
    publish_job,
    reject_job,
)

_jobs_lock = threading.Lock()
_running: dict[str, str] = {}  # job_id -> status message


def create_app() -> FastAPI:
    cfg = load_config()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="insta2 Studio", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(aireels_router)  # AI 릴스 (/api/aireels/*)

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
            "running": dict(_running),
        }

    @app.get("/api/kling/status")
    def kling_status():
        return kling_ready(load_config())

    @app.get("/api/jobs")
    def jobs(limit: int = 40):
        return {"jobs": [j.to_dict() for j in list_jobs(limit=limit)]}

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

    @app.post("/api/jobs/{job_id}/approve")
    def jobs_approve(job_id: str):
        c = load_config()
        if not (c.ig_user_id and c.ig_access_token):
            raise HTTPException(400, "IG_USER_ID / IG_ACCESS_TOKEN 이 .env에 필요합니다.")
        if not (
            c.imgbb_api_key
            or c.public_base_url
            or (c.cloudinary_cloud_name and c.cloudinary_api_key)
        ):
            # reel-only may not need image host
            job = load_job(job_id, cfg=c)
            if job and job.fmt in {"photo", "both"}:
                raise HTTPException(
                    400,
                    "이미지 호스트가 필요합니다 (IMGBB_API_KEY 또는 PUBLIC_BASE_URL).",
                )
        try:
            with _jobs_lock:
                _running[job_id] = "업로드 중"
            return publish_job(job_id, cfg=c).to_dict()
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        finally:
            _running.pop(job_id, None)

    @app.post("/api/jobs/{job_id}/reject")
    def jobs_reject(job_id: str):
        try:
            return reject_job(job_id).to_dict()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

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
