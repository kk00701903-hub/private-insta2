"""AI 릴스: one request -> storyboard -> PhotoCraft stills -> clips -> finished Reel job.

Stages (reported per stage in output/<job_id>/aireel.json):
    plan        Claude storyboard (or offline plan without a key)
    photocraft  grade / 9:16 smart fill / text layers (Pillow fallback with a warning)
    clips       Kling image-to-video (paid, needs explicit confirmation) or free Ken Burns
    assemble    ffmpeg -> reel.mp4 (Reels spec) -> normal job (job.json) awaiting approval

Nothing is ever posted from here: the result is an ordinary job with status "ready" that goes
through the existing approve/reject flow (/api/jobs/{id}/approve).
"""

from __future__ import annotations

import json
import shutil
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from insta_auto import photocraft
from insta_auto.analyze import AnalyzeError
from insta_auto.config import AppConfig, load_config
from insta_auto.pipeline import JobResult
from insta_auto.reel_builder import Segment, assemble_reel, list_music, make_kenburns_clip
from insta_auto.reel_planner import Storyboard, plan_offline, plan_reel
from insta_auto.worklog import append_worklog

STATE_FILE = "aireel.json"
STAGES = ("plan", "photocraft", "clips", "assemble")
MODES = ("free", "kling")

_lock = threading.Lock()
_active: set[str] = set()


class AiReelError(RuntimeError):
    pass


# ------------------------------------------------------------------------------- state helpers


def _out_dir(cfg: AppConfig, job_id: str) -> Path:
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise AiReelError("bad job id")
    return cfg.output_dir / job_id


