from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest

from insta_auto.affiliate import coupang
from insta_auto.affiliate.coupang import (
    CoupangClient,
    CoupangError,
    authorization_header,
    build_search_path,
    sign,
    signed_date,
)

FIXED = datetime(2026, 10, 23, 5, 15, 30, tzinfo=timezone.utc)


def test_hmac_primitive_rfc4231_case2():
    # RFC 4231 Test Case 2 (key="Jefe") — sign() must be plain HMAC-SHA256 hex over the concatenation
    assert (
        sign("", "", "", "Jefe", "what do ya want for nothing?")
        == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    )


def test_signed_date_format_is_utc_yymmddThhmmssZ():
    assert signed_date(FIXED) == "261023T051530Z"
    kst = FIXED.astimezone(__import__("datetime").timezone(__import__("datetime").timedelta(hours=9)))
    assert signed_date(kst) == "261023T051530Z"


def test_search_signature_vector():
    # 기대값은 openssl 로 독립 계산:
    # printf '%s' '261023T051530ZGET/v2/.../products/searchkeyword=...&limit=5' | openssl dgst -sha256 -hmac test-secret-key
    path = build_search_path("에어프라이어", 5)
    assert path == (
        "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
        "?keyword=%EC%97%90%EC%96%B4%ED%94%84%EB%9D%BC%EC%9D%B4%EC%96%B4&limit=5"
    )
    header = authorization_header("GET", path, access_key="AK", secret_key="test-secret-key", now=FIXED)
    assert header == (
        "CEA algorithm=HmacSHA256, access-key=AK, signed-date=261023T051530Z, "
        "signature=6d1f8829f5131902836da280a0cc3d1cbbb0e672fb85cc9821821745ba5966a9"
    )


def test_deeplink_signature_vector_no_query():
    header = authorization_header(
        "POST", coupang.DEEPLINK_PATH, access_key="AK", secret_key="test-secret-key", now=FIXED
    )
    assert header.endswith("signature=2528ff2a317c2f228ae641c82c38bbb1d6fbd7e173147eeb3e7e3e42e14c4d61")


def _verify_request(req: httpx.Request, secret="sk"):
    auth = req.headers["Authorization"]
    parts = dict(p.strip().split("=", 1) for p in auth.split(" ", 1)[1].split(","))
    path = req.url.raw_path.decode()
    p, _, q = path.partition("?")
    msg = parts["signed-date"] + req.method + p + q
    assert parts["signature"] == hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    assert parts["access-key"] == "ak"
    return parts


def test_search_parses_products_and_caches():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        _verify_request(req)
        assert req.url.host == "api-gateway.coupang.com"
        assert req.url.params["keyword"] == "랜턴"
        assert req.url.params["subId"] == "mysub"
        return httpx.Response(200, json={
            "rCode": "0", "rMessage": "",
            "data": {"landingUrl": "https://link.coupang.com/x", "productData": [
                {"productId": 1, "productName": "캠핑 랜턴", "productPrice": 19900,
                 "productImage": "https://img/1.jpg", "productUrl": "https://link.coupang.com/re/AFF1",
                 "isRocket": True},
            ]},
        })

    c = CoupangClient("ak", "sk", sub_id="mysub", transport=httpx.MockTransport(handler))
    products = c.search("랜턴", limit=3)
    assert products[0].name == "캠핑 랜턴" and products[0].price == 19900
    assert products[0].url == "https://link.coupang.com/re/AFF1"
    c2 = CoupangClient("ak", "sk", sub_id="mysub", transport=httpx.MockTransport(handler))
    c2.search("랜턴", limit=3)
    assert len(calls) == 1  # 프로세스 전역 캐시 (검색 API 호출 한도 보호)


def test_deeplink_request_body_and_error_redaction():
    def handler(req: httpx.Request):
        _verify_request(req)
        body = json.loads(req.content)
        assert body == {"coupangUrls": ["https://www.coupang.com/vp/products/123"]}
        return httpx.Response(200, json={"rCode": "0", "data": [
            {"originalUrl": "https://www.coupang.com/vp/products/123",
             "shortenUrl": "https://link.coupang.com/a/abc", "landingUrl": "https://link.coupang.com/re/x"}]})

    c = CoupangClient("ak", "sk", transport=httpx.MockTransport(handler))
    out = c.deeplink(["https://www.coupang.com/vp/products/123"])
    assert out[0]["shorten_url"] == "https://link.coupang.com/a/abc"

    with pytest.raises(CoupangError):
        c.deeplink(["https://example.com/not-coupang"])

    def bad(req):
        return httpx.Response(401, json={"rCode": "401", "rMessage": "bad key sk-SECRETVALUE"})

    c3 = CoupangClient("ak", "sk-SECRETVALUE", transport=httpx.MockTransport(bad))
    with pytest.raises(CoupangError) as ei:
        c3.deeplink(["https://www.coupang.com/vp/products/1"])
    assert "sk-SECRETVALUE" not in str(ei.value)


def test_client_requires_keys():
    with pytest.raises(CoupangError):
        CoupangClient("", "")
