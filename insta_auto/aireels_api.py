"""FastAPI routes for AI 릴스 (registered from web.py with app.include_router)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from insta_auto import aireels, photocraft
from insta_auto.config import load_config
from insta_auto.pipeline import IMAGE_EXTS
from insta_auto.reel_builder import MUSIC_EXTS, list_music

router = APIRouter(prefix="/api/aireels", tags=["aireels"])


def _truthy(v: str | None) -> bool:
    return str(v or "").lower() in {"1", "true", "yes", "on"}


@router.get("/status")
def aireels_status():
    c = load_config()
    looks = photocraft.load_presets()
    return {
        "photocraft": photocraft.status(),
        "has_claude": bool(c.claude_api_key),
        "looks": [{"name": k, "label": v.label, "description": v.description} for k, v in looks.items()],
        "music": [p.name for p in list_music(c.music_dir)],
        "modes": list(aireels.MODES),
    }


@router.get("")
def aireels_list(limit: int = 20):
    return {"projects": aireels.list_projects(limit=limit)}


@router.post("")
async def aireels_create(
    request: str = Form(...),
    mode: str = Form("free"),
    seconds: float | None = Form(None),
    offline: str = Form("false"),
    music_name: str = Form(""),
    photos: list[UploadFile] | None = File(None),
    music: UploadFile | None = File(None),
):
    c = load_config()
    if mode not in aireels.MODES:
        raise HTTPException(400, f"mode must be one of {aireels.MODES}")
    tmp = Path(tempfile.mkdtemp(prefix="insta2_aireel_"))
    try:
        photo_paths: list[Path] = []
        for i, up in enumerate(photos or []):
            if not up or not up.filename:
                continue
            suffix = Path(up.filename).suffix.lower() or ".jpg"
            if suffix not in IMAGE_EXTS:
                suffix = ".jpg"
            dest = tmp / f"photo_{i:02d}{suffix}"
            dest.write_bytes(await up.read())
            photo_paths.append(dest)
        music_path: Path | None = None
        if music and music.filename:
            suffix = Path(music.filename).suffix.lower()
            if suffix not in MUSIC_EXTS:
                raise HTTPException(400, "지원하지 않는 음악 형식입니다")
            music_path = tmp / f"music{suffix}"
            music_path.write_bytes(await music.read())
        elif music_name:
            candidates = {p.name: p for p in list_music(c.music_dir)}
            if music_name not in candidates:
                raise HTTPException(400, f"music/ 폴더에 {music_name} 이(가) 없습니다")
            music_path = candidates[music_name]
        target = float(seconds) if seconds else None
        if target is not None and not (7 <= target <= 30):
            raise HTTPException(400, "길이는 7–30초 사이로 입력하세요")
        return aireels.create_project(
            request,
            photo_paths,
            cfg=c,
            mode=mode,
            music=music_path,
            target_seconds=target,
            offline=_truthy(offline) or not c.claude_api_key,
        )
    except aireels.AiReelError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@router.get("/{job_id}")
def aireels_get(job_id: str):
    try:
        state = aireels.load_state(job_id)
    except aireels.AiReelError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not state:
        raise HTTPException(404, "AI 릴스 작업을 찾을 수 없습니다")
    return state


@router.post("/{job_id}/render")
def aireels_render(job_id: str, confirm_jobs: int = Form(0), mode: str = Form("")):
    """Start clips + assembly. Paid Kling jobs require confirm_jobs == shown estimate."""
    try:
        return aireels.start_render(job_id, confirm_jobs=confirm_jobs, mode=mode or None)
    except aireels.AiReelError as exc:
        raise HTTPException(400, str(exc)) from exc
