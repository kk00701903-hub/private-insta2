"""작업(job)별 수익화 정보 저장 + 파이프라인 단계.

job.json 은 다른 기능이 다시 쓰므로 건드리지 않고, 같은 폴더의 ``monetize.json`` 에 따로 저장한다.

단계 (작업마다 선택):
  1. enable_affiliate(job, True)        — 쿠팡 제휴 사용 켜기
  2. suggest_products(job)              — 분석(주제·해시태그)으로 상품 N개 추천
  3. choose_product / set_manual_link   — 사용자가 PWA에서 하나 선택 (또는 링크 직접 입력)
  4. final_caption_for_job(job)         — 승인·게시 직전, 고지 문구를 맨 앞에 넣은 캡션
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from insta_auto.affiliate.caption import compose_affiliate_caption
from insta_auto.affiliate.coupang import CoupangClient, CoupangError, is_coupang_url
from insta_auto.affiliate.settings import (
    MonetizeSecrets,
    MonetizeSettings,
    load_secrets,
    load_settings,
)
from insta_auto.config import AppConfig, load_config

FILENAME = "monetize.json"
_lock = threading.Lock()


class MonetizeError(RuntimeError):
    pass


def _job_dir(cfg: AppConfig, job_id: str) -> Path:
    if not re.fullmatch(r"[\w\-]+", job_id or ""):
        raise MonetizeError("잘못된 job id")
    d = cfg.output_dir / job_id
    if not (d / "job.json").exists():
        raise FileNotFoundError(f"Job not found: {job_id}")
    return d


def _job_meta(cfg: AppConfig, job_id: str) -> dict:
    return json.loads((_job_dir(cfg, job_id) / "job.json").read_text(encoding="utf-8"))


def default_state() -> dict:
    return {
        "affiliate_enabled": False,
        "search_keyword": "",
        "keyword_candidates": [],
        "suggestions": [],
        "link": None,  # {"url","source","product_id","product_name","chosen_at"}
        "comment_dm_enabled": True,  # 게시물별 스위치 (전체 스위치가 꺼져 있으면 동작 안 함)
        "dm_keywords": [],  # 비어 있으면 전체 기본 키워드 사용
        "updated_at": None,
    }


def load_state(job_id: str, *, cfg: AppConfig | None = None) -> dict:
    cfg = cfg or load_config()
    path = _job_dir(cfg, job_id) / FILENAME
    state = default_state()
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def save_state(job_id: str, state: dict, *, cfg: AppConfig | None = None) -> dict:
    cfg = cfg or load_config()
    path = _job_dir(cfg, job_id) / FILENAME
    state = {**default_state(), **state, "updated_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return state


def _require_editable(cfg: AppConfig, job_id: str) -> None:
    status = _job_meta(cfg, job_id).get("status", "ready")
    if status not in {"ready", "error", "scheduled"}:
        raise MonetizeError(f"이미 {status} 상태인 작업은 제휴 링크를 바꿀 수 없습니다.")


# ---------------------------------------------------------------- keywords
_SPLIT = re.compile(r"[:：\-–—|/,·]")


def keyword_candidates(analysis: dict, *, generic: list[str] | None = None) -> list[str]:
    generic_set = {g.strip().lstrip("#") for g in (generic or [])}
    out: list[str] = []

    def add(s: str) -> None:
        s = re.sub(r"\s+", " ", (s or "").strip().lstrip("#")).strip()
        if 1 < len(s) <= 40 and s not in generic_set and s not in out:
            out.append(s)

    for tag in analysis.get("hashtags") or []:
        add(str(tag))
    topic = str(analysis.get("topic") or "")
    for part in reversed(_SPLIT.split(topic)):
        add(part)
    add(topic)
    for slide in analysis.get("slides") or []:
        if isinstance(slide, dict):
            add(str(slide.get("title") or ""))
    return out[:10]


def _analysis(cfg: AppConfig, job_id: str) -> dict:
    d = _job_dir(cfg, job_id)
    a = d / "analysis.json"
    if a.exists():
        try:
            return json.loads(a.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    meta = _job_meta(cfg, job_id)
    return meta.get("analysis") or {"topic": meta.get("topic", ""), "hashtags": meta.get("hashtags", [])}


# ---------------------------------------------------------------- steps
def enable_affiliate(job_id: str, enabled: bool, *, cfg: AppConfig | None = None) -> dict:
    cfg = cfg or load_config()
    _require_editable(cfg, job_id)
    state = load_state(job_id, cfg=cfg)
    state["affiliate_enabled"] = bool(enabled)
    if enabled and not state.get("keyword_candidates"):
        state["keyword_candidates"] = keyword_candidates(
            _analysis(cfg, job_id), generic=cfg.default_hashtags
        )
        state["search_keyword"] = (state["keyword_candidates"] or [""])[0]
    return save_state(job_id, state, cfg=cfg)


def make_client(secrets: MonetizeSecrets, settings: MonetizeSettings, **kw) -> CoupangClient:
    if not secrets.has_coupang:
        raise MonetizeError(
            "쿠팡 파트너스 API 키가 없습니다. .env 에 COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 를 넣거나 "
            "제휴 링크를 직접 붙여 넣으세요."
        )
    return CoupangClient(
        secrets.coupang_access_key or "",
        secrets.coupang_secret_key or "",
        sub_id=secrets.coupang_sub_id,
        cache_seconds=settings.coupang_cache_minutes * 60,
        **kw,
    )


def suggest_products(
    job_id: str,
    *,
    keyword: str | None = None,
    count: int | None = None,
    cfg: AppConfig | None = None,
    client: CoupangClient | None = None,
) -> dict:
    """파이프라인 단계: 분석 결과 키워드로 쿠팡 상품 상위 N개를 추천해 저장."""
    cfg = cfg or load_config()
    _require_editable(cfg, job_id)
    settings = load_settings()
    state = load_state(job_id, cfg=cfg)
    if not state.get("keyword_candidates"):
        state["keyword_candidates"] = keyword_candidates(
            _analysis(cfg, job_id), generic=cfg.default_hashtags
        )
    kw = (keyword or state.get("search_keyword") or (state["keyword_candidates"] or [""])[0]).strip()
    if not kw:
        raise MonetizeError("검색 키워드가 없습니다. 키워드를 입력하세요.")
    client = client or make_client(load_secrets(), settings)
    try:
        products = client.search(kw, limit=count or settings.suggest_count)
    except CoupangError as exc:
        raise MonetizeError(str(exc)) from None
    state["affiliate_enabled"] = True
    state["search_keyword"] = kw
    state["suggestions"] = [p.to_dict() for p in products]
    return save_state(job_id, state, cfg=cfg)


def choose_product(job_id: str, product_id: str, *, cfg: AppConfig | None = None) -> dict:
    cfg = cfg or load_config()
    _require_editable(cfg, job_id)
    state = load_state(job_id, cfg=cfg)
    match = next((p for p in state.get("suggestions") or [] if str(p.get("product_id")) == str(product_id)), None)
    if not match:
        raise MonetizeError("추천 목록에 없는 상품입니다. 다시 추천을 받아 주세요.")
    if not match.get("url", "").startswith("https://"):
        raise MonetizeError("상품 링크가 올바르지 않습니다.")
    state["affiliate_enabled"] = True
    state["link"] = {
        "url": match["url"],
        "source": "coupang_search",
        "product_id": match.get("product_id"),
        "product_name": match.get("name"),
        "chosen_at": datetime.now().isoformat(timespec="seconds"),
    }
    return save_state(job_id, state, cfg=cfg)


def set_manual_link(
    job_id: str,
    url: str,
    *,
    name: str = "",
    cfg: AppConfig | None = None,
    client: CoupangClient | None = None,
) -> dict:
    """사용자가 직접 붙여 넣은 제휴 링크 저장. 키가 있고 일반 쿠팡 상품 URL이면 딥링크로 바꾼다."""
    cfg = cfg or load_config()
    _require_editable(cfg, job_id)
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise MonetizeError("https:// 로 시작하는 링크를 넣어 주세요.")
    if len(url) > 500:
        raise MonetizeError("링크가 너무 깁니다.")
    source = "manual"
    host = (parts.hostname or "").lower()
    already_tracked = host == "link.coupang.com" or host.endswith("coupa.ng")
    secrets = load_secrets()
    if is_coupang_url(url) and not already_tracked and (client or secrets.has_coupang):
        client = client or make_client(secrets, load_settings())
        try:
            links = client.deeplink([url])
        except CoupangError as exc:
            raise MonetizeError(str(exc)) from None
        if links and (links[0].get("shorten_url") or links[0].get("landing_url")):
            url = links[0].get("shorten_url") or links[0]["landing_url"]
            source = "coupang_deeplink"
    state = load_state(job_id, cfg=cfg)
    state["affiliate_enabled"] = True
    state["link"] = {
        "url": url,
        "source": source,
        "product_id": None,
        "product_name": (name or "").strip()[:120],
        "chosen_at": datetime.now().isoformat(timespec="seconds"),
    }
    return save_state(job_id, state, cfg=cfg)


def clear_link(job_id: str, *, cfg: AppConfig | None = None) -> dict:
    cfg = cfg or load_config()
    _require_editable(cfg, job_id)
    state = load_state(job_id, cfg=cfg)
    state["link"] = None
    return save_state(job_id, state, cfg=cfg)


def set_comment_dm(
    job_id: str,
    *,
    enabled: bool | None = None,
    keywords: list[str] | None = None,
    cfg: AppConfig | None = None,
) -> dict:
    cfg = cfg or load_config()
    state = load_state(job_id, cfg=cfg)
    if enabled is not None:
        state["comment_dm_enabled"] = bool(enabled)
    if keywords is not None:
        state["dm_keywords"] = [k.strip() for k in keywords if k and k.strip()][:20]
    return save_state(job_id, state, cfg=cfg)


def attached_link(state: dict) -> str | None:
    if not state.get("affiliate_enabled"):
        return None
    link = state.get("link") or {}
    return link.get("url") or None


def final_caption_for_job(job, *, cfg: AppConfig | None = None) -> tuple[str, list[str]]:
    """게시 직전 호출. 제휴 링크가 있으면 (고지 문구가 맨 앞인 완성 캡션, []) 을 반환.

    제휴를 켰는데 링크를 고르지 않았으면 게시를 막는다 (사용자 선택 → 승인 순서 보장).
    제휴가 꺼져 있으면 원래 캡션/해시태그를 그대로 돌려준다.
    """
    cfg = cfg or load_config()
    try:
        state = load_state(job.job_id, cfg=cfg)
    except FileNotFoundError:
        return job.caption, list(job.hashtags)
    if not state.get("affiliate_enabled"):
        return job.caption, list(job.hashtags)
    link = attached_link(state)
    if not link:
        raise MonetizeError(
            "쿠팡 제휴가 켜져 있지만 상품/링크가 선택되지 않았습니다. 미리보기에서 고르거나 제휴를 끄세요."
        )
    settings = load_settings()
    text = compose_affiliate_caption(
        job.caption,
        list(job.hashtags),
        disclosure=settings.disclosure_text,
        cta=settings.cta_text if state.get("comment_dm_enabled", True) else "",
    )
    return text, []


def preview_caption(job_id: str, *, cfg: AppConfig | None = None) -> dict:
    """PWA 미리보기용: 실제 게시될 캡션과 글자 수."""
    from insta_auto.affiliate.caption import CAPTION_LIMIT, char_count

    cfg = cfg or load_config()
    meta = _job_meta(cfg, job_id)

    class _J:
        pass

    j = _J()
    j.job_id = job_id
    j.caption = meta.get("caption", "")
    j.hashtags = list(meta.get("hashtags") or [])
    try:
        text, tags = final_caption_for_job(j, cfg=cfg)
        tag_line = " ".join(f"#{t.lstrip('#')}" for t in tags if t)
        full = "\n\n".join(p for p in (text.strip(), tag_line) if p)
        error = None
    except MonetizeError as exc:
        full, error = "", str(exc)
    return {"caption": full, "chars": char_count(full), "limit": CAPTION_LIMIT, "error": error}


def affiliate_posts(cfg: AppConfig | None = None, limit: int = 50) -> list[dict]:
    """게시 완료 + 제휴 링크가 있는 작업 목록 (댓글→DM 대상)."""
    cfg = cfg or load_config()
    out: list[dict] = []
    if not cfg.output_dir.exists():
        return out
    for d in sorted(cfg.output_dir.iterdir(), reverse=True):
        if not (d / FILENAME).exists() or not (d / "job.json").exists():
            continue
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
            state = load_state(d.name, cfg=cfg)
        except (OSError, json.JSONDecodeError, FileNotFoundError):
            continue
        link = attached_link(state)
        if not link:
            continue
        out.append(
            {
                "job_id": d.name,
                "topic": meta.get("topic", ""),
                "status": meta.get("status"),
                "media_ids": list(meta.get("media_ids") or []),
                "link": link,
                "product_name": (state.get("link") or {}).get("product_name") or "",
                "comment_dm_enabled": bool(state.get("comment_dm_enabled", True)),
                "dm_keywords": list(state.get("dm_keywords") or []),
            }
        )
        if len(out) >= limit:
            break
    return out
