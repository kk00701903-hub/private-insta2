"""AI reel storyboard: user request (+ photos) -> 3–6 shots, 7–30 s, caption + hashtags.

Uses the same Claude client/config as analyze.py (CLAUDE_API_KEY, CLAUDE_MODEL). Without a key
(or with offline=True) a deterministic plan is produced so the rest of the pipeline still works.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from insta_auto.analyze import AnalysisResult, AnalyzeError, SlidePlan, _extract_json, _image_block
from insta_auto.config import AppConfig

MIN_SHOTS, MAX_SHOTS = 3, 6
MIN_TOTAL, MAX_TOTAL = 7.0, 30.0
MIN_SHOT, MAX_SHOT = 1.5, 8.0
CAMERA_MOVES = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static")


class Shot(BaseModel):
    # photo: user photo · generate: Kling text-to-image · color: gradient backdrop (no photo, free mode)
    source: Literal["photo", "generate", "color"] = "photo"
    photo_index: int | None = Field(default=None, description="0-based input photo index when source=photo")
    image_prompt: str | None = Field(default=None, description="English prompt when source=generate")
    look: str = Field(default="natural", description="PhotoCraft look preset name")
    overlay_text: str = Field(default="", max_length=40)
    sub_text: str = Field(default="", max_length=60)
    text_position: Literal["top", "center", "bottom"] = "center"
    motion_prompt: str = Field(default="", description="English image-to-video motion prompt")
    camera: Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static"] = "zoom_in"
    duration: float = Field(default=4.0, ge=MIN_SHOT, le=MAX_SHOT)

    @model_validator(mode="after")
    def _source_fields(self) -> "Shot":
        if self.source == "photo" and self.photo_index is None:
            raise ValueError("photo shot needs photo_index")
        if self.source == "generate" and not (self.image_prompt or "").strip():
            raise ValueError("generate shot needs image_prompt")
        return self


class Storyboard(BaseModel):
    topic: str
    mood: str = ""
    is_real_place: bool = False
    shots: list[Shot] = Field(min_length=MIN_SHOTS, max_length=MAX_SHOTS)
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    music_mood: str = ""

    @field_validator("hashtags")
    @classmethod
    def _strip_hash(cls, tags: list[str]) -> list[str]:
        out: list[str] = []
        for t in tags:
            t = str(t).strip().lstrip("#").replace(" ", "")
            if t and t not in out:
                out.append(t)
        return out

    @model_validator(mode="after")
    def _total(self) -> "Storyboard":
        total = self.total_duration
        if not (MIN_TOTAL <= total <= MAX_TOTAL):
            raise ValueError(f"total duration {total:.1f}s outside {MIN_TOTAL}-{MAX_TOTAL}s")
        if self.is_real_place and any(s.source == "generate" for s in self.shots):
            raise ValueError("real places must use real photos only")
        return self

    @property
    def total_duration(self) -> float:
        return round(sum(s.duration for s in self.shots), 2)

    def to_analysis(self) -> AnalysisResult:
        """Same shape as the card pipeline's analysis.json (job.json 'analysis')."""
        n = len(self.shots)
        slides = [
            SlidePlan(
                role="cover" if i == 0 else ("ending" if i == n - 1 else "body"),
                title=s.overlay_text,
                body=s.sub_text,
                photo_index=s.photo_index if s.source == "photo" else None,
                need_illustration=s.source == "generate",
                illustration_prompt=s.image_prompt,
            )
            for i, s in enumerate(self.shots)
        ]
        return AnalysisResult(
            topic=self.topic,
            mood=self.mood,
            slides=slides,
            caption=self.caption,
            hashtags=list(self.hashtags),
            is_real_place=self.is_real_place,
        )


# --------------------------------------------------------------------------------------------
# Normalisation: make a model answer valid instead of failing the whole job
# --------------------------------------------------------------------------------------------


def _fit_durations(durations: list[float], target: float | None) -> list[float]:
    ds = [min(MAX_SHOT, max(MIN_SHOT, float(d or 4.0))) for d in durations]
    total = sum(ds)
    goal = target if target else total
    goal = min(MAX_TOTAL, max(MIN_TOTAL, goal, MIN_SHOT * len(ds)))
    goal = min(goal, MAX_SHOT * len(ds))
    if abs(total - goal) > 0.01:
        k = goal / total
        ds = [min(MAX_SHOT, max(MIN_SHOT, d * k)) for d in ds]
        # rounding / clamping leftovers go to the longest shots
        diff = goal - sum(ds)
        for i in sorted(range(len(ds)), key=lambda j: -ds[j]):
            if abs(diff) < 0.01:
                break
            nd = min(MAX_SHOT, max(MIN_SHOT, ds[i] + diff))
            diff -= nd - ds[i]
            ds[i] = nd
    out = [round(d, 2) for d in ds]
    # keep the 2-decimal total exact (e.g. 7 s / 3 shots -> 2.33 + 2.33 + 2.34)
    residual = round(round(goal, 2) - sum(out), 2)
    if residual:
        i = max(range(len(out)), key=lambda j: out[j]) if residual < 0 else min(range(len(out)), key=lambda j: out[j])
        out[i] = round(min(MAX_SHOT, max(MIN_SHOT, out[i] + residual)), 2)
    return out


