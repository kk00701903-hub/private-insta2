from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from insta_auto.config import AppConfig


class SlidePlan(BaseModel):
    role: str = Field(description="cover | body | ending")
    title: str = ""
    body: str = ""
    photo_index: int | None = Field(
        default=None,
        description="0-based index into input photos, or null if illustration",
    )
    need_illustration: bool = False
    illustration_prompt: str | None = None


class AnalysisResult(BaseModel):
    topic: str
    mood: str = ""
    slides: list[SlidePlan]
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    is_real_place: bool = Field(
        default=False,
        description="True for restaurants/venues — use real photos only",
    )


class AnalyzeError(RuntimeError):
    pass


def _media_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        return mime
    return "image/jpeg"


def _image_block(path: Path) -> dict[str, Any]:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _media_type(path),
            "data": data,
        },
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise AnalyzeError(f"No JSON object in model response: {text[:400]}")
    return json.loads(text[start : end + 1])


def analyze(
    photos: list[Path],
    comment: str,
    *,
    cfg: AppConfig,
    card_count: int,
    mood: str = "",
    fmt: str = "photo",
) -> AnalysisResult:
    if not cfg.claude_api_key:
        raise AnalyzeError("CLAUDE_API_KEY is not set in .env")
    if not photos:
        raise AnalyzeError("At least one photo is required")

    try:
        import anthropic
    except ImportError as exc:
        raise AnalyzeError("anthropic package is not installed") from exc

    photo_list = "\n".join(f"- photo[{i}]: {p.name}" for i, p in enumerate(photos))
    system = (
        "You are an Instagram content planner for Korean card news / Reels. "
        "Return ONLY valid JSON matching the schema. "
        "Do not copy the source order blindly — reorder and enrich for a fresh angle. "
        "Count characters including spaces for Korean copy. "
        "For real restaurants or venues set is_real_place=true and never request AI illustration."
    )
    user_text = f"""Analyze the photos and comment, then plan {card_count} Instagram cards.

Comment:
{comment}

Mood / direction: {mood or "(none)"}
Format: {fmt}
Account: @{cfg.account}

Input photos:
{photo_list}

JSON schema:
{{
  "topic": "string",
  "mood": "string",
  "is_real_place": false,
  "slides": [
    {{
      "role": "cover|body|ending",
      "title": "short Korean title",
      "body": "1-3 short Korean lines",
      "photo_index": 0,
      "need_illustration": false,
      "illustration_prompt": null
    }}
  ],
  "caption": "Instagram caption in Korean, no hashtags here",
  "hashtags": ["tag1", "tag2"]
}}

Rules:
- Exactly {card_count} slides. First is cover, last is ending, middle are body.
- photo_index must be a valid index or null when need_illustration is true.
- Prefer user photos. Only set need_illustration when a diagram/illustration clearly helps.
- Include 5–12 hashtags without # prefix. Mix with defaults if helpful: {cfg.default_hashtags}.
"""

    content: list[dict[str, Any]] = [_image_block(p) for p in photos[:8]]
    content.append({"type": "text", "text": user_text})

    client = anthropic.Anthropic(api_key=cfg.claude_api_key)
    message = client.messages.create(
        model=cfg.claude_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    raw = _extract_json("\n".join(parts))
    result = AnalysisResult.model_validate(raw)

    # Clamp slide count and photo indices
    if len(result.slides) > card_count:
        result.slides = result.slides[:card_count]
    while len(result.slides) < card_count:
        result.slides.append(
            SlidePlan(role="body", title=result.topic, body="", photo_index=0)
        )
    if result.slides:
        result.slides[0].role = "cover"
        result.slides[-1].role = "ending"
    for slide in result.slides:
        if result.is_real_place:
            slide.need_illustration = False
            slide.illustration_prompt = None
        if slide.photo_index is not None:
            if slide.photo_index < 0 or slide.photo_index >= len(photos):
                slide.photo_index = 0
    if not result.hashtags and cfg.default_hashtags:
        result.hashtags = list(cfg.default_hashtags)
    return result


def analyze_offline(
    photos: list[Path],
    comment: str,
    *,
    cfg: AppConfig,
    card_count: int,
    mood: str = "",
) -> AnalysisResult:
    """Deterministic fallback when no API key (for dry-run / smoke tests)."""
    topic = (comment.strip().split("\n")[0] or "오늘의 추천")[:40]
    slides: list[SlidePlan] = []
    for i in range(card_count):
        if i == 0:
            role = "cover"
            title = topic
            body = mood or "원본과 다른 느낌으로 정리했어요"
        elif i == card_count - 1:
            role = "ending"
            title = "더 알아보기"
            body = f"@{cfg.account}"
        else:
            role = "body"
            title = f"포인트 {i}"
            body = comment.strip()[:80] or "핵심만 짧게"
        slides.append(
            SlidePlan(
                role=role,
                title=title,
                body=body,
                photo_index=min(i, len(photos) - 1) if photos else 0,
            )
        )
    tags = list(cfg.default_hashtags) or ["추천", "정보"]
    return AnalysisResult(
        topic=topic,
        mood=mood,
        slides=slides,
        caption=comment.strip() or topic,
        hashtags=tags,
        is_real_place=False,
    )
