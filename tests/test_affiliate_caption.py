from __future__ import annotations

import httpx
import pytest

from insta_auto.affiliate import jobs as aff
from insta_auto.affiliate.caption import (
    CAPTION_LIMIT,
    CaptionError,
    char_count,
    compose_affiliate_caption,
)
from insta_auto.affiliate.coupang import CoupangClient
from insta_auto.affiliate.settings import DEFAULT_CTA, DEFAULT_DISCLOSURE

D = DEFAULT_DISCLOSURE


def test_disclosure_is_at_very_start():
    text = compose_affiliate_caption("좋은 랜턴 소개", ["캠핑"], disclosure=D, cta=DEFAULT_CTA)
    assert text.startswith(D)
    assert text.index(DEFAULT_CTA) > text.index("좋은 랜턴 소개")
    assert text.endswith("#캠핑")


def test_disclosure_moved_from_middle_and_not_duplicated():
    body = f"본문 첫 줄\n\n{D}\n\n본문 끝"
    text = compose_affiliate_caption(body, [], disclosure=D, cta="")
    assert text.startswith(D)
    assert text.count(D) == 1


def test_empty_disclosure_rejected():
    with pytest.raises(CaptionError):
        compose_affiliate_caption("x", [], disclosure="   ")


def test_char_limit_counts_spaces_and_keeps_disclosure_and_cta():
    body = ("가 나 " * 1000).strip()  # 공백 포함 ~3000자
    tags = [f"태그{i}" for i in range(40)]
    text = compose_affiliate_caption(body, tags, disclosure=D, cta=DEFAULT_CTA)
    assert char_count(text) <= CAPTION_LIMIT
    assert len(text) <= CAPTION_LIMIT
    assert text.startswith(D)
    assert DEFAULT_CTA in text
    assert "…" in text


def test_hashtags_trimmed_before_body():
    body = "짧은 본문"
    tags = [f"아주긴해시태그{i:03d}" for i in range(30)]
    text = compose_affiliate_caption(body, tags, disclosure=D, cta=DEFAULT_CTA, limit=300)
    assert char_count(text) <= 300
    assert "짧은 본문" in text and "…" not in text
    assert text.count("#") < 30


def test_exact_limit_boundary():
    fixed = compose_affiliate_caption("", [], disclosure=D, cta="")
    room = CAPTION_LIMIT - len(fixed) - 2  # "\n\n"
    text = compose_affiliate_caption("a" * room, [], disclosure=D, cta="")
    assert len(text) == CAPTION_LIMIT and "…" not in text
    text2 = compose_affiliate_caption("a" * (room + 1), [], disclosure=D, cta="")
    assert len(text2) <= CAPTION_LIMIT and "…" in text2


class _Job:
    def __init__(self, job_id, caption="본문", hashtags=("캠핑",)):
        self.job_id = job_id
        self.caption = caption
        self.hashtags = list(hashtags)


def test_final_caption_unchanged_without_affiliate(cfg, make_job):
    jid = make_job()
    cap, tags = aff.final_caption_for_job(_Job(jid), cfg=cfg)
    assert cap == "본문" and tags == ["캠핑"]


def test_affiliate_enabled_without_link_blocks_publish(cfg, make_job):
    jid = make_job()
    aff.enable_affiliate(jid, True, cfg=cfg)
    with pytest.raises(aff.MonetizeError):
        aff.final_caption_for_job(_Job(jid), cfg=cfg)


def test_manual_link_flow_puts_disclosure_first(cfg, make_job):
    jid = make_job()
    aff.set_manual_link(jid, "https://link.coupang.com/a/xyz", cfg=cfg)
    cap, tags = aff.final_caption_for_job(_Job(jid, caption=f"{D}\n본문"), cfg=cfg)
    assert cap.startswith(D) and cap.count(D) == 1 and tags == []
    assert DEFAULT_CTA in cap and "#캠핑" in cap
    with pytest.raises(aff.MonetizeError):
        aff.set_manual_link(jid, "http://insecure.example", cfg=cfg)


def test_cannot_change_link_after_publish(cfg, make_job):
    jid = make_job(status="published")
    with pytest.raises(aff.MonetizeError):
        aff.set_manual_link(jid, "https://link.coupang.com/a/xyz", cfg=cfg)


def test_suggest_and_choose_step(cfg, make_job):
    jid = make_job()

    def handler(req):
        return httpx.Response(200, json={"rCode": "0", "data": {"productData": [
            {"productId": 11, "productName": "랜턴A", "productPrice": 1000, "productImage": "",
             "productUrl": "https://link.coupang.com/re/A"},
            {"productId": 12, "productName": "랜턴B", "productPrice": 2000, "productImage": "",
             "productUrl": "https://link.coupang.com/re/B"}]}})

    client = CoupangClient("ak", "sk", transport=httpx.MockTransport(handler))
    st = aff.suggest_products(jid, cfg=cfg, client=client)
    assert st["search_keyword"] == "캠핑"  # 기본 해시태그(추천/정보) 제외 후 첫 후보
    assert [p["product_id"] for p in st["suggestions"]] == ["11", "12"]
    st = aff.choose_product(jid, "12", cfg=cfg)
    assert st["link"]["url"] == "https://link.coupang.com/re/B"
    with pytest.raises(aff.MonetizeError):
        aff.choose_product(jid, "999", cfg=cfg)


def test_suggest_without_keys_explains_manual_fallback(cfg, make_job):
    jid = make_job()
    with pytest.raises(aff.MonetizeError) as ei:
        aff.suggest_products(jid, cfg=cfg)
    assert "직접" in str(ei.value)


def test_keyword_candidates_skip_generic():
    c = aff.keyword_candidates({"topic": "가을 캠핑: 감성 랜턴", "hashtags": ["#추천", "캠핑"]}, generic=["추천"])
    assert c[0] == "캠핑" and "추천" not in c and "감성 랜턴" in c
