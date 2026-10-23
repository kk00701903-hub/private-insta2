const $ = (id) => document.getElementById(id);

const TITLES = {
  home: "시작",
  create: "만들기",
  preview: "미리보기",
  library: "보관함",
  queue: "예약",
  history: "작업 이력",
  settings: "설정",
};

const EYEBROWS = {
  home: "만들기",
  create: "Instagram",
  preview: "승인",
  library: "기록",
  queue: "예약 게시",
  history: "기록",
  settings: "환경설정",
};

const CAPTION_LIMIT = 2200;
const HASHTAG_LIMIT = 30;

const STATUS_LABELS = {
  ready: "생성됨",
  scheduled: "예약됨",
  published: "게시됨",
  failed: "실패",
  rejected: "거절됨",
};

const state = {
  view: "home",
  fmt: "photo",
  files: [],
  musicFile: null,
  job: null,
  slideIndex: 0,
  installPrompt: null,
  busy: false,
  editJobId: null,
  historyStatus: "",
  tokenBannerClosed: false,
};

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.hidden = true;
  }, 2400);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data?.detail || data?.message || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

// ---- 캡션 글자 수 (공백 포함, 백엔드 insta_auto/caption.py 와 같은 규칙) ----
function countChars(text) {
  return [...(text || "")].length;
}

function normalizeTags(raw) {
  const out = [];
  const seen = new Set();
  String(raw || "")
    .split(/[\s,#]+/)
    .map((t) => t.trim())
    .filter(Boolean)
    .forEach((t) => {
      const key = t.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(t);
    });
  return out;
}

function fullCaption(caption, tags) {
  const tagLine = tags.map((t) => `#${t}`).join(" ");
  return [String(caption || "").trim(), tagLine].filter(Boolean).join("\n\n");
}

function hashtagCount(caption, tags) {
  const set = new Set(tags.map((t) => t.toLowerCase()));
  for (const m of String(caption || "").matchAll(/#([^\s#]+)/g)) set.add(m[1].toLowerCase());
  return set.size;
}

function checkCaption(caption, tags) {
  const chars = countChars(fullCaption(caption, tags));
  const nTags = hashtagCount(caption, tags);
  const warnings = [];
  if (chars > CAPTION_LIMIT) {
    warnings.push(
      `캡션이 ${chars.toLocaleString()}자로 인스타 제한 ${CAPTION_LIMIT.toLocaleString()}자를 넘습니다 (공백·해시태그 포함).`
    );
  }
  if (nTags > HASHTAG_LIMIT) {
    warnings.push(`해시태그가 ${nTags}개로 인스타 제한 ${HASHTAG_LIMIT}개를 넘습니다.`);
  }
  return {
    chars,
    captionChars: countChars(String(caption || "").trim()),
    nTags,
    warnings,
    ok: !warnings.length,
  };
}

function editedCaption() {
  return {
    caption: $("edit-caption").value,
    hashtags: normalizeTags($("edit-tags").value),
  };
}

function renderCaptionMeter() {
  const { caption, hashtags } = editedCaption();
  const c = checkCaption(caption, hashtags);
  const near = c.chars > CAPTION_LIMIT * 0.9;
  $("caption-meter").innerHTML = `
    <span class="${c.chars > CAPTION_LIMIT ? "over" : near ? "near" : ""}">전체 ${c.chars.toLocaleString()} / ${CAPTION_LIMIT.toLocaleString()}자</span>
    <span class="muted">본문 ${c.captionChars.toLocaleString()}자 · 공백 포함</span>
    <span class="${c.nTags > HASHTAG_LIMIT ? "over" : ""}">해시태그 ${c.nTags} / ${HASHTAG_LIMIT}개</span>
  `;
  const warn = $("caption-warn");
  warn.hidden = c.ok;
  warn.innerHTML = c.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  return c;
}

function mediaUrl(rel) {
  if (!rel) return "";
  return `/media/${rel.split("\\").join("/")}`;
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("on", el.id === `view-${view}`);
  });
  document.querySelectorAll(".side-nav .nav, .mobile-nav .nav").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.view === view);
  });
  $("page-title").textContent = TITLES[view] || view;
  $("eyebrow").textContent = EYEBROWS[view] || "";
  $("shell").classList.remove("nav-open");
  $("nav-scrim").hidden = true;
  if (view === "library") refreshLibrary();
  if (view === "queue") refreshQueue();
  if (view === "history") refreshHistory();
  if (view === "settings") refreshSettings();
  if (view === "preview") renderPreview();
}