def _rel(cfg: AppConfig, path: Path | str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(cfg.output_dir.resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _abs(cfg: AppConfig, rel: str | None) -> Path | None:
    return (cfg.output_dir / rel) if rel else None


def load_state(job_id: str, *, cfg: AppConfig | None = None) -> dict[str, Any] | None:
    cfg = cfg or load_config()
    path = _out_dir(cfg, job_id) / STATE_FILE
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    state["running"] = job_id in _active
    return state


def _save(cfg: AppConfig, state: dict[str, Any]) -> None:
    path = _out_dir(cfg, state["job_id"]) / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _stage(cfg: AppConfig, state: dict[str, Any], name: str, status: str, detail: str = "", **extra: Any) -> None:
    st = state["stages"][name]
    st.update({"status": status, "detail": detail, **extra})
    _save(cfg, state)


def list_projects(*, cfg: AppConfig | None = None, limit: int = 20) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    if not cfg.output_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(cfg.output_dir.iterdir(), reverse=True):
        if (d / STATE_FILE).exists():
            s = load_state(d.name, cfg=cfg)
            if s:
                out.append({k: s.get(k) for k in ("job_id", "status", "mode", "created_at", "error")} | {"topic": (s.get("storyboard") or {}).get("topic")})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------------------- estimate


def estimate(state: dict[str, Any]) -> dict[str, Any]:
    """Paid Kling jobs still needed for this project (0 in free mode)."""
    if state.get("mode") != "kling":
        return {"image_jobs": 0, "video_jobs": 0, "total_jobs": 0}
    shots = state.get("shots") or []
    sb = state.get("storyboard") or {}
    image_jobs = sum(
        1
        for s, plan in zip(shots, sb.get("shots") or [])
        if plan.get("source") == "generate" and not s.get("generated")
    )
    video_jobs = sum(1 for s in shots if not s.get("clip"))
    return {
        "image_jobs": image_jobs,
        "video_jobs": video_jobs,
        "total_jobs": image_jobs + video_jobs,
        "clip_seconds": [plan.get("duration") for s, plan in zip(shots, sb.get("shots") or []) if not s.get("clip")],
    }


# ------------------------------------------------------------------------------- create / plan


def create_project(
    request: str,
    photos: list[Path],
    *,
    cfg: AppConfig | None = None,
    mode: str = "free",
    music: Path | None = None,
    target_seconds: float | None = None,
    offline: bool = False,
    job_id: str | None = None,
    background: bool = True,
) -> dict[str, Any]:
    """Save inputs, then plan + prepare stills (free work) in the background."""
    cfg = cfg or load_config()
    if mode not in MODES:
        raise AiReelError(f"mode must be one of {MODES}")
    if not (request or "").strip():
        raise AiReelError("요청 내용을 입력하세요")
    job_id = job_id or datetime.now().strftime("%Y%m%d_%H%M%S") + "_ai"
    out_dir = _out_dir(cfg, job_id)
    inp = out_dir / "input"
    inp.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, p in enumerate(photos):
        dest = inp / f"photo_{i:02d}{Path(p).suffix.lower() or '.jpg'}"
        shutil.copy2(p, dest)
        saved.append(_rel(cfg, dest))
    music_rel = None
    if music:
        dest = inp / f"music{Path(music).suffix.lower()}"
        shutil.copy2(music, dest)
        music_rel = _rel(cfg, dest)

    state: dict[str, Any] = {
        "job_id": job_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "request": request.strip(),
        "mode": mode,
        "photos": saved,
        "music": music_rel,
        "target_seconds": target_seconds,
        "offline": bool(offline or not cfg.claude_api_key),
        "status": "planning",
        "stages": {name: {"status": "pending", "detail": ""} for name in STAGES},
        "storyboard": None,
        "shots": [],
        "estimate": None,
        "warnings": [],
        "error": None,
        "reel_path": None,
        "credits_used": {"image_jobs": 0, "video_jobs": 0},
    }
    _save(cfg, state)
    _start(cfg, job_id, _prepare, background)
    return load_state(job_id, cfg=cfg) or state


def _start(cfg: AppConfig, job_id: str, fn, background: bool, *args: Any) -> None:
    with _lock:
        if job_id in _active:
            raise AiReelError("이미 진행 중입니다")
        _active.add(job_id)

    def runner() -> None:
        try:
            fn(cfg, job_id, *args)
        except Exception as exc:  # surface any unexpected failure in the state file
            state = load_state(job_id, cfg=cfg) or {"job_id": job_id, "stages": {}}
            state["status"] = "error"
            state["error"] = f"{exc}"
            state.setdefault("warnings", []).append(traceback.format_exc(limit=3)[-800:])
            _save(cfg, state)
        finally:
            with _lock:
                _active.discard(job_id)

    if background:
        threading.Thread(target=runner, name=f"aireel-{job_id}", daemon=True).start()
    else:
        runner()


def _photos(cfg: AppConfig, state: dict[str, Any]) -> list[Path]:
    return [cfg.output_dir / p for p in state.get("photos") or []]


def _prepare(cfg: AppConfig, job_id: str) -> None:
    state = load_state(job_id, cfg=cfg)
    assert state is not None
    photos = _photos(cfg, state)
    presets = photocraft.load_presets()
    looks = {name: f"{p.label} — {p.description}" for name, p in presets.items()}
    allow_generate = state["mode"] == "kling"

    _stage(cfg, state, "plan", "running", "스토리보드 작성 중")
    try:
        if state["offline"]:
            sb = plan_offline(state["request"], photos, cfg=cfg, looks=list(looks), allow_generate=allow_generate, target_seconds=state.get("target_seconds"))
            planner = "offline"
        else:
            sb = plan_reel(state["request"], photos, cfg=cfg, looks=looks, allow_generate=allow_generate, target_seconds=state.get("target_seconds"))
            planner = "claude"
    except (AnalyzeError, ValueError) as exc:
        state["status"] = "error"
        state["error"] = f"스토리보드 실패: {exc}"
        _stage(cfg, state, "plan", "error", str(exc))
        return
    state["storyboard"] = json.loads(sb.model_dump_json())
    state["shots"] = [{"index": i} for i in range(len(sb.shots))]
    state["planner"] = planner
    (cfg.output_dir / job_id / "storyboard.json").write_text(sb.model_dump_json(indent=2), encoding="utf-8")
    _stage(cfg, state, "plan", "done", f"{len(sb.shots)}컷 · {sb.total_duration:.1f}초 ({planner})")

    _prepare_stills(cfg, state, sb, only_missing=False, allow_pending_generate=True)
    state["estimate"] = estimate(state)
    if state["estimate"]["total_jobs"] > 0:
        state["status"] = "awaiting_confirm"
        _save(cfg, state)
        return
    _save(cfg, state)
    # Free mode: nothing to pay for — continue straight to clips + assembly.
    _render(cfg, job_id)


def _prepare_stills(cfg: AppConfig, state: dict[str, Any], sb: Storyboard, *, only_missing: bool, allow_pending_generate: bool) -> None:
    photos = _photos(cfg, state)
    out_dir = cfg.output_dir / state["job_id"] / "stills"
    total = len(sb.shots)
    use_pc = photocraft.available()
    if not use_pc:
        msg = "PhotoCraft CLI가 없어 Pillow로 처리합니다 (npm run photocraft:install)."
        if msg not in state["warnings"]:
            state["warnings"].append(msg)
    _stage(cfg, state, "photocraft", "running", "PhotoCraft 보정 중", done=0, total=total)
    for i, shot in enumerate(sb.shots):
        rec = state["shots"][i]
        if only_missing and rec.get("still"):
            continue
        if shot.source == "generate" and not rec.get("generated"):
            if allow_pending_generate:
                # Paid image not generated yet: show a placeholder until the user confirms.
                prep = photocraft.prepare_shot(
                    None, out_dir, i, look=shot.look, title=shot.overlay_text,
                    subtitle=shot.sub_text or "AI 이미지 생성 예정", position=shot.text_position,
                    cfg=cfg, use_photocraft=False,
                )
                rec.update({"preview": _rel(cfg, prep.preview), "placeholder": True})
                _stage(cfg, state, "photocraft", "running", f"{i + 1}/{total}", done=i + 1, total=total)
                continue
            src: Path | None = None
        elif shot.source == "generate":
            src = _abs(cfg, rec.get("generated"))
        elif shot.source == "photo" and shot.photo_index is not None and photos:
            src = photos[shot.photo_index]
        else:
            src = None  # color backdrop
        prep = photocraft.prepare_shot(
            src, out_dir, i, look=shot.look, title=shot.overlay_text, subtitle=shot.sub_text,
            position=shot.text_position, cfg=cfg, use_photocraft=use_pc,
        )
        rec.update(
            {
                "still": _rel(cfg, prep.still),
                "overlay": _rel(cfg, prep.overlay),
                "preview": _rel(cfg, prep.preview),
                "engine": prep.engine,
                "placeholder": False,
            }
        )
        for w in prep.warnings:
            if w not in state["warnings"]:
                state["warnings"].append(w)
        _stage(cfg, state, "photocraft", "running", f"{i + 1}/{total}", done=i + 1, total=total)
    engines = sorted({s.get("engine") for s in state["shots"] if s.get("engine")})
    pending = sum(1 for s in state["shots"] if s.get("placeholder"))
    detail = f"엔진: {', '.join(engines) or '-'}" + (f" · 생성 대기 {pending}컷" if pending else "")
    _stage(cfg, state, "photocraft", "done" if not pending else "partial", detail, done=total, total=total)


# ------------------------------------------------------------------------------- confirm / render


def start_render(
    job_id: str,
    *,
    cfg: AppConfig | None = None,
    confirm_jobs: int = 0,
    mode: str | None = None,
    background: bool = True,
) -> dict[str, Any]:
    """User confirmed (or switched to free mode). confirm_jobs must equal the shown estimate."""
    cfg = cfg or load_config()
    state = load_state(job_id, cfg=cfg)
    if not state:
        raise AiReelError("작업을 찾을 수 없습니다")
    if state.get("running"):
        raise AiReelError("이미 진행 중입니다")
    if state.get("status") in {"ready", "planning"}:
        raise AiReelError(f"지금 상태({state['status']})에서는 시작할 수 없습니다")
    if not state.get("storyboard"):
        raise AiReelError("스토리보드가 없습니다")
    if mode:
        if mode not in MODES:
            raise AiReelError(f"mode must be one of {MODES}")
        state["mode"] = mode
    est = estimate(state)
    if est["total_jobs"] > 0 and int(confirm_jobs) != est["total_jobs"]:
        state["estimate"] = est
        _save(cfg, state)
        raise AiReelError(
            f"Kling 유료 작업 {est['total_jobs']}건(이미지 {est['image_jobs']} · 영상 {est['video_jobs']})을 확인해 주세요."
        )
    state["estimate"] = est
    state["status"] = "rendering"
    state["error"] = None
    _save(cfg, state)
    _start(cfg, job_id, _render, background)
    return load_state(job_id, cfg=cfg) or state


def _render(cfg: AppConfig, job_id: str) -> None:
    state = load_state(job_id, cfg=cfg)
    assert state is not None
    sb = Storyboard.model_validate(state["storyboard"])
    state["status"] = "rendering"
    _save(cfg, state)
    out_dir = cfg.output_dir / job_id

    backend = None
    if state["mode"] == "kling" and estimate(state)["total_jobs"] > 0:
        from insta_auto.kling_video import KlingJobError, choose_backend

        try:
            backend = choose_backend(cfg)
        except KlingJobError as exc:
            state["status"] = "error"
            state["error"] = str(exc)
            _stage(cfg, state, "clips", "error", str(exc))
            return

    # 1) paid stills for source=generate shots (Kling text-to-image), then PhotoCraft them
    if backend:
        from insta_auto.kling_video import KlingJobError, text_to_image_once

        for i, shot in enumerate(sb.shots):
            rec = state["shots"][i]
            if shot.source != "generate" or rec.get("generated"):
                continue
            _stage(cfg, state, "photocraft", "running", f"Kling 이미지 생성 {i + 1}컷")
            try:
                img = text_to_image_once(shot.image_prompt or sb.topic, out_dir / "generated" / f"gen_{i + 1:02d}.png", cfg=cfg, backend=backend)
            except KlingJobError as exc:
                rec["error"] = f"이미지 생성 실패: {exc}"
                state["status"] = "error"
                state["error"] = f"{i + 1}번 컷 Kling 이미지 생성 실패 — 자동 재시도하지 않습니다. 확인 후 다시 시도하세요."
                state["credits_used"]["image_jobs"] += 1
                _stage(cfg, state, "photocraft", "error", rec["error"])
                return
            state["credits_used"]["image_jobs"] += 1
            rec["generated"] = _rel(cfg, img)
            rec["still"] = None
            _save(cfg, state)
    elif state["mode"] == "free":
        # free mode: generate shots without an image become gradient cards
        for i, shot in enumerate(sb.shots):
            rec = state["shots"][i]
            if shot.source == "generate" and not rec.get("generated"):
                state["storyboard"]["shots"][i]["source"] = "color"
                state["storyboard"]["shots"][i]["image_prompt"] = shot.image_prompt
        sb = Storyboard.model_validate(state["storyboard"])
        for rec in state["shots"]:
            if rec.get("placeholder"):
                rec["still"] = None
    _prepare_stills(cfg, state, sb, only_missing=True, allow_pending_generate=False)

    # 2) clips
    total = len(sb.shots)
    _stage(cfg, state, "clips", "running", "클립 생성 중", done=0, total=total)
    fade = 0.5
    for i, shot in enumerate(sb.shots):
        rec = state["shots"][i]
        if rec.get("clip"):
            continue
        still = _abs(cfg, rec.get("still"))
        if not still or not still.exists():
            raise AiReelError(f"{i + 1}번 컷 이미지가 없습니다")
        length = shot.duration + (fade if i < total - 1 else 0.0)
        clip_path = out_dir / "clips" / f"clip_{i + 1:02d}.mp4"
        if backend:
            from insta_auto.kling_video import KlingJobError, image_to_video_once

            _stage(cfg, state, "clips", "running", f"Kling 영상 {i + 1}/{total} (수 분 걸릴 수 있음)", done=i, total=total)
            try:
                image_to_video_once(still, clip_path, cfg=cfg, prompt=shot.motion_prompt, duration=length, backend=backend)
                rec["clip_engine"] = f"kling-{backend}"
            except KlingJobError as exc:
                state["credits_used"]["video_jobs"] += 1
                rec["error"] = f"영상 생성 실패: {exc}"
                state["status"] = "error"
                state["error"] = (
                    f"{i + 1}번 컷 Kling 영상 생성 실패 — 자동 재시도하지 않았습니다. "
                    "다시 시도하거나 '무료 모션으로 마무리'를 선택하세요."
                )
                state["estimate"] = estimate(state)
                _stage(cfg, state, "clips", "error", rec["error"], done=i, total=total)
                return
            state["credits_used"]["video_jobs"] += 1
        else:
            _stage(cfg, state, "clips", "running", f"무료 모션 {i + 1}/{total}", done=i, total=total)
            make_kenburns_clip(still, clip_path, duration=length, camera=shot.camera)
            rec["clip_engine"] = "ffmpeg-kenburns"
        rec["clip"] = _rel(cfg, clip_path)
        rec.pop("error", None)
        _stage(cfg, state, "clips", "running", f"{i + 1}/{total}", done=i + 1, total=total)
    _stage(cfg, state, "clips", "done", ", ".join(sorted({s.get("clip_engine", "") for s in state["shots"]})), done=total, total=total)

    # 3) assemble
    _stage(cfg, state, "assemble", "running", "ffmpeg 합성 중")
    segments = [
        Segment(clip=_abs(cfg, rec["clip"]), duration=shot.duration, overlay=_abs(cfg, rec.get("overlay")))
        for rec, shot in zip(state["shots"], sb.shots)
    ]
    music = _abs(cfg, state.get("music"))
    reel = out_dir / "reel.mp4"
    info = assemble_reel(segments, reel, music=music, keep_clip_audio=False, fade=fade)
    state["reel_path"] = _rel(cfg, reel)
    state["reel_info"] = {k: info.get(k) for k in ("duration", "video", "audio")}
    _stage(cfg, state, "assemble", "done", f"{info.get('duration') or 0:.1f}초 · 1080x1920")

    # 4) hand over to the normal approve/reject flow
    tools = ["aireel_" + state.get("planner", "offline")]
    tools += sorted({f"{s.get('engine')}" for s in state["shots"] if s.get("engine")})
    tools += sorted({s.get("clip_engine") for s in state["shots"] if s.get("clip_engine")})
    tools.append("ffmpeg")
    credits = state["credits_used"]
    credits_text = (
        f"Kling 이미지 {credits['image_jobs']}건 · 영상 {credits['video_jobs']}건"
        if (credits["image_jobs"] or credits["video_jobs"])
        else "-"
    )
    job = JobResult(
        job_id=job_id,
        status="ready",
        topic=sb.topic,
        caption=sb.caption,
        hashtags=list(sb.hashtags),
        fmt="reel",
        out_dir=str(out_dir),
        card_paths=[],
        reel_path=state["reel_path"],
        analysis_path=_rel(cfg, out_dir / "storyboard.json"),
        tools=tools,
        notes="미리보기 대기 (AI 릴스)",
    )
    payload = job.to_dict()
    payload["analysis"] = json.loads(sb.to_analysis().model_dump_json())
    payload["aireel"] = True
    (out_dir / "job.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    state["job"] = job.to_dict()
    state["status"] = "ready"
    _save(cfg, state)
    append_worklog(
        job_id=job_id,
        topic=sb.topic,
        tools=tools,
        result_files=[reel, out_dir / "storyboard.json"],
        media_ids=[],
        notes="AI 릴스 제작 완료 — 승인 대기 (자동 게시 안 함)",
        credits=credits_text,
    )


def music_choices(cfg: AppConfig | None = None) -> list[str]:
    cfg = cfg or load_config()
    return [p.name for p in list_music(cfg.music_dir)]
