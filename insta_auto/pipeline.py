from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from insta_auto.analyze import AnalysisResult, analyze, analyze_offline
from insta_auto.caption import check_caption, normalize_hashtags
from insta_auto.cards import render_cards
from insta_auto.config import AppConfig, load_config
from insta_auto.illustrate import generate_illustrations_sync
from insta_auto.post_instagram import publish_carousel_sync, publish_reel_sync
from insta_auto.video import encode_reel
from insta_auto.worklog import append_worklog

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 상태: ready(제작됨·승인 대기) | scheduled(예약 승인) | published(게시됨)
#       | failed(게시 실패 — 재승인 필요) | rejected(거절)
STATUS_ALIASES = {"created": "ready", "posted": "published", "error": "failed"}
EDITABLE_STATUSES = {"ready", "failed"}
PUBLISHABLE_STATUSES = {"ready", "failed", "scheduled"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_when(value: str | datetime) -> datetime:
    """ISO string/datetime → aware UTC datetime. Naive values are local time."""
    dt = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    )
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


@dataclass
class JobResult:
    job_id: str
    status: str  # ready | scheduled | published | failed | rejected
    topic: str
    caption: str
    hashtags: list[str]
    fmt: str
    out_dir: str
    card_paths: list[str] = field(default_factory=list)
    reel_path: str | None = None
    analysis_path: str | None = None
    media_ids: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    notes: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    approved_at: str | None = None
    scheduled_at: str | None = None  # UTC ISO; set only by explicit approval
    posted_at: str | None = None
    # 다시 만들기용 입력 (output_dir 기준 상대경로 + 옵션)
    inputs: dict = field(default_factory=dict)
    regenerated_from: str | None = None
    # 부분 성공 기록 (carousel/reel) — 재시도 때 중복 게시 방지
    posted_parts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["caption_check"] = check_caption(self.caption, self.hashtags)
        return d


