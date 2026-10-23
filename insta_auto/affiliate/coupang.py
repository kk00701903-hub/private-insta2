"""쿠팡 파트너스 Open API 클라이언트.

서명 방식 (쿠팡 파트너스 공식 예제 코드와 공개 구현 기준):

    signed-date = UTC 기준 ``yyMMdd'T'HHmmss'Z'``  (예: 261023T051530Z)
    message     = signed-date + METHOD + path + query   (구분자 없음, query 는 '?' 제외)
    signature   = hex( HMAC-SHA256(secret_key, message) )
    Authorization: CEA algorithm=HmacSHA256, access-key={AK}, signed-date={date}, signature={sig}

엔드포인트 (도메인 https://api-gateway.coupang.com):
    GET  /v2/providers/affiliate_open_api/apis/openapi/v1/products/search?keyword=..&limit=..&subId=..
    POST /v2/providers/affiliate_open_api/apis/openapi/v1/deeplink   {"coupangUrls": [...], "subId": ".."}

검색 API는 호출 한도가 매우 낮으므로(공식 안내상 시간당 수 회 수준) 결과를 캐시한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from insta_auto.affiliate.redact import redact

DOMAIN = "https://api-gateway.coupang.com"
BASE_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1"
SEARCH_PATH = f"{BASE_PATH}/products/search"
DEEPLINK_PATH = f"{BASE_PATH}/deeplink"

COUPANG_HOSTS = ("coupang.com", "coupa.ng")

# 검색 결과 캐시 (프로세스 전역 — 요청마다 클라이언트를 새로 만들어도 유지)
_SEARCH_CACHE: dict[str, tuple[float, list["Product"]]] = {}
_CACHE_LOCK = threading.Lock()


class CoupangError(RuntimeError):
    pass


def signed_date(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.strftime("%y%m%dT%H%M%SZ")


def sign(method: str, path: str, query: str, secret_key: str, date: str) -> str:
    message = f"{date}{method.upper()}{path}{query}"
    return hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def authorization_header(
    method: str,
    url_path_with_query: str,
    *,
    access_key: str,
    secret_key: str,
    now: datetime | None = None,
) -> str:
    path, _, query = url_path_with_query.partition("?")
    date = signed_date(now)
    sig = sign(method, path, query, secret_key, date)
    return (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={date}, signature={sig}"
    )


@dataclass
class Product:
    product_id: str
    name: str
    price: int | None
    image: str
    url: str  # 파트너스 추적 링크 (검색 API가 반환하는 productUrl)
    is_rocket: bool = False
    keyword: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_coupang_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in COUPANG_HOSTS)


def build_search_path(keyword: str, limit: int, sub_id: str | None = None) -> str:
    q = f"keyword={quote(keyword, safe='')}&limit={int(limit)}"
    if sub_id:
        q += f"&subId={quote(sub_id, safe='')}"
    return f"{SEARCH_PATH}?{q}"


class CoupangClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        sub_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
        cache_seconds: int = 3600,
        timeout: float = 15.0,
    ) -> None:
        if not access_key or not secret_key:
            raise CoupangError("COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 가 필요합니다.")
        self._ak = access_key
        self._sk = secret_key
        self.sub_id = sub_id
        self._transport = transport
        self._timeout = timeout
        self._cache_seconds = cache_seconds

    def _request(self, method: str, path_with_query: str, body: dict | None = None) -> dict:
        headers = {
            "Authorization": authorization_header(
                method, path_with_query, access_key=self._ak, secret_key=self._sk
            ),
            "Content-Type": "application/json;charset=UTF-8",
        }
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.request(
                    method,
                    DOMAIN + path_with_query,
                    headers=headers,
                    content=json.dumps(body, ensure_ascii=False).encode("utf-8")
                    if body is not None
                    else None,
                )
        except httpx.HTTPError as exc:
            raise CoupangError(redact(f"쿠팡 API 연결 실패: {exc}", [self._sk, self._ak])) from None
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text[:300]}
        rcode = str(payload.get("rCode", "")) if isinstance(payload, dict) else ""
        if not resp.is_success or (rcode and rcode != "0"):
            msg = payload.get("rMessage") if isinstance(payload, dict) else None
            raise CoupangError(
                redact(f"쿠팡 API 오류 {resp.status_code}: {msg or payload}", [self._sk, self._ak])
            )
        return payload

    def search(self, keyword: str, limit: int = 5) -> list[Product]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        limit = max(1, min(int(limit), 10))
        key = f"{keyword}|{limit}|{self.sub_id or ''}"
        with _CACHE_LOCK:
            hit = _SEARCH_CACHE.get(key)
            if hit and time.time() - hit[0] < self._cache_seconds:
                return list(hit[1])
        payload = self._request("GET", build_search_path(keyword, limit, self.sub_id))
        data = payload.get("data") or {}
        items = data.get("productData") if isinstance(data, dict) else data
        products: list[Product] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            price = it.get("productPrice")
            try:
                price = int(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            products.append(
                Product(
                    product_id=str(it.get("productId", "")),
                    name=str(it.get("productName", "")),
                    price=price,
                    image=str(it.get("productImage", "")),
                    url=str(it.get("productUrl", "")),
                    is_rocket=bool(it.get("isRocket")),
                    keyword=keyword,
                )
            )
        with _CACHE_LOCK:
            _SEARCH_CACHE[key] = (time.time(), products)
        return products

    def deeplink(self, urls: list[str]) -> list[dict]:
        urls = [u for u in urls if u]
        if not urls:
            return []
        for u in urls:
            if not is_coupang_url(u):
                raise CoupangError(f"쿠팡 URL만 딥링크로 변환할 수 있습니다: {u}")
        body: dict = {"coupangUrls": urls}
        if self.sub_id:
            body["subId"] = self.sub_id
        payload = self._request("POST", DEEPLINK_PATH, body)
        out = []
        for it in payload.get("data") or []:
            if isinstance(it, dict):
                out.append(
                    {
                        "original_url": it.get("originalUrl", ""),
                        "shorten_url": it.get("shortenUrl", ""),
                        "landing_url": it.get("landingUrl", ""),
                    }
                )
        return out
