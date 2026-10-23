// 수익화 에이전트 UI — 쿠팡 파트너스 제휴(미리보기 패널) + 댓글→DM 설정·로그(수익화 화면)
// app.js 와는 'studio:job' 이벤트와 data-view="monetize" 로만 연결한다.

const $ = (id) => document.getElementById(id);

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let currentJob = null;
let busy = false;

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2600);
}

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    delete opts.json;
  }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const d = data?.detail || res.statusText;
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return data;
}

const splitKeywords = (s) =>
  String(s || "")
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

// ------------------------------------------------------------------ 미리보기 패널 (작업별)
async function loadJobPanel(job) {
  const box = $("monetize-panel");
  if (!box) return;
  currentJob = job;
  if (!job) {
    box.innerHTML = "";
    return;
  }
  try {
    const data = await api(`/api/monetize/jobs/${encodeURIComponent(job.job_id)}`);
    if (currentJob?.job_id !== job.job_id) return;
    renderJobPanel(job, data);
  } catch (err) {
    box.innerHTML = `<p class="hint">수익화 정보를 불러오지 못했습니다: ${esc(err.message)}</p>`;
  }
}

function renderJobPanel(job, data) {
  const box = $("monetize-panel");
  const st = data.state || {};
  const cap = data.caption_preview || {};
  const editable = ["ready", "error", "scheduled"].includes(job.status);
  const link = st.link;
  const products = st.suggestions || [];

  const productHtml = products
    .map((p) => {
      const on = link && String(link.product_id) === String(p.product_id);
      const price = p.price != null ? `${Number(p.price).toLocaleString("ko-KR")}원` : "";
      return `<button type="button" class="mz-product${on ? " on" : ""}" data-pid="${esc(p.product_id)}" ${editable ? "" : "disabled"}>
        ${p.image ? `<img src="${esc(p.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : ""}
        <span><strong>${esc(p.name)}</strong><small>${esc(price)}${p.is_rocket ? " · 로켓" : ""}</small></span>
      </button>`;
    })
    .join("");

  const candidates = (st.keyword_candidates || [])
    .map((k) => `<button type="button" class="chip mz-kw" data-kw="${esc(k)}" ${editable ? "" : "disabled"}>${esc(k)}</button>`)
    .join(" ");

  box.innerHTML = `
    <div class="mz-card">
      <label class="mz-switch">
        <input type="checkbox" id="mz-aff-on" ${st.affiliate_enabled ? "checked" : ""} ${editable ? "" : "disabled"} />
        <strong>쿠팡 파트너스 제휴 링크</strong>
      </label>
      ${
        st.affiliate_enabled
          ? `
        <p class="hint">링크가 붙으면 공정위 고지 문구가 캡션 <b>맨 앞</b>에 자동으로 들어갑니다 (삭제·이동 불가).</p>
        <div class="url-row">
          <input id="mz-keyword" type="text" value="${esc(st.search_keyword || "")}" placeholder="검색 키워드" ${editable ? "" : "disabled"} />
          <button class="btn ghost" id="mz-suggest" type="button" ${editable ? "" : "disabled"}>상품 추천</button>
        </div>
        ${candidates ? `<div class="mz-kws">${candidates}</div>` : ""}
        <div class="mz-products">${productHtml}</div>
        <details class="guide" ${products.length ? "" : "open"}>
          <summary>제휴 링크 직접 붙여 넣기</summary>
          <div class="url-row">
            <input id="mz-manual-url" type="url" placeholder="https://link.coupang.com/a/..." ${editable ? "" : "disabled"} />
            <button class="btn ghost" id="mz-manual-save" type="button" ${editable ? "" : "disabled"}>저장</button>
          </div>
          <input id="mz-manual-name" type="text" placeholder="상품 이름 (선택)" ${editable ? "" : "disabled"} />
        </details>
        <p class="hint">선택된 링크: ${
          link ? `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.product_name || link.url)}</a> (${esc(link.source)})` : "<b>없음 — 승인 전에 선택하세요</b>"
        }</p>
        <label class="mz-switch">
          <input type="checkbox" id="mz-post-dm" ${st.comment_dm_enabled ? "checked" : ""} />
          이 게시물 댓글에 키워드가 있으면 DM으로 링크 보내기
        </label>
        ${data.comment_dm_global ? "" : `<p class="hint mz-error">전체 댓글→DM 스위치가 꺼져 있습니다. '수익화' 화면에서 켜야 DM이 나갑니다.</p>`}
        <input id="mz-post-keywords" type="text" value="${esc((st.dm_keywords || []).join(", "))}" placeholder="게시물별 키워드 (비우면 기본: 링크, link, 정보)" />
        <h4 style="margin:14px 0 4px;">실제 게시될 캡션 <small class="muted">${cap.chars ?? 0} / ${cap.limit ?? 2200}자 (공백 포함)</small></h4>
        ${cap.error ? `<p class="hint mz-error">${esc(cap.error)}</p>` : `<pre class="mz-caption">${esc(cap.caption || "")}</pre>`}
      `
          : ""
      }
    </div>`;

  const jid = encodeURIComponent(job.job_id);
  const run = async (fn, okMsg) => {
    if (busy) return;
    busy = true;
    try {
      const d = await fn();
      if (okMsg) toast(okMsg);
      renderJobPanel(job, d);
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  };

  $("mz-aff-on")?.addEventListener("change", (e) =>
    run(() => api(`/api/monetize/jobs/${jid}/affiliate`, { method: "POST", json: { enabled: e.target.checked } }))
  );
  $("mz-suggest")?.addEventListener("click", () =>
    run(
      () => api(`/api/monetize/jobs/${jid}/suggest`, { method: "POST", json: { keyword: $("mz-keyword").value.trim() || null } }),
      "추천 상품을 불러왔습니다"
    )
  );
  box.querySelectorAll(".mz-kw").forEach((b) =>
    b.addEventListener("click", () => {
      $("mz-keyword").value = b.dataset.kw;
    })
  );
  box.querySelectorAll(".mz-product").forEach((b) =>
    b.addEventListener("click", () =>
      run(() => api(`/api/monetize/jobs/${jid}/choose`, { method: "POST", json: { product_id: b.dataset.pid } }), "상품 선택됨")
    )
  );
  $("mz-manual-save")?.addEventListener("click", () =>
    run(
      () =>
        api(`/api/monetize/jobs/${jid}/manual-link`, {
          method: "POST",
          json: { url: $("mz-manual-url").value.trim(), name: $("mz-manual-name").value.trim() },
        }),
      "링크 저장됨"
    )
  );
  $("mz-post-dm")?.addEventListener("change", (e) =>
    run(() => api(`/api/monetize/jobs/${jid}/comment-dm`, { method: "POST", json: { enabled: e.target.checked } }))
  );
  $("mz-post-keywords")?.addEventListener("change", (e) =>
    run(
      () => api(`/api/monetize/jobs/${jid}/comment-dm`, { method: "POST", json: { keywords: splitKeywords(e.target.value) } }),
      "키워드 저장됨"
    )
  );
}

// ------------------------------------------------------------------ 수익화 화면 (전체 설정 · 로그)
const REASONS = {
  older_than_7_days: "7일 지난 댓글",
  no_timestamp: "작성 시각 없음",
  rate_limited_hourly: "시간당 한도 도달",
  rate_limited_interval: "발송 간격 대기",
  private_reply_failed: "비공개 답장 실패",
  list_comments_failed: "댓글 조회 실패",
};

const reasonText = (r) => {
  const key = String(r || "").split(" ")[0];
  return REASONS[key] ? `${REASONS[key]}${r.includes("(") ? " · 재시도" : ""}` : r || "";
};

async function refreshMonetizeView() {
  try {
    const [status, posts, log] = await Promise.all([
      api("/api/monetize/status"),
      api("/api/monetize/posts"),
      api("/api/monetize/log?limit=200"),
    ]);
    const s = status.settings;
    const sec = status.secrets;
    $("mz-global").checked = !!s.comment_dm_enabled;
    $("mz-keywords").value = (s.keywords || []).join(", ");
    $("mz-public").checked = !!s.public_reply_enabled;
    $("mz-public-text").value = s.public_reply_text || "";
    $("mz-max").value = s.max_dm_per_hour;
    $("mz-poll").checked = !!s.poll_enabled;
    $("mz-interval").value = s.poll_interval_seconds;
    const p = status.poller || {};
    $("mz-keys").innerHTML = [
      `쿠팡 파트너스 API 키: ${sec.has_coupang_keys ? "설정됨" : "없음 (링크 직접 입력 가능)"}`,
      `Instagram 토큰: ${sec.has_ig ? "설정됨" : "미설정"}`,
      `페이지 토큰: ${sec.has_page_token ? "설정됨" : "자동 조회 (/me/accounts)"}`,
      `웹훅: ${sec.has_webhook_verify_token && sec.has_app_secret ? "준비됨 — /api/monetize/webhook/instagram" : "미설정 (폴링만 사용)"}`,
      `폴링: ${p.running ? "실행 중" : "정지"}${p.last_run ? ` · 마지막 ${esc(p.last_run.at || "")}` : ""}${p.last_error ? ` · 오류: ${esc(p.last_error)}` : ""}`,
    ]
      .map((l) => `<li>${l}</li>`)
      .join("");

    const list = posts.posts || [];
    $("mz-posts").innerHTML = list.length
      ? list
          .map(
            (x) => `<label class="mz-post">
              <input type="checkbox" data-job="${esc(x.job_id)}" ${x.comment_dm_enabled ? "checked" : ""} />
              <span><strong>${esc(x.topic || x.job_id)}</strong>
              <small class="muted">${esc(x.status)} · ${esc(x.product_name || x.link)}${x.dm_keywords?.length ? ` · 키워드: ${esc(x.dm_keywords.join(", "))}` : ""}</small></span>
            </label>`
          )
          .join("")
      : `<p class="empty-copy">제휴 링크가 붙은 게시물이 없습니다.</p>`;
    $("mz-posts").querySelectorAll("input[data-job]").forEach((cb) =>
      cb.addEventListener("change", async () => {
        try {
          await api(`/api/monetize/jobs/${encodeURIComponent(cb.dataset.job)}/comment-dm`, {
            method: "POST",
            json: { enabled: cb.checked },
          });
          toast(cb.checked ? "이 게시물 DM 켜짐" : "이 게시물 DM 꺼짐");
        } catch (err) {
          toast(err.message);
        }
      })
    );

    const rows = log.log || [];
    $("mz-log").innerHTML = rows.length
      ? rows
          .map(
            (e) => `<tr class="mz-${esc(e.action)}">
              <td>${esc((e.ts || "").replace("T", " "))}</td>
              <td>${esc(e.action === "sent" ? "보냄" : e.action === "skipped" ? "건너뜀" : "오류")}</td>
              <td>${esc(e.username || "")}</td>
              <td>${esc(e.text || "")}</td>
              <td>${esc(e.keyword || "")}</td>
              <td>${esc(reasonText(e.reason))}${e.error ? `<br><small>${esc(e.error)}</small>` : ""}${e.public_reply ? `<br><small>공개 답글: ${esc(e.public_reply)}</small>` : ""}</td>
            </tr>`
          )
          .join("")
      : `<tr><td colspan="6" class="muted">아직 기록이 없습니다.</td></tr>`;
  } catch (err) {
    $("mz-keys").innerHTML = `<li>API 연결 실패: ${esc(err.message)}</li>`;
  }
}

async function saveSettings() {
  try {
    await api("/api/monetize/settings", {
      method: "POST",
      json: {
        comment_dm_enabled: $("mz-global").checked,
        keywords: splitKeywords($("mz-keywords").value),
        public_reply_enabled: $("mz-public").checked,
        public_reply_text: $("mz-public-text").value.trim(),
        max_dm_per_hour: Number($("mz-max").value) || 200,
        poll_enabled: $("mz-poll").checked,
        poll_interval_seconds: Number($("mz-interval").value) || 120,
      },
    });
    toast("저장됨");
    refreshMonetizeView();
  } catch (err) {
    toast(err.message);
  }
}

export function initMonetize() {
  let lastKey = "";
  window.addEventListener("studio:job", (e) => {
    const j = e.detail;
    const key = j ? `${j.job_id}:${j.status}` : "";
    if (key === lastKey && $("monetize-panel")?.childElementCount) return; // 슬라이드 이동 등은 무시
    lastKey = key;
    loadJobPanel(j);
  });
  document.querySelectorAll('[data-view="monetize"]').forEach((b) => b.addEventListener("click", refreshMonetizeView));
  $("mz-save")?.addEventListener("click", saveSettings);
  $("mz-global")?.addEventListener("change", saveSettings);
  $("mz-refresh")?.addEventListener("click", refreshMonetizeView);
  $("mz-poll-now")?.addEventListener("click", async () => {
    try {
      const r = await api("/api/monetize/poll-now", { method: "POST" });
      toast(r.skipped ? `건너뜀: ${r.skipped}` : `확인 완료 · 보냄 ${r.sent ?? 0}`);
      refreshMonetizeView();
    } catch (err) {
      toast(err.message);
    }
  });
}