def collect_photos(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"Photos path not found: {path}")
    files = sorted(
        p for p in path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not files:
        raise FileNotFoundError(f"No images in {path}")
    return files


def _rel_media(cfg: AppConfig, path: Path) -> str:
    """Return path relative to output_dir for /media/ serving."""
    try:
        return path.resolve().relative_to(cfg.output_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def _store_inputs(
    cfg: AppConfig,
    out_dir: Path,
    photos: list[Path],
    music: Path | None,
) -> tuple[list[str], str | None]:
    """Copy inputs into the job folder so the job can be regenerated later."""
    in_dir = out_dir / "inputs"
    in_dir.mkdir(exist_ok=True)
    saved: list[str] = []
    for i, src in enumerate(photos):
        src = Path(src)
        dest = in_dir / f"photo_{i:02d}{src.suffix.lower() or '.jpg'}"
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        saved.append(_rel_media(cfg, dest))
    saved_music: str | None = None
    if music and Path(music).exists():
        mdest = in_dir / f"music{Path(music).suffix.lower()}"
        if Path(music).resolve() != mdest.resolve():
            shutil.copy2(music, mdest)
        saved_music = _rel_media(cfg, mdest)
    return saved, saved_music


def create_job(
    photos: list[Path],
    comment: str,
    *,
    cfg: AppConfig | None = None,
    fmt: str = "photo",
    count: int | None = None,
    seconds: float | None = None,
    mood: str = "",
    music: Path | None = None,
    offline: bool = False,
    job_id: str | None = None,
) -> JobResult:
    cfg = cfg or load_config()
    count = count or cfg.default_card_count
    seconds = seconds if seconds is not None else cfg.default_seconds_per_slide
    if not job_id:
        base = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_id, n = base, 1
        while (cfg.output_dir / job_id).exists():
            n += 1
            job_id = f"{base}_{n}"
    out_dir = cfg.output_dir / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_photos, saved_music = _store_inputs(cfg, out_dir, photos, music)

    tools: list[str] = []
    use_offline = offline or not cfg.claude_api_key
    if use_offline:
        analysis = analyze_offline(
            photos, comment, cfg=cfg, card_count=count, mood=mood
        )
        tools.append("analyze_offline")
    else:
        analysis = analyze(
            photos, comment, cfg=cfg, card_count=count, mood=mood, fmt=fmt
        )
        tools.append("claude_vision")

    analysis_path = out_dir / "analysis.json"
    analysis_path.write_text(
        analysis.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    illustrated: dict[int, Path] = {}
    try:
        illustrated = generate_illustrations_sync(analysis, out_dir / "illus", cfg=cfg)
        if illustrated:
            tools.append("kling")
    except Exception:
        pass

    card_paths: list[Path] = []
    reel_frames: list[Path] = []
    if fmt in {"photo", "both"}:
        card_paths = render_cards(
            analysis, photos, out_dir / "cards", cfg=cfg, illustrated=illustrated
        )
        tools.append("pillow_cards")
    if fmt in {"reel", "both"}:
        reel_frames = render_cards(
            analysis,
            photos,
            out_dir / "reel_frames",
            cfg=cfg,
            illustrated=illustrated,
            for_reel=True,
        )
        tools.append("pillow_reel_frames")

    reel_path: Path | None = None
    if fmt in {"reel", "both"} and reel_frames:
        reel_path = encode_reel(
            reel_frames, out_dir / "reel.mp4", seconds=seconds, music=music
        )
        tools.append("ffmpeg")

    result = JobResult(
        job_id=job_id,
        status="ready",
        topic=analysis.topic,
        caption=analysis.caption,
        hashtags=list(analysis.hashtags),
        fmt=fmt,
        out_dir=str(out_dir),
        card_paths=[_rel_media(cfg, p) for p in card_paths],
        reel_path=_rel_media(cfg, reel_path) if reel_path else None,
        analysis_path=_rel_media(cfg, analysis_path),
        tools=tools,
        notes="미리보기 대기",
        created_at=_iso(_utcnow()),
        inputs={
            "comment": comment,
            "fmt": fmt,
            "count": count,
            "seconds": seconds,
            "mood": mood,
            "offline": bool(offline),
            "photos": saved_photos,
            "music": saved_music,
        },
    )
    _save_meta(out_dir, result, analysis)
    return result


def _save_meta(out_dir: Path, result: JobResult, analysis: AnalysisResult | None = None) -> None:
    meta = out_dir / "job.json"
    result.updated_at = _iso(_utcnow())
    payload = asdict(result)
    if analysis is not None:
        payload["analysis"] = json.loads(analysis.model_dump_json())
    elif meta.exists():
        # keep the original analysis when only status/caption changes
        try:
            old = json.loads(meta.read_text(encoding="utf-8"))
            if "analysis" in old:
                payload["analysis"] = old["analysis"]
        except (OSError, ValueError):
            pass
    tmp = meta.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta)


def load_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult | None:
    cfg = cfg or load_config()
    meta = cfg.output_dir / job_id / "job.json"
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known = {f.name for f in fields(JobResult)}
    kw = {k: v for k, v in data.items() if k in known}
    kw.setdefault("job_id", job_id)
    kw.setdefault("status", "ready")
    kw.setdefault("topic", "")
    kw.setdefault("caption", "")
    kw.setdefault("fmt", "photo")
    kw.setdefault("out_dir", str(cfg.output_dir / job_id))
    kw["status"] = STATUS_ALIASES.get(kw["status"], kw["status"])
    for key in ("hashtags", "card_paths", "media_ids", "tools"):
        kw[key] = list(kw.get(key) or [])
    for key in ("inputs", "posted_parts"):
        kw[key] = dict(kw.get(key) or {})
    return JobResult(**kw)


def save_job(job: JobResult) -> JobResult:
    _save_meta(Path(job.out_dir), job)
    return job


def list_jobs(
    *,
    cfg: AppConfig | None = None,
    limit: int | None = 40,
    status: str | list[str] | None = None,
    query: str = "",
) -> list[JobResult]:
    cfg = cfg or load_config()
    if not cfg.output_dir.exists():
        return []
    wanted: set[str] | None = None
    if status:
        raw = status if isinstance(status, list) else str(status).split(",")
        wanted = {STATUS_ALIASES.get(s.strip(), s.strip()) for s in raw if s.strip()}
    q = (query or "").strip().lower()
    jobs: list[JobResult] = []
    for d in sorted(cfg.output_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        job = load_job(d.name, cfg=cfg)
        if not job:
            continue
        if wanted and job.status not in wanted:
            continue
        if q and q not in f"{job.job_id} {job.topic} {job.caption}".lower():
            continue
        jobs.append(job)
        if limit and len(jobs) >= limit:
            break
    return jobs


def result_files(job: JobResult, *, cfg: AppConfig | None = None) -> list[Path]:
    cfg = cfg or load_config()
    files = [cfg.output_dir / p for p in job.card_paths]
    if job.reel_path:
        files.append(cfg.output_dir / job.reel_path)
    return [f for f in files if f.exists()]


def _log(job: JobResult, cfg: AppConfig, notes: str, media_ids: list[str] | None = None) -> None:
    append_worklog(
        job_id=job.job_id,
        topic=job.topic,
        tools=job.tools,
        result_files=result_files(job, cfg=cfg),
        media_ids=media_ids or [],
        notes=notes,
    )


def _require(job_id: str, cfg: AppConfig) -> JobResult:
    job = load_job(job_id, cfg=cfg)
    if not job:
        raise FileNotFoundError(f"Job not found: {job_id}")
    return job


def update_caption(
    job_id: str,
    caption: str,
    hashtags: list[str] | str | None,
    *,
    cfg: AppConfig | None = None,
) -> JobResult:
    """Save an edited caption/hashtags (only before approval)."""
    cfg = cfg or load_config()
    job = _require(job_id, cfg)
    if job.status not in EDITABLE_STATUSES:
        raise ValueError(
            "예약된 작업은 예약을 취소한 뒤 수정하세요."
            if job.status == "scheduled"
            else f"이 상태({job.status})에서는 캡션을 수정할 수 없습니다."
        )
    job.caption = (caption or "").strip()
    job.hashtags = normalize_hashtags(hashtags)
    return save_job(job)


def schedule_job(
    job_id: str,
    when: str | datetime,
    *,
    cfg: AppConfig | None = None,
    now: datetime | None = None,
) -> JobResult:
    """Approve a job for posting at `when`. 예약 = 승인."""
    cfg = cfg or load_config()
    job = _require(job_id, cfg)
    if job.status not in EDITABLE_STATUSES:
        raise ValueError(f"이 상태({job.status})에서는 예약할 수 없습니다.")
    at = parse_when(when)
    now = now or _utcnow()
    if at <= now:
        raise ValueError("예약 시각은 현재보다 이후여야 합니다.")
    check = check_caption(job.caption, job.hashtags)
    if not check["ok"]:
        raise ValueError(" ".join(check["warnings"]))
    job.status = "scheduled"
    job.scheduled_at = _iso(at)
    job.approved_at = _iso(now)
    job.error = None
    job.notes = f"예약 승인 — {at.astimezone().strftime('%Y-%m-%d %H:%M')} 게시 예정"
    save_job(job)
    _log(job, cfg, job.notes)
    return job


def cancel_schedule(job_id: str, *, cfg: AppConfig | None = None) -> JobResult:
    cfg = cfg or load_config()
    job = _require(job_id, cfg)
    if job.status != "scheduled":
        raise ValueError("예약된 작업이 아닙니다.")
    job.status = "ready"
    job.scheduled_at = None
    job.approved_at = None
    job.notes = "예약 취소 — 승인 대기"
    save_job(job)
    _log(job, cfg, job.notes)
    return job


def mark_failed(job_id: str, message: str, *, cfg: AppConfig | None = None) -> JobResult:
    """Posting failed → status failed. 재시도는 다시 승인해야 한다."""
    cfg = cfg or load_config()
    job = _require(job_id, cfg)
    job.status = "failed"
    job.error = message
    job.scheduled_at = None
    job.approved_at = None
    job.notes = "게시 실패 — 확인 후 다시 승인하세요"
    save_job(job)
    _log(job, cfg, f"{job.notes}: {message}", list(job.posted_parts.values()))
    return job


def regenerate_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult:
    """Re-run the pipeline with the same stored inputs → new job (승인 대기)."""
    cfg = cfg or load_config()
    job = _require(job_id, cfg)
    inp = job.inputs or {}
    photos = [cfg.output_dir / p for p in inp.get("photos") or []]
    photos = [p for p in photos if p.exists()]
    if not photos:
        raise ValueError("입력 사진이 저장되지 않은 이전 작업이라 다시 만들 수 없습니다.")
    music = cfg.output_dir / inp["music"] if inp.get("music") else None
    new = create_job(
        photos,
        inp.get("comment") or job.topic,
        cfg=cfg,
        fmt=inp.get("fmt") or job.fmt,
        count=inp.get("count"),
        seconds=inp.get("seconds"),
        mood=inp.get("mood") or "",
        music=music if music and music.exists() else None,
        offline=bool(inp.get("offline")) or not cfg.claude_api_key,
    )
    new.regenerated_from = job.job_id
    new.notes = f"다시 만들기 ({job.job_id}) — 미리보기 대기"
    return save_job(new)


def publish_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult:
    """Post to Instagram. Call only after explicit approval
    (승인 버튼, 또는 사용자가 승인한 예약)."""
    cfg = cfg or load_config()
    job = load_job(job_id, cfg=cfg)
    if not job:
        raise FileNotFoundError(f"Job not found: {job_id}")
    if job.status == "published":
        return job
    if job.status not in PUBLISHABLE_STATUSES:
        raise ValueError(f"이 상태({job.status})에서는 게시할 수 없습니다.")
    check = check_caption(job.caption, job.hashtags)
    if not check["ok"]:
        raise ValueError(" ".join(check["warnings"]))

    out = Path(job.out_dir)
    try:
        if job.fmt in {"photo", "both"} and job.card_paths and "carousel" not in job.posted_parts:
            cards = [cfg.output_dir / p for p in job.card_paths]
            job.posted_parts["carousel"] = publish_carousel_sync(
                cfg, cards, caption=job.caption, hashtags=job.hashtags
            )
            _save_meta(out, job)
        if job.fmt in {"reel", "both"} and job.reel_path and "reel" not in job.posted_parts:
            job.posted_parts["reel"] = publish_reel_sync(
                cfg,
                cfg.output_dir / job.reel_path,
                caption=job.caption,
                hashtags=job.hashtags,
            )
            _save_meta(out, job)
    except Exception as exc:
        mark_failed(job_id, str(exc)[:500], cfg=cfg)
        raise
    media_ids = [job.posted_parts[k] for k in ("carousel", "reel") if k in job.posted_parts]

    job.status = "published"
    job.media_ids = media_ids
    job.error = None
    job.scheduled_at = None
    job.posted_at = _iso(_utcnow())
    job.approved_at = job.approved_at or job.posted_at
    job.notes = "게시 완료"
    job.tools = list(dict.fromkeys([*job.tools, "graph_api"]))
    _save_meta(out, job)
    append_worklog(
        job_id=job.job_id,
        topic=job.topic,
        tools=job.tools,
        result_files=[*(cfg.output_dir / p for p in job.card_paths)]
        + ([cfg.output_dir / job.reel_path] if job.reel_path else []),
        media_ids=media_ids,
        notes=job.notes,
    )
    return job


def reject_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult:
    cfg = cfg or load_config()
    job = load_job(job_id, cfg=cfg)
    if not job:
        raise FileNotFoundError(f"Job not found: {job_id}")
    job.status = "rejected"
    job.scheduled_at = None
    job.notes = "승인 거부 — 로컬 결과만 보관"
    _save_meta(Path(job.out_dir), job)
    append_worklog(
        job_id=job.job_id,
        topic=job.topic,
        tools=job.tools,
        result_files=[*(cfg.output_dir / p for p in job.card_paths)]
        + ([cfg.output_dir / job.reel_path] if job.reel_path else []),
        media_ids=[],
        notes=job.notes,
    )
    return job
