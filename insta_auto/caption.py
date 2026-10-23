"""Caption / hashtag helpers shared by the API and the posting code.

글자 수는 공백 포함으로 센다 (설계서 7장). Python ``len`` 은 코드포인트 단위라
한글·이모지 모두 1글자로 계산된다. PWA 쪽도 ``[...text].length`` 로 같은 방식을 쓴다.
"""

from __future__ import annotations

import re

CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30

_TAG_RE = re.compile(r"#([^\s#]+)")


def count_chars(text: str | None) -> int:
    """Characters including spaces and line breaks (code points)."""
    return len(text or "")


def normalize_hashtags(raw: list[str] | str | None) -> list[str]:
    """Accept a list or a 'a, #b c' string → ['a', 'b', 'c'] (dedup, order kept)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[\s,]+", str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in re.split(r"[\s,#]+", str(item)):
            tag = part.strip()
            if not tag:
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tag)
    return out


def full_caption(caption: str | None, hashtags: list[str] | None) -> str:
    """The exact text sent to Instagram (caption + blank line + hashtag line)."""
    tag_line = " ".join(f"#{t.lstrip('#')}" for t in (hashtags or []) if t)
    parts = [p for p in ((caption or "").strip(), tag_line) if p]
    return "\n\n".join(parts)


def hashtag_count(caption: str | None, hashtags: list[str] | None) -> int:
    """Unique hashtags in the final post (list + any #tag written in the body)."""
    tags = {t.lower() for t in normalize_hashtags(hashtags)}
    tags.update(m.lower() for m in _TAG_RE.findall(caption or ""))
    return len(tags)


def check_caption(caption: str | None, hashtags: list[str] | None) -> dict:
    tags = normalize_hashtags(hashtags)
    text = full_caption(caption, tags)
    chars = count_chars(text)
    n_tags = hashtag_count(caption, tags)
    warnings: list[str] = []
    if chars > CAPTION_LIMIT:
        warnings.append(
            f"캡션이 {chars:,}자로 인스타 제한 {CAPTION_LIMIT:,}자를 넘습니다 (공백·해시태그 포함)."
        )
    if n_tags > HASHTAG_LIMIT:
        warnings.append(f"해시태그가 {n_tags}개로 인스타 제한 {HASHTAG_LIMIT}개를 넘습니다.")
    return {
        "chars": chars,
        "caption_chars": count_chars((caption or "").strip()),
        "hashtags": n_tags,
        "caption_limit": CAPTION_LIMIT,
        "hashtag_limit": HASHTAG_LIMIT,
        "ok": not warnings,
        "warnings": warnings,
    }
