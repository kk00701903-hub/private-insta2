"""제휴 링크가 붙은 게시물의 캡션 조립.

규칙
- 공정위(FTC) 고지 문구는 캡션의 **맨 앞**에 둔다. 본문에 같은 문구가 있으면 지우고 맨 앞으로 옮긴다.
- 고지 문구와 CTA 는 잘리지 않는다. 길면 해시태그(뒤에서부터) → 본문 순으로 줄인다.
- 글자 수는 공백·줄바꿈 포함으로 센다 (Instagram 캡션 한도 2200자).
"""
from __future__ import annotations

import unicodedata

CAPTION_LIMIT = 2200
MAX_HASHTAGS = 30
ELLIPSIS = "…"


class CaptionError(ValueError):
    pass


def char_count(text: str) -> int:
    """공백·줄바꿈 포함 글자 수 (NFC 기준)."""
    return len(unicodedata.normalize("NFC", text or ""))


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def _tag_line(tags: list[str]) -> str:
    return " ".join(f"#{t.strip().lstrip('#')}" for t in tags if t and t.strip().lstrip("#"))


def strip_disclosure(body: str, disclosure: str) -> str:
    body = _norm(body)
    d = _norm(disclosure).strip()
    if d:
        body = body.replace(d, "")
    # 고지 제거 후 남는 빈 줄 정리
    lines = [ln.rstrip() for ln in body.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def compose_affiliate_caption(
    body: str,
    hashtags: list[str],
    *,
    disclosure: str,
    cta: str = "",
    limit: int = CAPTION_LIMIT,
) -> str:
    disclosure = _norm(disclosure).strip()
    if not disclosure:
        raise CaptionError("제휴 링크가 있으면 고지 문구를 비울 수 없습니다.")
    cta = _norm(cta).strip()
    body = strip_disclosure(body, disclosure)
    if cta:
        body = body.replace(cta, "").strip()
    tags = [t for t in (hashtags or []) if t and t.strip().lstrip("#")][:MAX_HASHTAGS]

    def build(b: str, t: list[str]) -> str:
        parts = [disclosure]
        if b:
            parts.append(b)
        if cta:
            parts.append(cta)
        tl = _tag_line(t)
        if tl:
            parts.append(tl)
        return "\n\n".join(parts)

    text = build(body, tags)
    if char_count(text) <= limit:
        return text

    fixed = build("", [])
    if char_count(fixed) > limit:
        raise CaptionError(
            f"고지 문구+CTA만으로 {char_count(fixed)}자입니다 (한도 {limit}자). 문구를 줄여 주세요."
        )

    # 1) 해시태그를 뒤에서부터 제거
    while tags and char_count(build(body, tags)) > limit:
        tags = tags[:-1]
    text = build(body, tags)
    if char_count(text) <= limit:
        return text

    # 2) 본문 줄이기 (말줄임표 포함)
    overflow = char_count(text) - limit
    keep = max(0, char_count(body) - overflow - len(ELLIPSIS))
    body = (body[:keep].rstrip() + ELLIPSIS) if keep > 0 else ""
    text = build(body, tags)
    while char_count(text) > limit and body:
        body = body[:-2].rstrip() + ELLIPSIS if len(body) > 2 else ""
        text = build(body, tags)
    return text
