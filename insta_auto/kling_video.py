"""Paid Kling jobs for AI reels: image-to-video clips and (optional) generated stills.

Rules (credits are real money):
- The backend (official `kling` CLI, else REST keys) is chosen BEFORE submitting. A failed job is
  never re-submitted on another backend or retried automatically; the error goes back to the UI
  and the user decides whether to retry.
- Callers must have the user's explicit confirmation of the job count first (aireels.py does).

REST fallback follows the Kling open-platform API (POST /v1/videos/image2video, GET
/v1/videos/image2video/{task_id}, result in data.task_result.videos[].url). That path is not
exercised against the live service in this repo's tests.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import os
from pathlib import Path
from typing import Any

from insta_auto import illustrate, kling_cli
from insta_auto.config import AppConfig


class KlingJobError(RuntimeError):
    pass


def choose_backend(cfg: AppConfig) -> str:
    """'cli' | 'api'; raises if neither is usable. Decided once, before any paid submission."""
    backend = (cfg.kling_backend or "auto").lower()
    if backend in {"auto", "cli"} and kling_cli.cli_available(cfg):
        status = kling_cli.login_status(cfg)
        if status.get("logged_in"):
            return "cli"
        if backend == "cli":
            raise KlingJobError("Kling CLI 로그인이 필요합니다: `npm run kling:login`")
    if backend in {"auto", "api"} and cfg.kling_api_key:
        return "api"
    raise KlingJobError("Kling 사용 불가: `npm run kling:login` 또는 .env 의 KLING_API_KEY 를 설정하세요.")


def clip_seconds(duration: float) -> int:
    """Kling clip length to request for a shot (assembly trims to the shot duration)."""
    allowed = os.getenv("KLING_VIDEO_DURATIONS", "5,10")
    options = sorted(int(x) for x in allowed.split(",") if x.strip().isdigit()) or [5, 10]
    for opt in options:
        if opt >= duration:
            return opt
    return options[-1]


# ------------------------------------------------------------------------------- REST fallback


async def _api_image_to_video(image: Path, dest: Path, *, cfg: AppConfig, prompt: str, duration: int) -> Path:
    body: dict[str, Any] = {
        "model_name": os.getenv("KLING_VIDEO_MODEL") or "kling-v1-6",
        "image": base64.b64encode(Path(image).read_bytes()).decode("ascii"),
        "prompt": prompt or "",
        "duration": str(duration),
        "mode": "std",
    }
    created = await illustrate._request(cfg, "POST", "/v1/videos/image2video", json=body)
    task_id = illustrate._task_id(created)
    for _ in range(int(os.getenv("KLING_VIDEO_POLL_SECONDS") or 600) // 5):
        payload = await illustrate._request(cfg, "GET", f"/v1/videos/image2video/{task_id}")
        data = payload.get("data") or payload
        status = str(data.get("task_status") or data.get("status") or "").upper() if isinstance(data, dict) else ""
        if status in {"SUCCEED", "SUCCESS", "SUCCEEDED", "FINISHED"}:
            videos = ((data.get("task_result") or {}).get("videos") or []) if isinstance(data, dict) else []
            urls = [v.get("url") for v in videos if isinstance(v, dict) and v.get("url")]
            if not urls:
                raise KlingJobError(f"Kling API returned no video URL (task {task_id})")
            return await illustrate._download(urls[0], dest)
        if status in {"FAILED", "ERROR"}:
            raise KlingJobError(f"Kling video task failed: {data.get('task_status_msg') if isinstance(data, dict) else payload}")
        await asyncio.sleep(5)
    raise KlingJobError(f"Kling video task {task_id} still running — check it later in Kling; not resubmitted")


# ------------------------------------------------------------------------------- public API


def image_to_video_once(
    image: Path,
    dest: Path,
    *,
    cfg: AppConfig,
    prompt: str,
    duration: float,
    backend: str,
) -> Path:
    secs = clip_seconds(duration)
    try:
        if backend == "cli":
            return kling_cli.image_to_video(image, dest, cfg=cfg, prompt=prompt, duration=secs)
        return asyncio.run(_api_image_to_video(image, dest, cfg=cfg, prompt=prompt, duration=secs))
    except KlingJobError:
        raise
    except Exception as exc:
        raise KlingJobError(str(exc)) from exc


def text_to_image_once(prompt: str, dest: Path, *, cfg: AppConfig, backend: str) -> Path:
    """One 9:16 still for a source=generate shot. No CLI->REST fallback after a failure."""
    vcfg = dataclasses.replace(cfg, kling_aspect_ratio="9:16")
    full = f"{prompt}. Vertical 9:16, clean composition, no text, no watermark, no distorted hands or faces."
    try:
        if backend == "cli":
            return kling_cli.text_to_image(full, dest, cfg=vcfg, aspect_ratio="9:16")
        return asyncio.run(illustrate._generate_via_api(full, dest, vcfg))
    except Exception as exc:
        raise KlingJobError(str(exc)) from exc
