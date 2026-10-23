"""수익화 에이전트 API (FastAPI router).

web.py 에서는 ``register_monetize(app)`` 한 줄만 호출한다.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from insta_auto.affiliate import jobs as aff
from insta_auto.affiliate.settings import load_secrets, load_settings, update_runtime_settings
from insta_auto.comment_dm import (
    CommentDMAgent,
    CommentPoller,
    DMLog,
    parse_webhook,
    post_contexts,
    verify_signature,
    verify_subscription,
)
from insta_auto.config import load_config

router = APIRouter(prefix="/api/monetize", tags=["monetize"])
poller = CommentPoller()


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, (aff.MonetizeError, ValueError)):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


class SuggestBody(BaseModel):
    keyword: str | None = None
    count: int | None = None


class ChooseBody(BaseModel):
    product_id: str


class ManualLinkBody(BaseModel):
    url: str
    name: str = ""


class ToggleBody(BaseModel):
    enabled: bool


class PostDMBody(BaseModel):
    enabled: bool | None = None
    keywords: list[str] | None = None


class SettingsBody(BaseModel):
    comment_dm_enabled: bool | None = None
    keywords: list[str] | None = None
    public_reply_enabled: bool | None = None
    public_reply_text: str | None = None
    max_dm_per_hour: int | None = None
    poll_enabled: bool | None = None
    poll_interval_seconds: int | None = None


@router.get("/status")
def status():
    s = load_settings()
    return {
        "settings": s.to_dict(),
        "secrets": load_secrets().public_status(),
        "poller": poller.status(),
    }


@router.post("/settings")
def settings_update(body: SettingsBody):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"settings": update_runtime_settings(patch).to_dict()}


# ------------------------------------------------------------ 쿠팡 제휴 (작업별)
@router.get("/jobs/{job_id}")
def job_state(job_id: str):
    try:
        return {
            "state": aff.load_state(job_id),
            "caption_preview": aff.preview_caption(job_id),
            "comment_dm_global": load_settings().comment_dm_enabled,
        }
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.post("/jobs/{job_id}/affiliate")
def job_affiliate_toggle(job_id: str, body: ToggleBody):
    try:
        aff.enable_affiliate(job_id, body.enabled)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.post("/jobs/{job_id}/suggest")
def job_suggest(job_id: str, body: SuggestBody):
    try:
        aff.suggest_products(job_id, keyword=body.keyword, count=body.count)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.post("/jobs/{job_id}/choose")
def job_choose(job_id: str, body: ChooseBody):
    try:
        aff.choose_product(job_id, body.product_id)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.post("/jobs/{job_id}/manual-link")
def job_manual_link(job_id: str, body: ManualLinkBody):
    try:
        aff.set_manual_link(job_id, body.url, name=body.name)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.delete("/jobs/{job_id}/link")
def job_clear_link(job_id: str):
    try:
        aff.clear_link(job_id)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


@router.post("/jobs/{job_id}/comment-dm")
def job_comment_dm(job_id: str, body: PostDMBody):
    try:
        aff.set_comment_dm(job_id, enabled=body.enabled, keywords=body.keywords)
        return job_state(job_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


# ------------------------------------------------------------ 댓글 → DM
@router.get("/posts")
def posts():
    return {"posts": aff.affiliate_posts()}


@router.get("/log")
def dm_log(limit: int = 200):
    return {"log": DMLog().read(limit=max(1, min(limit, 1000)))}


@router.post("/poll-now")
def poll_now():
    try:
        return poller.run_once()
    except Exception as exc:  # noqa: BLE001
        raise _err(exc) from exc


# Meta Webhooks — 공개 HTTPS 주소를 연결한 경우에만 사용
@router.get("/webhook/instagram", response_class=PlainTextResponse)
def webhook_verify(request: Request):
    q = request.query_params
    challenge = verify_subscription(
        q.get("hub.mode"), q.get("hub.verify_token"), q.get("hub.challenge"),
        load_secrets().webhook_verify_token,
    )
    if challenge is None:
        raise HTTPException(403, "verification failed")
    return PlainTextResponse(challenge)


@router.post("/webhook/instagram")
async def webhook_receive(request: Request):
    raw = await request.body()
    secrets = load_secrets()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), secrets.fb_app_secret):
        raise HTTPException(403, "invalid signature")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "invalid json") from None
    events = parse_webhook(payload)
    if not events:
        return {"ok": True, "processed": 0}
    cfg = load_config()
    ctx = post_contexts(cfg)
    agent = CommentDMAgent(secrets=secrets, account_username=cfg.account)
    results = await run_in_threadpool(
        lambda: [agent.process(ev, ctx.get(ev.media_id)) for ev in events]
    )
    return {"ok": True, "processed": len(results)}


def register_monetize(app: FastAPI, *, start_poller: bool = True) -> None:
    """라우터 등록 + API 프로세스 수명 동안 댓글 폴링 스레드 실행."""
    app.include_router(router)
    if not start_poller:
        return
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        poller.start()
        try:
            async with original(a) as state:
                yield state
        finally:
            poller.stop()

    app.router.lifespan_context = lifespan