def normalize_storyboard(
    raw: dict[str, Any],
    *,
    n_photos: int,
    looks: list[str],
    allow_generate: bool,
    target_seconds: float | None = None,
    default_hashtags: list[str] | None = None,
) -> Storyboard:
    data = dict(raw or {})
    is_real = bool(data.get("is_real_place"))
    shots_raw = [s for s in (data.get("shots") or []) if isinstance(s, dict)]
    if not shots_raw:
        raise AnalyzeError("storyboard has no shots")
    shots_raw = shots_raw[:MAX_SHOTS]
    while len(shots_raw) < MIN_SHOTS:
        shots_raw.append(dict(shots_raw[len(shots_raw) % len(shots_raw)]))

    default_look = "natural" if "natural" in looks else (looks[0] if looks else "natural")
    shots: list[dict[str, Any]] = []
    for i, s in enumerate(shots_raw):
        s = dict(s)
        src = s.get("source") if s.get("source") in {"photo", "generate", "color"} else "photo"
        if src == "color" and n_photos > 0:
            src = "photo"
        pidx = s.get("photo_index")
        if src == "generate" and (is_real or not allow_generate or not (s.get("image_prompt") or "").strip()):
            src = "photo"
        if src == "photo":
            if n_photos <= 0:
                if allow_generate and not is_real:
                    src = "generate"
                    s["image_prompt"] = (s.get("image_prompt") or s.get("motion_prompt") or data.get("topic") or "abstract soft background").strip()
                else:
                    src = "color"  # no photos and no generation: gradient backdrop (free mode)
            elif not isinstance(pidx, int) or not (0 <= pidx < n_photos):
                pidx = i % n_photos
        s["source"] = src
        s["photo_index"] = pidx if src == "photo" else None
        if s.get("look") not in looks:
            s["look"] = default_look
        if s.get("camera") not in CAMERA_MOVES:
            s["camera"] = CAMERA_MOVES[i % 4]
        if s.get("text_position") not in {"top", "center", "bottom"}:
            s["text_position"] = "top" if i == 0 else "bottom"
        s["overlay_text"] = str(s.get("overlay_text") or "")[:40]
        s["sub_text"] = str(s.get("sub_text") or "")[:60]
        s["motion_prompt"] = str(s.get("motion_prompt") or "slow cinematic camera push-in")
        shots.append(s)

    for s, d in zip(shots, _fit_durations([s.get("duration") or 4.0 for s in shots], target_seconds)):
        s["duration"] = d

    tags = list(data.get("hashtags") or []) or list(default_hashtags or [])
    sb_data = {
        "topic": str(data.get("topic") or "AI 릴스")[:80],
        "mood": str(data.get("mood") or ""),
        "is_real_place": is_real,
        "shots": shots,
        "caption": str(data.get("caption") or data.get("topic") or ""),
        "hashtags": tags[:15],
        "music_mood": str(data.get("music_mood") or ""),
    }
    return Storyboard.model_validate(sb_data)


# --------------------------------------------------------------------------------------------
# Claude / offline planners
# --------------------------------------------------------------------------------------------

SYSTEM = (
    "You are a short-form video director for Korean Instagram Reels. "
    "Return ONLY valid JSON matching the schema. Overlay text is Korean, short and punchy "
    "(count characters including spaces). image_prompt and motion_prompt are English. "
    "For real restaurants or venues set is_real_place=true and use only the user's photos."
)


