"""댓글 → DM 링크 자동 응답 (Instagram 공식 '비공개 답장(Private Replies)' API).

정책 준수 규칙 (코드로 강제):
- 제휴 링크가 붙은 게시물의 댓글만 대상. 전체 스위치 + 게시물별 스위치가 모두 켜져 있어야 동작.
- 댓글이 설정 키워드와 맞을 때만, 그 댓글 작성자에게만 비공개 답장 **1회** (댓글 ID 기준, 디스크에 기록해 중복 방지).
- 댓글 작성 후 7일 이내만 보낸다 (Meta 정책).
- 우리 계정이 쓴 댓글은 건너뛴다.
- 시간당 발송 수 제한 (기본 200/시간, Meta 한도 750/시간보다 낮게) + 발송 간 최소 간격.
- 댓글을 남기지 않은 사람에게는 절대 보내지 않는다 (수신자는 항상 recipient.comment_id).
- 토큰·키는 로그에 남기지 않는다.

수신 방식:
  (a) 폴링 — API 서버 백그라운드 스레드가 주기적으로 최근 게시물 댓글을 조회 (공개 HTTPS 불필요)
  (b) 웹훅 — Meta Webhooks (GET 검증 / POST + X-Hub-Signature-256 서명 검증)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from insta_auto.affiliate.redact import redact
from insta_auto.affiliate.settings import (
    MonetizeSecrets,
    MonetizeSettings,
    data_dir,
    load_secrets,
    load_settings,
)

GRAPH_VERSION = "v22.0"
FB_GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
IG_GRAPH = f"https://graph.instagram.com/{GRAPH_VERSION}"
STATE_RETENTION_DAYS = 30


class CommentDMError(RuntimeError):
    pass


# ---------------------------------------------------------------- matching
def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def match_keyword(text: str, keywords: list[str]) -> str | None:
    """댓글에 키워드가 있으면 그 키워드를 반환.

    - 대소문자·전각/반각 무시 (NFKC + casefold)
    - 한글 키워드는 부분 일치 ('링크요', '링크 주세요' 매치)
    - 영문/숫자 키워드는 단어 단위 일치 ('link' 는 'linkedin' 과 매치되지 않음)
    """
    t = _norm(text)
    if not t:
        return None
    for kw in keywords or []:
        k = _norm(str(kw)).strip()
        if not k:
            continue
        if re.fullmatch(r"[0-9a-z _\-.]+", k):
            if re.search(rf"(?<![0-9a-z]){re.escape(k)}(?![0-9a-z])", t):
                return str(kw)
        elif k in t:
            return str(kw)
    return None


def parse_ts(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # ms
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_ts(float(s))
    # Graph: 2026-10-20T12:34:56+0000
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def within_window(comment_time: datetime | None, now: datetime, days: int = 7) -> bool:
    if comment_time is None:
        return False
    days = max(1, min(int(days), 7))  # Meta: 7일 초과 불가
    age = now - comment_time
    return timedelta(0) - timedelta(minutes=5) <= age <= timedelta(days=days)


# ---------------------------------------------------------------- model
@dataclass
class CommentEvent:
    comment_id: str
    media_id: str
    text: str
    from_id: str | None = None
    username: str | None = None
    timestamp: datetime | None = None
    parent_id: str | None = None
    source: str = "poll"  # poll | webhook


@dataclass
class PostContext:
    job_id: str
    media_id: str
    link: str
    product_name: str
    keywords: list[str]
    enabled: bool


# ---------------------------------------------------------------- persistence
class DMState:
    """처리한 댓글(중복 방지)과 발송 시각(속도 제한)을 디스크에 저장."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "comment_dm_state.json")
        self._lock = threading.RLock()
        self.processed: dict[str, dict] = {}
        self.sent_times: list[float] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.processed = dict(raw.get("processed") or {})
        self.sent_times = [float(t) for t in raw.get("sent_times") or []]

    def save(self) -> None:
        with self._lock:
            cutoff = time.time() - STATE_RETENTION_DAYS * 86400
            self.processed = {
                k: v for k, v in self.processed.items() if float(v.get("at", 0)) >= cutoff
            }
            self.sent_times = [t for t in self.sent_times if t >= time.time() - 3600 * 24]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"processed": self.processed, "sent_times": self.sent_times}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def status(self, comment_id: str) -> str | None:
        with self._lock:
            rec = self.processed.get(str(comment_id))
            return rec.get("status") if rec else None

    def mark(self, comment_id: str, status: str, now: float | None = None) -> None:
        with self._lock:
            self.processed[str(comment_id)] = {"status": status, "at": now or time.time()}
            self.save()

    def sent_last_hour(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            return sum(1 for t in self.sent_times if now - 3600 < t <= now)

    def record_sent(self, now: float | None = None) -> None:
        with self._lock:
            self.sent_times.append(now or time.time())
            self.save()

    def rate_check(self, max_per_hour: int, min_interval: float, now: float | None = None) -> str | None:
        """보낼 수 있으면 None, 아니면 사유."""
        now = now or time.time()
        with self._lock:
            if self.sent_last_hour(now) >= max_per_hour:
                return "rate_limited_hourly"
            if self.sent_times and now - max(self.sent_times) < min_interval:
                return "rate_limited_interval"
        return None


class DMLog:
    def __init__(self, path: Path | None = None, secrets: list[str] | None = None) -> None:
        self.path = path or (data_dir() / "comment_dm_log.jsonl")
        self._secrets = secrets or []
        self._lock = threading.Lock()

    def add(self, **entry) -> dict:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
        for k in ("text", "reason", "error"):
            if k in entry and entry[k] is not None:
                entry[k] = redact(entry[k], self._secrets)[:300]
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return entry

    def read(self, limit: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-max(1, limit):]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


# ---------------------------------------------------------------- Graph client
class GraphMessenger:
    def __init__(
        self,
        secrets: MonetizeSecrets,
        settings: MonetizeSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.s = secrets
        self.api = settings.messaging_api
        self._transport = transport
        self._page: tuple[str, str] | None = None

    @property
    def graph(self) -> str:
        return IG_GRAPH if self.api == "instagram_login" else FB_GRAPH

    def _call(self, method: str, url: str, *, token: str, params=None, json_body=None) -> dict:
        params = {**(params or {}), "access_token": token}
        try:
            with httpx.Client(transport=self._transport, timeout=30.0) as c:
                r = c.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise CommentDMError(redact(f"Graph 연결 실패: {exc}", self.s.secret_values())) from None
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text[:200]}
        if not r.is_success or (isinstance(payload, dict) and "error" in payload):
            err = payload.get("error") if isinstance(payload, dict) else payload
            if isinstance(err, dict):
                err = {k: err.get(k) for k in ("message", "type", "code", "error_subcode") if k in err}
            raise CommentDMError(redact(f"Graph API 오류 {r.status_code}: {err}", self.s.secret_values()))
        return payload

    def _user_token(self) -> str:
        if not self.s.ig_access_token:
            raise CommentDMError("IG_ACCESS_TOKEN 이 필요합니다.")
        return self.s.ig_access_token

    def page_credentials(self) -> tuple[str, str]:
        """Facebook 로그인 방식: 비공개 답장은 페이지 ID + 페이지 액세스 토큰으로 보낸다."""
        if self.s.fb_page_id and self.s.fb_page_access_token:
            return self.s.fb_page_id, self.s.fb_page_access_token
        if self._page:
            return self._page
        data = self._call(
            "GET",
            f"{FB_GRAPH}/me/accounts",
            token=self._user_token(),
            params={"fields": "id,access_token,instagram_business_account"},
        )
        for page in data.get("data") or []:
            ig = page.get("instagram_business_account") or {}
            if str(ig.get("id")) == str(self.s.ig_user_id) and page.get("access_token"):
                self._page = (str(page["id"]), str(page["access_token"]))
                return self._page
        raise CommentDMError(
            "IG 계정에 연결된 Facebook 페이지 토큰을 찾지 못했습니다. FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN 을 .env 에 넣으세요."
        )

    def list_comments(self, media_id: str, limit: int = 50) -> list[CommentEvent]:
        data = self._call(
            "GET",
            f"{self.graph}/{media_id}/comments",
            token=self._user_token(),
            params={"fields": "id,text,timestamp,username,from", "limit": str(limit)},
        )
        events = []
        for c in data.get("data") or []:
            frm = c.get("from") or {}
            events.append(
                CommentEvent(
                    comment_id=str(c.get("id")),
                    media_id=str(media_id),
                    text=str(c.get("text") or ""),
                    from_id=str(frm.get("id")) if frm.get("id") else None,
                    username=c.get("username") or frm.get("username"),
                    timestamp=parse_ts(c.get("timestamp")),
                    source="poll",
                )
            )
        return events

    def send_private_reply(self, comment_id: str, text: str) -> dict:
        body = {"recipient": {"comment_id": str(comment_id)}, "message": {"text": text}}
        if self.api == "instagram_login":
            if not self.s.ig_user_id:
                raise CommentDMError("IG_USER_ID 가 필요합니다.")
            return self._call("POST", f"{IG_GRAPH}/{self.s.ig_user_id}/messages",
                              token=self._user_token(), json_body=body)
        page_id, page_token = self.page_credentials()
        return self._call("POST", f"{FB_GRAPH}/{page_id}/messages", token=page_token, json_body=body)

    def public_reply(self, comment_id: str, text: str) -> dict:
        return self._call(
            "POST",
            f"{self.graph}/{comment_id}/replies",
            token=self._user_token(),
            params={"message": text},
        )


# ---------------------------------------------------------------- agent
def build_dm_text(template: str, *, link: str, disclosure: str, product: str = "") -> str:
    text = (template or "{link}").replace("{link}", link).replace("{disclosure}", disclosure)
    text = text.replace("{product}", product or "").strip()
    if link not in text:
        text = f"{text}\n{link}".strip()
    return text[:1000]


class CommentDMAgent:
    def __init__(
        self,
        *,
        settings: MonetizeSettings | None = None,
        secrets: MonetizeSecrets | None = None,
        messenger: GraphMessenger | None = None,
        state: DMState | None = None,
        log: DMLog | None = None,
        account_username: str | None = None,
        clock=time.time,
    ) -> None:
        self.settings = settings or load_settings()
        self.secrets = secrets or load_secrets()
        self.messenger = messenger or GraphMessenger(self.secrets, self.settings)
        self.state = state or DMState()
        self.log = log or DMLog(secrets=self.secrets.secret_values())
        self.account_username = (account_username or "").lstrip("@").casefold() or None
        self.clock = clock
        self._lock = threading.Lock()

    # -- helpers
    def _is_own(self, ev: CommentEvent) -> bool:
        if ev.from_id and self.secrets.ig_user_id and str(ev.from_id) == str(self.secrets.ig_user_id):
            return True
        if ev.username and self.account_username and ev.username.lstrip("@").casefold() == self.account_username:
            return True
        return False

    def _log(self, ev: CommentEvent, post: PostContext | None, action: str, reason: str = "", **extra) -> dict:
        return self.log.add(
            action=action,
            reason=reason,
            comment_id=ev.comment_id,
            media_id=ev.media_id,
            job_id=post.job_id if post else None,
            username=ev.username,
            text=ev.text,
            source=ev.source,
            **extra,
        )

    # -- main rule engine
    def process(self, ev: CommentEvent, post: PostContext | None) -> dict:
        """댓글 1개 처리. 반환: {"action": sent|skipped|ignored|error, "reason": ...}"""
        with self._lock:
            return self._process(ev, post)

    def _process(self, ev: CommentEvent, post: PostContext | None) -> dict:
        s = self.settings
        now = self.clock()
        if not ev.comment_id or ev.comment_id == "None":
            return {"action": "ignored", "reason": "no_comment_id"}
        prev = self.state.status(ev.comment_id)
        if prev and prev not in {"deferred"}:
            return {"action": "ignored", "reason": f"already_{prev}"}
        if not s.comment_dm_enabled:
            return {"action": "ignored", "reason": "global_off"}
        if post is None or not post.link:
            return {"action": "ignored", "reason": "not_affiliate_post"}
        if not post.enabled:
            return {"action": "ignored", "reason": "post_off"}
        if self._is_own(ev):
            self.state.mark(ev.comment_id, "own", now)
            return {"action": "ignored", "reason": "own_comment"}
        keywords = post.keywords or s.keywords
        kw = match_keyword(ev.text, keywords)
        if not kw:
            self.state.mark(ev.comment_id, "no_match", now)
            return {"action": "ignored", "reason": "no_match"}

        # 여기부터는 '키워드 매치' — 로그에 남긴다
        ts = ev.timestamp
        if ts is None and ev.source == "webhook":
            ts = datetime.fromtimestamp(now, tz=timezone.utc)  # 웹훅은 실시간 이벤트
        if not within_window(ts, datetime.fromtimestamp(now, tz=timezone.utc), s.reply_window_days):
            self.state.mark(ev.comment_id, "expired", now)
            self._log(ev, post, "skipped", "older_than_7_days" if ts else "no_timestamp", keyword=kw)
            return {"action": "skipped", "reason": "older_than_7_days"}

        limited = self.state.rate_check(s.max_dm_per_hour, s.min_seconds_between_dm, now)
        if limited:
            if prev != "deferred":
                self._log(ev, post, "skipped", limited + " (다음 주기에 재시도)", keyword=kw)
            self.state.mark(ev.comment_id, "deferred", now)
            return {"action": "skipped", "reason": limited}

        # 댓글당 1회: 보내기 전에 먼저 '발송 시도' 로 기록 → 실패해도 재시도하지 않음
        self.state.mark(ev.comment_id, "sending", now)
        text = build_dm_text(
            s.dm_template, link=post.link, disclosure=s.disclosure_text, product=post.product_name
        )
        try:
            self.messenger.send_private_reply(ev.comment_id, text)
        except Exception as exc:  # noqa: BLE001
            self.state.mark(ev.comment_id, "error", now)
            err = redact(exc, self.secrets.secret_values())
            self._log(ev, post, "error", "private_reply_failed", keyword=kw, error=err)
            return {"action": "error", "reason": err}
        self.state.record_sent(now)
        self.state.mark(ev.comment_id, "sent", now)
        public = None
        if s.public_reply_enabled and s.public_reply_text.strip():
            try:
                self.messenger.public_reply(ev.comment_id, s.public_reply_text.strip())
                public = "ok"
            except Exception as exc:  # noqa: BLE001
                public = "failed: " + redact(exc, self.secrets.secret_values())[:120]
        self._log(ev, post, "sent", "", keyword=kw, public_reply=public)
        return {"action": "sent", "reason": "", "public_reply": public}


# ---------------------------------------------------------------- post lookup
def post_contexts(cfg=None) -> dict[str, PostContext]:
    """media_id → PostContext (게시 완료 + 제휴 링크가 있는 작업)."""
    from insta_auto.affiliate.jobs import affiliate_posts

    out: dict[str, PostContext] = {}
    for p in affiliate_posts(cfg):
        if p.get("status") != "published":
            continue
        for mid in p.get("media_ids") or []:
            out[str(mid)] = PostContext(
                job_id=p["job_id"],
                media_id=str(mid),
                link=p["link"],
                product_name=p.get("product_name") or "",
                keywords=list(p.get("dm_keywords") or []),
                enabled=bool(p.get("comment_dm_enabled", True)),
            )
    return out


def poll_once(agent: CommentDMAgent, posts: dict[str, PostContext]) -> dict:
    summary = {"posts": 0, "comments": 0, "sent": 0, "skipped": 0, "errors": 0}
    limit = agent.settings.max_posts_to_poll
    for media_id, post in list(posts.items())[:limit]:
        if not post.enabled:
            continue
        summary["posts"] += 1
        try:
            comments = agent.messenger.list_comments(media_id)
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            agent.log.add(action="error", reason="list_comments_failed", media_id=media_id,
                          job_id=post.job_id, error=redact(exc, agent.secrets.secret_values()))
            continue
        for ev in comments:
            summary["comments"] += 1
            res = agent.process(ev, post)
            if res["action"] == "sent":
                summary["sent"] += 1
            elif res["action"] == "skipped":
                summary["skipped"] += 1
            elif res["action"] == "error":
                summary["errors"] += 1
    return summary


# ---------------------------------------------------------------- webhook
def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app_secret, raw_body)>"""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip().lower())


def verify_subscription(mode: str | None, token: str | None, challenge: str | None, expected: str | None) -> str | None:
    if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
        return challenge or ""
    return None


def parse_webhook(payload: dict) -> list[CommentEvent]:
    """Instagram 'comments' 필드 웹훅 → CommentEvent 목록."""
    events: list[CommentEvent] = []
    if not isinstance(payload, dict) or payload.get("object") not in {"instagram", "page"}:
        return events
    for entry in payload.get("entry") or []:
        etime = parse_ts(entry.get("time"))
        for ch in entry.get("changes") or []:
            if ch.get("field") not in {"comments"}:
                continue  # live_comments 등은 처리하지 않음
            v = ch.get("value") or {}
            frm = v.get("from") or {}
            media = v.get("media") or {}
            events.append(
                CommentEvent(
                    comment_id=str(v.get("id") or ""),
                    media_id=str(media.get("id") or ""),
                    text=str(v.get("text") or ""),
                    from_id=str(frm.get("id")) if frm.get("id") else None,
                    username=frm.get("username"),
                    timestamp=parse_ts(v.get("timestamp")) or etime,
                    parent_id=v.get("parent_id"),
                    source="webhook",
                )
            )
    return events


# ---------------------------------------------------------------- background poller
class CommentPoller:
    def __init__(self, cfg_loader=None) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cfg_loader = cfg_loader
        self.last_run: dict | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="comment-dm-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_once(self) -> dict:
        from insta_auto.config import load_config

        cfg = (self._cfg_loader or load_config)()
        settings = load_settings()
        secrets = load_secrets()
        if not settings.comment_dm_enabled:
            return {"skipped": "global_off"}
        if not (secrets.ig_user_id and secrets.ig_access_token):
            return {"skipped": "no_ig_credentials"}
        agent = CommentDMAgent(settings=settings, secrets=secrets, account_username=cfg.account)
        result = poll_once(agent, post_contexts(cfg))
        self.last_run = {"at": datetime.now().isoformat(timespec="seconds"), **result}
        return self.last_run

    def _run(self) -> None:
        while not self._stop.is_set():
            interval = 120
            try:
                settings = load_settings()
                interval = settings.poll_interval_seconds
                if settings.comment_dm_enabled and settings.poll_enabled:
                    self.run_once()
                    self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = redact(exc, load_secrets().secret_values())[:300]
            self._stop.wait(interval)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_run": self.last_run,
            "last_error": self.last_error,
        }


def event_to_dict(ev: CommentEvent) -> dict:
    d = asdict(ev)
    d["timestamp"] = ev.timestamp.isoformat() if ev.timestamp else None
    return d