function setFmt(fmt) {
  state.fmt = fmt;
  document.querySelectorAll("#format-grid .count-opt").forEach((btn) => {
    const on = btn.dataset.fmt === fmt;
    btn.classList.toggle("primary", on);
    btn.classList.toggle("ghost", !on);
  });
}

function renderThumbs() {
  const box = $("photo-thumbs");
  box.innerHTML = "";
  state.files.forEach((file) => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.alt = file.name;
    box.appendChild(img);
  });
  $("photo-count").textContent = state.files.length
    ? `${state.files.length}장 선택됨`
    : "선택된 사진 없음";
}

function previewSlides() {
  if (!state.job) return [];
  const cards = (state.job.card_paths || []).map((p) => ({ type: "image", src: mediaUrl(p) }));
  if (state.job.reel_path) {
    return [
      ...cards,
      { type: "video", src: mediaUrl(state.job.reel_path) },
    ];
  }
  return cards;
}

function renderPreview() {
  const empty = $("preview-empty");
  const panel = $("preview-panel");
  if (!state.job) {
    empty.hidden = false;
    panel.hidden = true;
    return;
  }
  empty.hidden = true;
  panel.hidden = false;
  $("preview-topic").textContent = state.job.topic || state.job.job_id;
  $("preview-meta").textContent = `${state.job.job_id} · ${statusLabel(state.job.status)} · ${state.job.fmt}`;
  // 같은 작업을 다시 그릴 때는 편집 중인 내용을 덮어쓰지 않는다
  const editKey = `${state.job.job_id}:${state.job.updated_at || ""}:${state.job.status}`;
  if (state.editJobId !== editKey) {
    state.editJobId = editKey;
    $("edit-caption").value = state.job.caption || "";
    $("edit-tags").value = (state.job.hashtags || [])
      .map((t) => String(t).replace(/^#/, ""))
      .join(" ");
  }
  const st = state.job.status;
  const editable = st === "ready" || st === "failed";
  $("edit-caption").readOnly = !editable;
  $("edit-tags").readOnly = !editable;
  $("btn-save-caption").disabled = !editable || state.busy;
  $("input-schedule").disabled = !editable || state.busy;
  const check = renderCaptionMeter();
  const canPublish = editable && check.ok;
  $("btn-approve").disabled = !canPublish || state.busy;
  $("btn-schedule").disabled = !canPublish || state.busy;
  $("btn-reject").disabled = !(editable || st === "scheduled") || state.busy;
  $("btn-approve").textContent = st === "failed" ? "승인 · 다시 업로드" : "승인 · 지금 업로드";
  $("preview-status").textContent =
    st === "published"
      ? `게시됨: ${(state.job.media_ids || []).join(", ")}`
      : st === "rejected"
        ? "거절됨 — 로컬에만 보관"
        : st === "scheduled"
          ? `예약됨: ${fmtTime(state.job.scheduled_at)} 자동 게시 — 예약 탭에서 취소·즉시 게시`
          : st === "failed"
            ? `게시 실패: ${state.job.error || "-"} — 확인 후 다시 승인하세요`
            : !check.ok
              ? "인스타 제한을 넘어서 승인할 수 없습니다. 캡션을 줄여 주세요."
              : "승인 전에는 업로드되지 않습니다.";

  const slides = previewSlides();
  if (!slides.length) {
    $("preview-stage").innerHTML = "<p class='muted'>미디어 없음</p>";
    return;
  }
  if (state.slideIndex >= slides.length) state.slideIndex = 0;
  const slide = slides[state.slideIndex];
  const stage = $("preview-stage");
  stage.classList.toggle("reel-mode", slide.type === "video");
  stage.innerHTML = "";
  if (slide.type === "video") {
    const v = document.createElement("video");
    v.src = slide.src;
    v.controls = true;
    v.playsInline = true;
    stage.appendChild(v);
  } else {
    const img = document.createElement("img");
    img.src = slide.src;
    img.alt = `slide ${state.slideIndex + 1}`;
    stage.appendChild(img);
  }
}

async function refreshLibrary() {
  try {
    const data = await api("/api/jobs");
    const grid = $("library-grid");
    const empty = $("library-empty");
    grid.innerHTML = "";
    const jobs = data.jobs || [];
    empty.hidden = jobs.length > 0;
    jobs.forEach((job) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "library-card";
      btn.innerHTML = `
        <strong>${esc(job.topic || job.job_id)}</strong>
        <span class="muted">${esc(job.job_id)} · ${esc(job.fmt)}</span>
        <span class="chip ${esc(job.status)}">${esc(statusLabel(job.status))}</span>
      `;
      btn.addEventListener("click", async () => {
        state.job = await api(`/api/jobs/${job.job_id}`);
        state.slideIndex = 0;
        setView("preview");
      });
      grid.appendChild(btn);
    });
  } catch (err) {
    toast(err.message);
  }
}

async function refreshSettings() {
  try {
    const s = await api("/api/state");
    $("side-brand").textContent = `@${s.account || "top.pick00"}`;
    const k = s.kling || {};
    const cli = k.cli || {};
    let klingLine = "Kling: 미연결";
    if (cli.logged_in) {
      klingLine = `Kling CLI: 로그인됨${k.model ? ` · ${k.model}` : ""}`;
    } else if (cli.available) {
      klingLine = "Kling CLI: 설치됨 · 로그인 필요 (`npm run kling:login`)";
    } else if (k.has_api_key) {
      klingLine = "Kling REST API 키 설정됨";
    } else {
      klingLine = "Kling: 선택 (CLI 로그인 또는 API 키)";
    }
    const lines = [
      `Claude API: ${s.has_claude ? "연결됨" : "없음 (오프라인 가능)"}`,
      `Instagram Graph: ${s.has_ig ? "연결됨" : "미설정"}`,
      `이미지 호스트: ${s.has_image_host ? "연결됨" : "미설정"}`,
      klingLine,
      tokenLine(s.token),
      `예약 게시: ${s.scheduled_count || 0}건 대기${s.scheduler?.running ? "" : " · 스케줄러 꺼짐"}`,
    ];
    $("settings-status").innerHTML = lines.map((l) => `<li>${l}</li>`).join("");
    $("setup-bar").hidden = !!(s.has_claude || s.has_ig);
    renderTokenBanner(s.token);
    $("side-schedule").textContent = s.scheduled_count
      ? `승인 후 업로드 · 예약 ${s.scheduled_count}건`
      : "승인 후 업로드";
    if (!s.has_claude) {
      $("input-offline").checked = true;
    }
  } catch (err) {
    $("settings-status").innerHTML = `<li>API 연결 실패: ${err.message}</li>`;
  }
}

function tokenLine(t) {
  if (!t || !t.has_token) return "IG 토큰: 없음";
  if (t.days_left == null) return "IG 토큰 만료: 알 수 없음 (.env에 IG_TOKEN_OBTAINED_AT 필요)";
  if (t.expired) return "IG 토큰: 만료됨 — 새로 발급하세요";
  const auto = t.can_refresh ? "자동 갱신 켜짐" : "자동 갱신 꺼짐 (FB_APP_ID/SECRET 필요)";
  return `IG 토큰 만료: ${fmtTime(t.expires_at)} (약 ${Math.floor(t.days_left)}일 남음) · ${auto}`;
}

function renderTokenBanner(t) {
  const banner = $("token-banner");
  if (!t || !t.has_token || !t.warn || state.tokenBannerClosed) {
    banner.hidden = true;
    return;
  }
  let msg;
  if (t.expired) {
    msg = "IG 토큰이 만료되었습니다. 게시·예약 게시가 실패합니다. 새 토큰을 발급해 .env에 넣어 주세요.";
  } else {
    const days = Math.max(0, Math.floor(t.days_left));
    msg = `IG 토큰이 ${days}일 후 만료됩니다 (${fmtTime(t.expires_at)}). `;
    msg += t.can_refresh
      ? "서버가 자동 갱신을 시도합니다. 계속 보이면 python scripts/refresh_token.py 를 실행하세요."
      : ".env에 FB_APP_ID / FB_APP_SECRET을 넣으면 자동 갱신됩니다. 수동: python scripts/refresh_token.py";
  }
  $("token-banner-text").textContent = msg;
  banner.classList.toggle("expired", !!t.expired);
  banner.hidden = false;
}

// ---- 예약 목록 ----
function jobThumb(job) {
  if (job.card_paths?.length) {
    return `<img src="${esc(mediaUrl(job.card_paths[0]))}" alt="" loading="lazy" />`;
  }
  if (job.reel_path) return `<span class="job-thumb-reel">REEL</span>`;
  return "";
}

async function openJob(jobId) {
  try {
    state.job = await api(`/api/jobs/${jobId}`);
    state.slideIndex = 0;
    setView("preview");
  } catch (err) {
    toast(err.message);
  }
}

async function refreshQueue() {
  try {
    const data = await api("/api/schedule");
    const list = $("queue-list");
    const jobs = data.jobs || [];
    $("queue-empty").hidden = jobs.length > 0;
    list.innerHTML = "";
    jobs.forEach((job) => {
      const row = document.createElement("div");
      row.className = "job-row";
      row.innerHTML = `
        <div class="job-thumb">${jobThumb(job)}</div>
        <div class="job-main">
          <strong>${esc(job.topic || job.job_id)}</strong>
          <span class="muted">${esc(job.job_id)} · ${esc(job.fmt)}</span>
          <span class="job-when">${esc(fmtTime(job.scheduled_at))} 게시 예정</span>
        </div>
        <div class="job-actions">
          <button class="btn ghost" type="button" data-act="open">보기</button>
          <button class="btn primary" type="button" data-act="now">지금 게시</button>
          <button class="btn danger" type="button" data-act="cancel">예약 취소</button>
        </div>
      `;
      row.querySelector('[data-act="open"]').addEventListener("click", () => openJob(job.job_id));
      row.querySelector('[data-act="now"]').addEventListener("click", () => postNow(job));
      row.querySelector('[data-act="cancel"]').addEventListener("click", () => cancelSchedule(job));
      list.appendChild(row);
    });
  } catch (err) {
    toast(err.message);
  }
}

async function postNow(job) {
  if (state.busy) return;
  if (!confirm(`"${job.topic || job.job_id}" 을(를) 예약 시각을 기다리지 않고 지금 게시할까요?`)) return;
  state.busy = true;
  toast("업로드 중…");
  try {
    await api(`/api/jobs/${job.job_id}/post-now`, { method: "POST" });
    toast("게시 완료");
  } catch (err) {
    toast(err.message);
  } finally {
    state.busy = false;
    refreshQueue();
    refreshSettings();
  }
}

async function cancelSchedule(job) {
  if (state.busy) return;
  if (!confirm("예약을 취소할까요? 작업은 승인 대기 상태로 돌아갑니다.")) return;
  try {
    const updated = await api(`/api/jobs/${job.job_id}/schedule/cancel`, { method: "POST" });
    if (state.job?.job_id === job.job_id) state.job = updated;
    toast("예약 취소됨");
  } catch (err) {
    toast(err.message);
  } finally {
    refreshQueue();
    refreshSettings();
  }
}

// ---- 작업 이력 ----
function setHistoryStatus(status) {
  state.historyStatus = status;
  document.querySelectorAll("#history-status .filter-opt").forEach((btn) => {
    const on = btn.dataset.status === status;
    btn.classList.toggle("primary", on);
    btn.classList.toggle("ghost", !on);
  });
  refreshHistory();
}

function fileLinks(job) {
  const files = [
    ...(job.card_paths || []).map((p) => ({ href: mediaUrl(p), name: p.split("/").pop() })),
    ...(job.reel_path ? [{ href: mediaUrl(job.reel_path), name: "reel.mp4" }] : []),
  ];
  if (!files.length) return "";
  const zip = `/api/jobs/${encodeURIComponent(job.job_id)}/download`;
  return `
    <details class="job-files">
      <summary>결과 파일 ${files.length}개</summary>
      <div class="job-file-links">
        <a class="btn ghost" href="${esc(zip)}" download>전체 ZIP</a>
        ${files
          .map((f) => `<a href="${esc(f.href)}" download="${esc(`${job.job_id}_${f.name}`)}">${esc(f.name)}</a>`)
          .join("")}
      </div>
    </details>`;
}

async function refreshHistory() {
  try {
    const params = new URLSearchParams({ limit: "200" });
    if (state.historyStatus) params.set("status", state.historyStatus);
    const q = $("history-search").value.trim();
    if (q) params.set("q", q);
    const data = await api(`/api/jobs?${params}`);
    const list = $("history-list");
    const jobs = data.jobs || [];
    $("history-empty").hidden = jobs.length > 0;
    list.innerHTML = "";
    jobs.forEach((job) => {
      const row = document.createElement("div");
      row.className = "job-row";
      const extra =
        job.status === "scheduled"
          ? `<span class="job-when">${esc(fmtTime(job.scheduled_at))} 게시 예정</span>`
          : job.status === "failed"
            ? `<span class="job-error">${esc(job.error || "게시 실패")}</span>`
            : job.status === "published" && job.posted_at
              ? `<span class="muted">${esc(fmtTime(job.posted_at))} 게시 · ${esc((job.media_ids || []).join(", "))}</span>`
              : "";
      const from = job.regenerated_from ? ` · ${esc(job.regenerated_from)}에서 다시 만듦` : "";
      const retry =
        job.status === "failed"
          ? '<button class="btn primary" type="button" data-act="retry">게시 재시도</button>'
          : "";
      const regen = job.inputs?.photos?.length
        ? '<button class="btn" type="button" data-act="regen">다시 만들기</button>'
        : "";
      row.innerHTML = `
        <div class="job-thumb">${jobThumb(job)}</div>
        <div class="job-main">
          <strong>${esc(job.topic || job.job_id)}</strong>
          <span class="muted">${esc(job.job_id)} · ${esc(job.fmt)}${from}</span>
          <span><span class="chip ${esc(job.status)}">${esc(statusLabel(job.status))}</span></span>
          ${extra}
          ${fileLinks(job)}
        </div>
        <div class="job-actions">
          <button class="btn ghost" type="button" data-act="open">보기</button>
          ${retry}
          ${regen}
        </div>
      `;
      row.querySelector('[data-act="open"]').addEventListener("click", () => openJob(job.job_id));
      row.querySelector('[data-act="retry"]')?.addEventListener("click", async () => {
        await openJob(job.job_id);
        toast("내용을 확인하고 승인하면 다시 게시합니다");
      });
      row.querySelector('[data-act="regen"]')?.addEventListener("click", () => regenerate(job));
      list.appendChild(row);
    });
  } catch (err) {
    toast(err.message);
  }
}

async function regenerate(job) {
  if (state.busy) return;
  if (!confirm("같은 사진·코멘트로 새 시안을 다시 만들까요? (기존 작업은 그대로 남습니다)")) return;
  state.busy = true;
  toast("다시 만드는 중… 잠시만요");
  try {
    state.job = await api(`/api/jobs/${job.job_id}/regenerate`, { method: "POST" });
    state.slideIndex = 0;
    toast("새 시안이 준비됐습니다");
    setView("preview");
  } catch (err) {
    toast(err.message);
  } finally {
    state.busy = false;
  }
}

async function generate() {
  if (state.busy) return;
  if (!state.files.length) {
    toast("사진을 선택하세요");
    return;
  }
  const comment = $("input-comment").value.trim();
  if (!comment) {
    toast("코멘트를 입력하세요");
    return;
  }

  state.busy = true;
  $("btn-generate").disabled = true;
  $("create-status").textContent = "제작 중… 잠시만요";

  const fd = new FormData();
  state.files.forEach((f) => fd.append("photos", f, f.name));
  fd.append("comment", comment);
  fd.append("fmt", state.fmt);
  fd.append("count", String($("input-count").value || 5));
  fd.append("seconds", String($("input-seconds").value || 5));
  fd.append("mood", $("input-mood").value.trim());
  fd.append("offline", $("input-offline").checked ? "true" : "false");
  if (state.musicFile) fd.append("music", state.musicFile, state.musicFile.name);

  try {
    const job = await api("/api/jobs/create", { method: "POST", body: fd });
    state.job = job;
    state.slideIndex = 0;
    toast("시안이 준비됐습니다");
    setView("preview");
  } catch (err) {
    toast(err.message);
    $("create-status").textContent = err.message;
  } finally {
    state.busy = false;
    $("btn-generate").disabled = false;
  }
}

function jsonPost(body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

async function saveCaption() {
  if (!state.job || state.busy) return;
  try {
    state.job = await api(`/api/jobs/${state.job.job_id}/caption`, jsonPost(editedCaption()));
    state.editJobId = null;
    toast("캡션 저장됨");
  } catch (err) {
    toast(err.message);
  } finally {
    renderPreview();
  }
}

async function reloadJob() {
  // 상태가 바뀐 경우에만 교체 (편집 중인 캡션을 잃지 않도록)
  try {
    const fresh = await api(`/api/jobs/${state.job.job_id}`);
    if (fresh.status !== state.job.status) {
      state.job = fresh;
      state.editJobId = null;
    }
  } catch {
    /* ignore */
  }
}

async function approveSchedule() {
  if (!state.job || state.busy) return;
  const value = $("input-schedule").value;
  if (!value) {
    toast("예약할 날짜와 시간을 고르세요");
    return;
  }
  const when = new Date(value);
  if (Number.isNaN(when.getTime()) || when.getTime() <= Date.now() + 60 * 1000) {
    toast("예약 시각은 지금보다 1분 이상 뒤여야 합니다");
    return;
  }
  if (!confirm(`${fmtTime(when.toISOString())}에 자동으로 게시하도록 승인할까요?`)) return;
  state.busy = true;
  try {
    state.job = await api(
      `/api/jobs/${state.job.job_id}/approve`,
      jsonPost({ ...editedCaption(), scheduled_at: when.toISOString() })
    );
    state.editJobId = null;
    $("input-schedule").value = "";
    toast("예약 승인 완료");
    refreshSettings();
  } catch (err) {
    toast(err.message);
    $("preview-status").textContent = err.message;
  } finally {
    state.busy = false;
    renderPreview();
  }
}

async function approve() {
  if (!state.job || state.busy) return;
  state.busy = true;
  $("preview-status").textContent = "업로드 중…";
  try {
    state.job = await api(`/api/jobs/${state.job.job_id}/approve`, jsonPost(editedCaption()));
    state.editJobId = null;
    toast("게시 완료");
    renderPreview();
  } catch (err) {
    toast(err.message);
    // 게시 실패 시 서버가 상태를 failed 로 바꾸므로 다시 읽어 온다
    await reloadJob();
    state.busy = false;
    renderPreview();
    $("preview-status").textContent = err.message;
    return;
  } finally {
    state.busy = false;
  }
  renderPreview();
}

async function reject() {
  if (!state.job || state.busy) return;
  state.busy = true;
  try {
    state.job = await api(`/api/jobs/${state.job.job_id}/reject`, { method: "POST" });
    toast("거절 · 로컬 보관");
    renderPreview();
  } catch (err) {
    toast(err.message);
  } finally {
    state.busy = false;
  }
}

function bindNav() {
  document.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  $("btn-nav-open")?.addEventListener("click", () => {
    $("shell").classList.add("nav-open");
    $("nav-scrim").hidden = false;
  });
  $("nav-scrim")?.addEventListener("click", () => {
    $("shell").classList.remove("nav-open");
    $("nav-scrim").hidden = true;
  });
  $("btn-sidebar-toggle")?.addEventListener("click", () => {
    $("shell").classList.toggle("sidebar-collapsed");
  });
}

function bindHome() {
  $("btn-choose-photo")?.addEventListener("click", () => {
    setFmt("photo");
    setView("create");
  });
  $("btn-choose-reel")?.addEventListener("click", () => {
    setFmt("reel");
    setView("create");
  });
  $("btn-choose-both")?.addEventListener("click", () => {
    setFmt("both");
    setView("create");
  });
  $("btn-choose-library")?.addEventListener("click", () => setView("library"));
  $("btn-back-home-create")?.addEventListener("click", () => setView("home"));
  $("btn-back-home-preview")?.addEventListener("click", () => setView("home"));
  $("btn-go-create")?.addEventListener("click", () => setView("create"));
}

function bindCreate() {
  const dz = $("dropzone");
  const input = $("input-photos");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    state.files = [...input.files];
    renderThumbs();
  });
  ["dragenter", "dragover"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("drag");
    });
  });
  dz.addEventListener("drop", (e) => {
    state.files = [...(e.dataTransfer?.files || [])].filter((f) =>
      f.type.startsWith("image/")
    );
    renderThumbs();
  });

  document.querySelectorAll("#format-grid .count-opt").forEach((btn) => {
    btn.addEventListener("click", () => setFmt(btn.dataset.fmt));
  });
  $("input-music")?.addEventListener("change", (e) => {
    state.musicFile = e.target.files?.[0] || null;
  });
  $("btn-generate")?.addEventListener("click", generate);
}

