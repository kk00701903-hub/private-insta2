from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from insta_auto.affiliate.settings import MonetizeSecrets, MonetizeSettings
from insta_auto.comment_dm import (
    CommentDMAgent,
    CommentEvent,
    DMLog,
    DMState,
    GraphMessenger,
    PostContext,
    match_keyword,
    parse_ts,
    parse_webhook,
    poll_once,
    verify_signature,
    verify_subscription,
    within_window,
)

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
T0 = NOW.timestamp()
SECRETS = MonetizeSecrets(
    ig_user_id="17840000000000001", ig_access_token="IGTOKEN-SECRET-123",
    fb_page_id="1000", fb_page_access_token="PAGETOKEN-SECRET-456",
    fb_app_secret="app-secret", webhook_verify_token="verify-me",
)


# ---------------------------------------------------------------- keyword matching
@pytest.mark.parametrize("text,expected", [
    ("링크 주세요!", "링크"),
    ("링크요", "링크"),
    ("LINK please", "link"),
    ("Ｌｉｎｋ", "link"),  # 전각
    ("정보 부탁드려요", "정보"),
    ("linkedin 에서 봤어요", None),
    ("예뻐요", None),
    ("", None),
])
def test_match_keyword(text, expected):
    assert match_keyword(text, ["링크", "link", "정보"]) == expected


# ---------------------------------------------------------------- 7-day window
def test_within_window():
    assert within_window(NOW - timedelta(days=6, hours=23), NOW, 7)
    assert not within_window(NOW - timedelta(days=7, minutes=1), NOW, 7)
    assert not within_window(None, NOW, 7)
    # 설정을 30일로 해도 7일로 고정
    assert not within_window(NOW - timedelta(days=8), NOW, 30)


def test_parse_ts_graph_format():
    assert parse_ts("2026-10-20T12:34:56+0000") == datetime(2026, 10, 20, 12, 34, 56, tzinfo=timezone.utc)
    assert parse_ts(1761220800) is not None


# ---------------------------------------------------------------- agent fixtures
class FakeMessenger:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.public: list[tuple[str, str]] = []
        self.fail = fail
        self.comments: dict[str, list[CommentEvent]] = {}

    def send_private_reply(self, comment_id, text):
        if self.fail:
            raise RuntimeError("boom access_token=IGTOKEN-SECRET-123")
        self.sent.append((comment_id, text))
        return {"recipient_id": "x"}

    def public_reply(self, comment_id, text):
        self.public.append((comment_id, text))
        return {"id": "r"}

    def list_comments(self, media_id):
        return self.comments.get(media_id, [])


@pytest.fixture
def clock():
    class C:
        t = T0

        def __call__(self):
            return self.t

    return C()


def make_agent(tmp_path, clock, messenger=None, **overrides):
    settings = MonetizeSettings(comment_dm_enabled=True, min_seconds_between_dm=0, **overrides)
    return CommentDMAgent(
        settings=settings, secrets=SECRETS, messenger=messenger or FakeMessenger(),
        state=DMState(tmp_path / "state.json"), log=DMLog(tmp_path / "log.jsonl", SECRETS.secret_values()),
        account_username="top.pick00", clock=clock,
    )


POST = PostContext(job_id="j1", media_id="m1", link="https://link.coupang.com/a/xyz",
                   product_name="랜턴", keywords=[], enabled=True)


def ev(cid="c1", text="링크 주세요", age=timedelta(hours=1), **kw):
    return CommentEvent(comment_id=cid, media_id="m1", text=text, from_id=kw.pop("from_id", "999"),
                        username=kw.pop("username", "fan"), timestamp=NOW - age, **kw)


