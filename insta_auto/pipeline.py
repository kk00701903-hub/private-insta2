from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from insta_auto.analyze import AnalysisResult, analyze, analyze_offline
from insta_auto.cards import render_cards
from insta_auto.config import AppConfig, load_config
from insta_auto.illustrate import generate_illustrations_sync
from insta_auto.post_instagram import publish_carousel_sync, publish_reel_sync
from insta_auto.video import encode_reel
from insta_auto.worklog import append_worklog

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass
class JobResult:
    job_id: str
    status: str  # ready | published | rejected | error
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

    def to_dict(self) -> dict:
        return asdict(self)


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
    job_id = job_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = cfg.output_dir / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

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
    )
    _save_meta(out_dir, result, analysis)
    return result


def _save_meta(out_dir: Path, result: JobResult, analysis: AnalysisResult | None = None) -> None:
    payload = result.to_dict()
    if analysis is not None:
        payload["analysis"] = json.loads(analysis.model_dump_json())
    (out_dir / "job.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult | None:
    cfg = cfg or load_config()
    meta = cfg.output_dir / job_id / "job.json"
    if not meta.exists():
        return None
    data = json.loads(meta.read_text(encoding="utf-8"))
    return JobResult(
        job_id=data["job_id"],
        status=data.get("status", "ready"),
        topic=data.get("topic", ""),
        caption=data.get("caption", ""),
        hashtags=list(data.get("hashtags") or []),
        fmt=data.get("fmt", "photo"),
        out_dir=data.get("out_dir", str(cfg.output_dir / job_id)),
        card_paths=list(data.get("card_paths") or []),
        reel_path=data.get("reel_path"),
        analysis_path=data.get("analysis_path"),
        media_ids=list(data.get("media_ids") or []),
        tools=list(data.get("tools") or []),
        error=data.get("error"),
        notes=data.get("notes", ""),
    )


def list_jobs(*, cfg: AppConfig | None = None, limit: int = 40) -> list[JobResult]:
    cfg = cfg or load_config()
    if not cfg.output_dir.exists():
        return []
    jobs: list[JobResult] = []
    for d in sorted(cfg.output_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        job = load_job(d.name, cfg=cfg)
        if job:
            jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def publish_job(job_id: str, *, cfg: AppConfig | None = None) -> JobResult:
    cfg = cfg or load_config()
    job = load_job(job_id, cfg=cfg)
    if not job:
        raise FileNotFoundError(f"Job not found: {job_id}")
    if job.status == "published":
        return job

    media_ids: list[str] = []
    out = Path(job.out_dir)
    if job.fmt in {"photo", "both"} and job.card_paths:
        cards = [cfg.output_dir / p for p in job.card_paths]
        mid = publish_carousel_sync(
            cfg, cards, caption=job.caption, hashtags=job.hashtags
        )
        media_ids.append(mid)
    if job.fmt in {"reel", "both"} and job.reel_path:
        mid = publish_reel_sync(
            cfg,
            cfg.output_dir / job.reel_path,
            caption=job.caption,
            hashtags=job.hashtags,
        )
        media_ids.append(mid)

    job.status = "published"
    job.media_ids = media_ids
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