function bindPreview() {
  $("btn-prev-slide")?.addEventListener("click", () => {
    const slides = previewSlides();
    if (!slides.length) return;
    state.slideIndex = (state.slideIndex - 1 + slides.length) % slides.length;
    renderPreview();
  });
  $("btn-next-slide")?.addEventListener("click", () => {
    const slides = previewSlides();
    if (!slides.length) return;
    state.slideIndex = (state.slideIndex + 1) % slides.length;
    renderPreview();
  });
  $("btn-approve")?.addEventListener("click", approve);
  $("btn-schedule")?.addEventListener("click", approveSchedule);
  $("btn-save-caption")?.addEventListener("click", saveCaption);
  $("edit-caption")?.addEventListener("input", () => renderPreview());
  $("edit-tags")?.addEventListener("input", () => renderPreview());
  $("btn-reject")?.addEventListener("click", reject);
  $("btn-refresh-library")?.addEventListener("click", refreshLibrary);
}

function bindQueueHistory() {
  $("btn-refresh-queue")?.addEventListener("click", refreshQueue);
  $("btn-refresh-history")?.addEventListener("click", refreshHistory);
  document.querySelectorAll("#history-status .filter-opt").forEach((btn) => {
    btn.addEventListener("click", () => setHistoryStatus(btn.dataset.status));
  });
  let timer;
  $("history-search")?.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(refreshHistory, 250);
  });
  $("btn-token-banner-close")?.addEventListener("click", () => {
    state.tokenBannerClosed = true;
    $("token-banner").hidden = true;
  });
}

function bindInstall() {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    state.installPrompt = e;
    $("install-bar").hidden = false;
  });
  $("btn-install")?.addEventListener("click", async () => {
    if (!state.installPrompt) return;
    state.installPrompt.prompt();
    await state.installPrompt.userChoice;
    state.installPrompt = null;
    $("install-bar").hidden = true;
  });
  $("btn-install-close")?.addEventListener("click", () => {
    $("install-bar").hidden = true;
  });
  $("btn-setup-go")?.addEventListener("click", () => setView("settings"));
  $("btn-setup-close")?.addEventListener("click", () => {
    $("setup-bar").hidden = true;
  });
}

export function boot() {
  bindNav();
  bindHome();
  bindCreate();
  bindPreview();
  bindQueueHistory();
  bindInstall();
  setFmt("photo");
  setView("home");
  refreshSettings();
  // 토큰 만료 배너·예약 건수 주기적으로 갱신
  setInterval(refreshSettings, 10 * 60 * 1000);
}