def test_sends_one_private_reply_with_link_and_public_reply(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    r = agent.process(ev(), POST)
    assert r["action"] == "sent"
    m = agent.messenger
    assert len(m.sent) == 1 and m.sent[0][0] == "c1"
    assert "https://link.coupang.com/a/xyz" in m.sent[0][1]
    assert "쿠팡 파트너스" in m.sent[0][1]  # DM 에도 고지 포함
    assert m.public == [("c1", "DM 보냈어요!")]


def test_dedupe_persisted_to_disk(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    agent.process(ev(), POST)
    # 새 프로세스(새 상태 객체)에서도 다시 보내지 않음
    agent2 = make_agent(tmp_path, clock, messenger=agent.messenger)
    r = agent2.process(ev(), POST)
    assert r["action"] == "ignored" and r["reason"] == "already_sent"
    assert len(agent.messenger.sent) == 1


def test_failed_send_is_not_retried_and_token_not_logged(tmp_path, clock):
    m = FakeMessenger(fail=True)
    agent = make_agent(tmp_path, clock, messenger=m)
    assert agent.process(ev(), POST)["action"] == "error"
    m.fail = False
    assert agent.process(ev(), POST)["action"] == "ignored"
    assert m.sent == []
    raw = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "IGTOKEN-SECRET-123" not in raw


def test_skip_own_comment_and_non_matching(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    assert agent.process(ev(cid="own1", from_id=SECRETS.ig_user_id), POST)["reason"] == "own_comment"
    assert agent.process(ev(cid="own2", from_id="1", username="Top.Pick00"), POST)["reason"] == "own_comment"
    assert agent.process(ev(cid="c9", text="예뻐요"), POST)["reason"] == "no_match"
    assert agent.messenger.sent == []


def test_older_than_7_days_skipped_and_logged(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    r = agent.process(ev(age=timedelta(days=8)), POST)
    assert r == {"action": "skipped", "reason": "older_than_7_days"}
    log = agent.log.read()
    assert log[0]["action"] == "skipped" and log[0]["reason"] == "older_than_7_days"
    assert agent.messenger.sent == []


def test_switches(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    agent.settings.comment_dm_enabled = False
    assert agent.process(ev(), POST)["reason"] == "global_off"
    agent.settings.comment_dm_enabled = True
    off = PostContext(**{**POST.__dict__, "enabled": False})
    assert agent.process(ev(), off)["reason"] == "post_off"
    assert agent.process(ev(), None)["reason"] == "not_affiliate_post"
    # 스위치로 무시된 댓글은 나중에 켜면 처리 가능
    assert agent.process(ev(), POST)["action"] == "sent"


def test_per_post_keywords_override(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    post = PostContext(**{**POST.__dict__, "keywords": ["구매"]})
    assert agent.process(ev(cid="a", text="링크"), post)["reason"] == "no_match"
    assert agent.process(ev(cid="b", text="구매처 알려주세요"), post)["action"] == "sent"


def test_hourly_rate_limit_defers_then_resumes(tmp_path, clock):
    agent = make_agent(tmp_path, clock, max_dm_per_hour=2)
    assert agent.process(ev(cid="1"), POST)["action"] == "sent"
    assert agent.process(ev(cid="2"), POST)["action"] == "sent"
    r = agent.process(ev(cid="3"), POST)
    assert r == {"action": "skipped", "reason": "rate_limited_hourly"}
    agent.process(ev(cid="3"), POST)  # 다시 와도 로그는 한 번만
    assert sum(1 for e in agent.log.read() if e["comment_id"] == "3") == 1
    clock.t += 3601
    assert agent.process(ev(cid="3", age=timedelta(hours=2)), POST)["action"] == "sent"
    assert [c for c, _ in agent.messenger.sent] == ["1", "2", "3"]


def test_min_interval(tmp_path, clock):
    agent = make_agent(tmp_path, clock)
    agent.settings.min_seconds_between_dm = 10
    assert agent.process(ev(cid="1"), POST)["action"] == "sent"
    assert agent.process(ev(cid="2"), POST)["reason"] == "rate_limited_interval"
    clock.t += 11
    assert agent.process(ev(cid="2"), POST)["action"] == "sent"


def test_rate_limit_hard_cap():
    from insta_auto.affiliate.settings import HARD_MAX_DM_PER_HOUR, _coerce

    s = _coerce(MonetizeSettings(max_dm_per_hour=10_000, reply_window_days=30))
    assert s.max_dm_per_hour == HARD_MAX_DM_PER_HOUR < 750
    assert s.reply_window_days == 7


def test_poll_once(tmp_path, clock):
    m = FakeMessenger()
    m.comments["m1"] = [ev(cid="p1"), ev(cid="p2", text="예뻐요"), ev(cid="p3", age=timedelta(days=9))]
    agent = make_agent(tmp_path, clock, messenger=m)
    s = poll_once(agent, {"m1": POST})
    assert s["sent"] == 1 and s["skipped"] == 1 and s["comments"] == 3
    assert poll_once(agent, {"m1": POST})["sent"] == 0


# ---------------------------------------------------------------- Graph messenger (mocked HTTP)
def test_graph_private_reply_request_shape_facebook_login():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"recipient_id": "1", "message_id": "m"})

    gm = GraphMessenger(SECRETS, MonetizeSettings(), transport=httpx.MockTransport(handler))
    gm.send_private_reply("C123", "hi")
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/v22.0/1000/messages" and req.url.host == "graph.facebook.com"
    assert json.loads(req.content) == {"recipient": {"comment_id": "C123"}, "message": {"text": "hi"}}
    assert req.url.params["access_token"] == "PAGETOKEN-SECRET-456"


def test_graph_private_reply_instagram_login():
    seen = []
    gm = GraphMessenger(SECRETS, MonetizeSettings(messaging_api="instagram_login"),
                        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})))
    gm.send_private_reply("C1", "hi")
    assert seen[0].url.host == "graph.instagram.com"
    assert seen[0].url.path == f"/v22.0/{SECRETS.ig_user_id}/messages"


def test_graph_error_redacts_token():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "bad token IGTOKEN-SECRET-123", "code": 190}})

    gm = GraphMessenger(SECRETS, MonetizeSettings(), transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as ei:
        gm.list_comments("m1")
    assert "IGTOKEN-SECRET-123" not in str(ei.value)


def test_page_token_resolved_from_me_accounts():
    s = MonetizeSecrets(ig_user_id="IG1", ig_access_token="USERTOKEN")

    def handler(req):
        if req.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [
                {"id": "P0", "access_token": "OTHER", "instagram_business_account": {"id": "IGX"}},
                {"id": "P1", "access_token": "PT1", "instagram_business_account": {"id": "IG1"}}]})
        assert req.url.path.endswith("/P1/messages")
        assert req.url.params["access_token"] == "PT1"
        return httpx.Response(200, json={})

    GraphMessenger(s, MonetizeSettings(), transport=httpx.MockTransport(handler)).send_private_reply("c", "t")