def _user_prompt(request: str, photos: list[Path], looks: dict[str, str], allow_generate: bool,
                 target_seconds: float | None, cfg: AppConfig) -> str:
    photo_list = "\n".join(f"- photo[{i}]: {p.name}" for i, p in enumerate(photos)) or "(no photos)"
    look_list = "\n".join(f"- {k}: {v}" for k, v in looks.items())
    length = f"about {target_seconds:.0f} seconds" if target_seconds else "7 to 30 seconds"
    gen_rule = (
        "You may use source=generate with an English image_prompt when no user photo fits."
        if allow_generate
        else "Do NOT use source=generate; every shot must use a user photo (source=photo)."
    )
    return f"""Plan an Instagram Reel for this request.

Request:
{request}

Account: @{cfg.account}
Input photos:
{photo_list}

PhotoCraft looks (pick one per shot by name):
{look_list}

JSON schema:
{{
  "topic": "short Korean topic",
  "mood": "string",
  "is_real_place": false,
  "music_mood": "e.g. calm acoustic, upbeat pop",
  "shots": [
    {{
      "source": "photo|generate",
      "photo_index": 0,
      "image_prompt": null,
      "look": "natural",
      "overlay_text": "Korean hook, <= 18 chars",
      "sub_text": "optional Korean line, <= 30 chars",
      "text_position": "top|center|bottom",
      "motion_prompt": "English camera/subject motion for image-to-video",
      "camera": "zoom_in|zoom_out|pan_left|pan_right|pan_up|pan_down|static",
      "duration": 4
    }}
  ],
  "caption": "Instagram caption in Korean, no hashtags here",
  "hashtags": ["tag1", "tag2"]
}}

Rules:
- {MIN_SHOTS} to {MAX_SHOTS} shots, total length {length} (each shot {MIN_SHOT}-{MAX_SHOT}s).
- First shot is a strong hook; last shot is a call to action (follow / save).
- {gen_rule}
- Prefer real photos; reorder them for a fresh angle.
- 5–12 hashtags without #. Defaults you may mix in: {cfg.default_hashtags}.
"""


def _call_claude(cfg: AppConfig, system: str, content: list[dict[str, Any]]) -> str:
    """Single Claude Messages call (mocked in tests)."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AnalyzeError("anthropic package is not installed") from exc
    client = anthropic.Anthropic(api_key=cfg.claude_api_key)
    message = client.messages.create(
        model=cfg.claude_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "\n".join(b.text for b in message.content if getattr(b, "type", None) == "text")


def plan_reel(
    request: str,
    photos: list[Path],
    *,
    cfg: AppConfig,
    looks: dict[str, str],
    allow_generate: bool,
    target_seconds: float | None = None,
) -> Storyboard:
    if not cfg.claude_api_key:
        raise AnalyzeError("CLAUDE_API_KEY is not set in .env")
    content: list[dict[str, Any]] = [_image_block(p) for p in photos[:8]]
    content.append({"type": "text", "text": _user_prompt(request, photos, looks, allow_generate, target_seconds, cfg)})
    text = _call_claude(cfg, SYSTEM, content)
    raw = _extract_json(text)
    return normalize_storyboard(
        raw,
        n_photos=len(photos),
        looks=list(looks),
        allow_generate=allow_generate,
        target_seconds=target_seconds,
        default_hashtags=cfg.default_hashtags,
    )


def plan_offline(
    request: str,
    photos: list[Path],
    *,
    cfg: AppConfig,
    looks: list[str],
    allow_generate: bool,
    target_seconds: float | None = None,
) -> Storyboard:
    """Deterministic storyboard without an API key (dry runs, tests)."""
    lines = [ln.strip() for ln in (request or "").splitlines() if ln.strip()]
    topic = (lines[0] if lines else "오늘의 추천")[:40]
    n = max(MIN_SHOTS, min(MAX_SHOTS, len(photos) or MIN_SHOTS))
    look = "natural" if "natural" in looks else (looks[0] if looks else "natural")
    shots: list[dict[str, Any]] = []
    for i in range(n):
        if i == 0:
            text, sub, pos = topic[:18], "", "top"
        elif i == n - 1:
            text, sub, pos = "저장하고 다시 보기", f"@{cfg.account}", "bottom"
        else:
            body = lines[i] if i < len(lines) else f"포인트 {i}"
            text, sub, pos = body[:18], "", "bottom"
        shots.append(
            {
                "source": "photo",
                "photo_index": (i % len(photos)) if photos else None,
                "look": look,
                "overlay_text": text,
                "sub_text": sub,
                "text_position": pos,
                "motion_prompt": "slow cinematic camera push-in, gentle natural motion",
                "camera": CAMERA_MOVES[i % 4],
                "duration": 4.0,
            }
        )
    return normalize_storyboard(
        {
            "topic": topic,
            "mood": "",
            "shots": shots,
            "caption": (request or topic).strip(),
            "hashtags": list(cfg.default_hashtags) or ["추천", "정보"],
        },
        n_photos=len(photos),
        looks=looks,
        allow_generate=allow_generate,
        target_seconds=target_seconds,
        default_hashtags=cfg.default_hashtags,
    )