# ---------------------------------------------------------------- webhook
def test_verify_signature():
    body = b'{"object":"instagram"}'
    # openssl dgst -sha256 -hmac app-secret
    good = "sha256=f9f60d2fd6f1498ad1636cf26d6916018f8c80c0a4ac67169c9499d510ee1762"
    assert verify_signature(body, good, "app-secret")
    assert not verify_signature(body + b" ", good, "app-secret")
    assert not verify_signature(body, good, "other")
    assert not verify_signature(body, None, "app-secret")
    assert not verify_signature(body, good, None)
    assert not verify_signature(body, "sha1=abc", "app-secret")


def test_verify_subscription():
    assert verify_subscription("subscribe", "verify-me", "123", "verify-me") == "123"
    assert verify_subscription("subscribe", "wrong", "123", "verify-me") is None
    assert verify_subscription("subscribe", "verify-me", "123", None) is None


def test_parse_webhook_comments():
    payload = {"object": "instagram", "entry": [{"id": "IG1", "time": 1761220800, "changes": [
        {"field": "comments", "value": {"from": {"id": "55", "username": "fan"},
                                        "media": {"id": "m1", "media_product_type": "FEED"},
                                        "id": "c77", "text": "링크"}},
        {"field": "live_comments", "value": {"id": "x"}}]}]}
    evs = parse_webhook(payload)
    assert len(evs) == 1 and evs[0].comment_id == "c77" and evs[0].media_id == "m1"
    assert evs[0].source == "webhook" and evs[0].timestamp is not None


def test_webhook_endpoint(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import insta_auto.monetize_api as api

    monkeypatch.setenv("FB_APP_SECRET", "app-secret")
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "verify-me")
    processed = []

    class StubAgent:
        def __init__(self, **kw):
            pass

        def process(self, ev, post):
            processed.append((ev.comment_id, post))
            return {"action": "ignored"}

    monkeypatch.setattr(api, "CommentDMAgent", StubAgent)
    monkeypatch.setattr(api, "post_contexts", lambda cfg: {"m1": POST})
    app = FastAPI()
    api.register_monetize(app, start_poller=False)
    client = TestClient(app)

    r = client.get("/api/monetize/webhook/instagram",
                   params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    assert client.get("/api/monetize/webhook/instagram",
                      params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"}).status_code == 403

    body = json.dumps({"object": "instagram", "entry": [{"id": "IG1", "time": 1, "changes": [
        {"field": "comments", "value": {"id": "c1", "text": "링크", "media": {"id": "m1"}, "from": {"id": "5"}}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert client.post("/api/monetize/webhook/instagram", content=body,
                       headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 403
    assert processed == []
    r = client.post("/api/monetize/webhook/instagram", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["processed"] == 1
    assert processed[0][0] == "c1" and processed[0][1] is POST


def test_status_endpoint_never_returns_secret_values(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import insta_auto.monetize_api as api

    monkeypatch.setenv("COUPANG_SECRET_KEY", "COUPANG-SECRET-XYZ")
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "COUPANG-AK-XYZ")
    app = FastAPI()
    api.register_monetize(app, start_poller=False)
    r = TestClient(app).get("/api/monetize/status")
    assert r.status_code == 200
    assert r.json()["secrets"]["has_coupang_keys"] is True
    assert "COUPANG-SECRET-XYZ" not in r.text and "COUPANG-AK-XYZ" not in r.text
