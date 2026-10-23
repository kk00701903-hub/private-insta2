// AI 릴스 view: request (+photos) -> storyboard -> PhotoCraft -> clips -> reel -> approve.
// Self-contained: injects its own nav button, home card and <section class="view"> before
// app.js boot() binds navigation, so app.js / index.html stay untouched.

const STAGES = [
  ["plan", "기획"],
  ["photocraft", "PhotoCraft"],
  ["clips", "클립"],
  ["assemble", "합성"],
];
const STATUS_LABEL = {
  pending: "대기",
  running: "진행 중",
  done: "완료",
  partial: "일부",
  error: "오류",
};
const LS_KEY = "insta2.aireels.last";

const ar = {
  files: [],
  mode: "free",
  project: null,
  pollTimer: null,
  busy: false,
  status: null,
};

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function media(rel) {
  return rel ? `/media/${String(rel).split("\\").join("/")}` : "";
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
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

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2600);
}

function remember(id) {
  try {
    localStorage.setItem(LS_KEY, id);
  } catch {
    /* storage may be unavailable */
  }
}

function recall() {
  try {
    return localStorage.getItem(LS_KEY);
  } catch {
    return null;
  }
}

const VIEW_HTML = `
  <div class="panel-head">
    <div>
      <h2>AI 릴스 만들기</h2>
      <p>요청만 쓰면 AI가 컷을 기획하고, PhotoCraft로 보정한 뒤 영상으로 만듭니다. 승인 전에는 올라가지 않습니다.</p>
    </div>
  </div>

  <div class="hero-panel ar-form">
    <label>무엇을 만들까요?
      <textarea id="ar-request" rows="4" placeholder="예: 성수동 가을 카페 3곳 소개, 따뜻하고 감성적인 분위기, 저장 유도"></textarea>
    </label>
    <div class="dropzone ar-drop" id="ar-drop">
      <input id="ar-photos" type="file" accept="image/*" multiple hidden />
      <p><strong>사진 추가 (선택)</strong></p>
      <p class="muted" id="ar-photo-count">사진 없이도 만들 수 있어요</p>
    </div>
    <div class="photo-thumbs" id="ar-thumbs"></div>

    <div class="ar-modes" role="radiogroup" aria-label="영상 방식">
      <button type="button" class="ar-mode on" data-mode="free" role="radio" aria-checked="true">
        <strong>무료 모션</strong><span>사진에 줌·팬·크로스페이드 (ffmpeg)</span>
      </button>
      <button type="button" class="ar-mode" data-mode="kling" role="radio" aria-checked="false">
        <strong>AI 영상 (Kling)</strong><span>컷마다 영상 생성 · 크레딧 사용 · 시작 전 확인</span>
      </button>
    </div>

    <div class="url-row">
      <label style="flex:1">길이 (초, 선택)
        <input id="ar-seconds" type="number" min="7" max="30" step="1" placeholder="7–30" />
      </label>
      <label style="flex:1">음악 (music/ 폴더)
        <select id="ar-music-name"><option value="">음악 없음</option></select>
      </label>
    </div>
    <label>또는 음악 파일 올리기
      <input id="ar-music-file" type="file" accept="audio/*,.mp3,.wav,.m4a" />
    </label>
    <label class="hint ar-check"><input id="ar-offline" type="checkbox" /> 오프라인 기획 (Claude 키 없이 테스트)</label>

    <div class="btn-row" style="margin-top:18px;">
      <button class="btn primary ar-go" id="ar-go" type="button">AI 릴스 만들기</button>
    </div>
    <p class="hint" id="ar-env"></p>
  </div>

  <div class="hero-panel" id="ar-progress" hidden>
    <h2 id="ar-title">진행 상황</h2>
    <ol class="ar-stages" id="ar-stages"></ol>
    <p class="hint ar-error" id="ar-error" hidden></p>
    <ul class="hint ar-warnings" id="ar-warnings"></ul>
  </div>

  <div class="hero-panel ar-confirm" id="ar-confirm" hidden>
    <h2>비용 확인</h2>
    <p id="ar-cost"></p>
    <label class="ar-check"><input id="ar-cost-ok" type="checkbox" /> 크레딧이 차감되는 것을 확인했습니다</label>
    <div class="btn-row" style="margin-top:14px;">
      <button class="btn primary" id="ar-confirm-go" type="button" disabled>생성 시작</button>
      <button class="btn ghost" id="ar-free-go" type="button">무료 모션으로 만들기</button>
    </div>
  </div>

  <div class="hero-panel" id="ar-board-panel" hidden>
    <h2>스토리보드</h2>
    <p class="muted" id="ar-board-meta"></p>
    <div class="ar-board" id="ar-board"></div>
  </div>

  <div class="hero-panel" id="ar-result" hidden>
    <h2>완성된 릴스</h2>
    <div class="ar-result-layout">
      <div class="asset-stage reel-mode" id="ar-video"></div>
      <div>
        <h3 style="margin-top:0;">캡션</h3>
        <p id="ar-caption" style="white-space:pre-wrap;"></p>
        <p class="muted" id="ar-tags"></p>
        <div class="btn-row" style="margin-top:16px;">
          <button class="btn primary" id="ar-approve" type="button">승인 · 업로드</button>
          <button class="btn danger" id="ar-reject" type="button">거절</button>
        </div>
        <p class="hint" id="ar-publish-status">승인 전에는 업로드되지 않습니다. 보관함에서도 다시 볼 수 있어요.</p>
      </div>
    </div>
  </div>
`;

function inject() {
  if ($("view-aireels")) return;
  const navHtml = `<button class="nav" data-view="aireels">AI 릴스</button>`;
  document.querySelectorAll(".side-nav, .mobile-nav").forEach((nav) => {
    const create = nav.querySelector('[data-view="create"]');
    if (create) create.insertAdjacentHTML("afterend", navHtml);
    else nav.insertAdjacentHTML("beforeend", navHtml);
  });

  const grid = document.querySelector("#view-home .choice-grid");
  if (grid) {
    grid.insertAdjacentHTML(
      "afterbegin",
      `<button class="choice-card ar-choice" id="btn-choose-aireels" type="button">
        <span class="choice-kicker">AI · PhotoCraft</span>
        <strong>AI 릴스 만들기</strong>
        <p>요청 한 줄로 기획 → 보정 → 영상 → 릴스까지. 승인 후 게시합니다.</p>
      </button>`
    );
  }

  const section = document.createElement("section");
  section.className = "view";
  section.id = "view-aireels";
  section.innerHTML = VIEW_HTML;
  const anchor = $("view-create");
  if (anchor) anchor.after(section);
  else document.querySelector("main")?.appendChild(section);
}

function goView() {
  const btn = document.querySelector('.side-nav [data-view="aireels"]') || document.querySelector('[data-view="aireels"]');
  btn?.click();
}

function onShown() {
  // app.js setView() titles unknown views by id; fix the title after it runs.
  setTimeout(() => {
    if (!$("view-aireels")?.classList.contains("on")) return;
    const t = $("page-title");
    const e = $("eyebrow");
    if (t) t.textContent = "AI 릴스";
    if (e) e.textContent = "PhotoCraft · Kling";
  }, 0);
  loadStatus();
  const last = recall();
  if (!ar.project && last) loadProject(last, { quiet: true });
}

async function loadStatus() {
  try {
    const s = await api("/api/aireels/status");
    ar.status = s;
    const sel = $("ar-music-name");
    if (sel && sel.options.length <= 1) {
      (s.music || []).forEach((name) => {
        const o = document.createElement("option");
        o.value = name;
        o.textContent = name;
        sel.appendChild(o);
      });
    }
    if (!s.has_claude) $("ar-offline").checked = true;
    const pc = s.photocraft || {};
    $("ar-env").textContent = [
      pc.available ? `PhotoCraft ${pc.version || ""}`.trim() : "PhotoCraft 미설치 → Pillow로 대체 (npm run photocraft:install)",
      s.has_claude ? "Claude 기획" : "오프라인 기획",
    ].join(" · ");
  } catch (err) {
    $("ar-env").textContent = `상태 확인 실패: ${err.message}`;
  }
}

function setMode(mode) {
  ar.mode = mode;
  document.querySelectorAll("#view-aireels .ar-mode").forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
}

function renderThumbs() {
  const box = $("ar-thumbs");
  box.innerHTML = "";
  ar.files.forEach((f) => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.alt = f.name;
    box.appendChild(img);
  });
  $("ar-photo-count").textContent = ar.files.length ? `${ar.files.length}장 선택됨` : "사진 없이도 만들 수 있어요";
}

async function start() {
  if (ar.busy) return;
  const request = $("ar-request").value.trim();
  if (!request) {
    toast("만들고 싶은 릴스를 적어 주세요");
    return;
  }
  ar.busy = true;
  $("ar-go").disabled = true;
  const fd = new FormData();
  fd.append("request", request);
  fd.append("mode", ar.mode);
  const secs = $("ar-seconds").value;
  if (secs) fd.append("seconds", secs);
  fd.append("offline", $("ar-offline").checked ? "true" : "false");
  ar.files.forEach((f) => fd.append("photos", f, f.name));
  const musicFile = $("ar-music-file").files?.[0];
  if (musicFile) fd.append("music", musicFile, musicFile.name);
  else if ($("ar-music-name").value) fd.append("music_name", $("ar-music-name").value);
  try {
    const p = await api("/api/aireels", { method: "POST", body: fd });
    remember(p.job_id);
    render(p);
    poll(p.job_id);
  } catch (err) {
    toast(err.message);
  } finally {
    ar.busy = false;
    $("ar-go").disabled = false;
  }
}

async function loadProject(id, { quiet = false } = {}) {
  try {
    const p = await api(`/api/aireels/${encodeURIComponent(id)}`);
    render(p);
    if (isActive(p)) poll(p.job_id);
  } catch (err) {
    if (!quiet) toast(err.message);
  }
}

function isActive(p) {
  return p.running || p.status === "planning" || p.status === "rendering";
}

function poll(id) {
  clearTimeout(ar.pollTimer);
  ar.pollTimer = setTimeout(async () => {
    try {
      const p = await api(`/api/aireels/${encodeURIComponent(id)}`);
      render(p);
      if (isActive(p)) poll(id);
    } catch {
      poll(id);
    }
  }, 1500);
}

function render(p) {
  ar.project = p;
  $("ar-progress").hidden = false;
  $("ar-title").textContent = p.storyboard?.topic ? `진행 상황 · ${p.storyboard.topic}` : "진행 상황";
  $("ar-stages").innerHTML = STAGES.map(([key, label]) => {
    const st = p.stages?.[key] || {};
    const status = st.status || "pending";
    const pct = st.total ? ` (${st.done || 0}/${st.total})` : "";
    return `<li class="ar-stage ${esc(status)}">
      <span class="ar-dot" aria-hidden="true"></span>
      <strong>${esc(label)}</strong>
      <span class="ar-chip">${esc(STATUS_LABEL[status] || status)}${esc(pct)}</span>
      <span class="muted">${esc(st.detail || "")}</span>
    </li>`;
  }).join("");
  const errEl = $("ar-error");
  errEl.hidden = !p.error;
  errEl.textContent = p.error || "";
  $("ar-warnings").innerHTML = (p.warnings || []).filter((w) => !w.includes("Traceback")).map((w) => `<li>${esc(w)}</li>`).join("");

  renderBoard(p);
  renderConfirm(p);
  renderResult(p);
}

function renderBoard(p) {
  const sb = p.storyboard;
  $("ar-board-panel").hidden = !sb;
  if (!sb) return;
  const looks = Object.fromEntries((ar.status?.looks || []).map((l) => [l.name, l.label]));
  const total = (sb.shots || []).reduce((a, s) => a + Number(s.duration || 0), 0);
  $("ar-board-meta").textContent = `${sb.shots.length}컷 · ${total.toFixed(1)}초 · ${p.mode === "kling" ? "AI 영상 (Kling)" : "무료 모션"}`;
  $("ar-board").innerHTML = sb.shots
    .map((s, i) => {
      const rec = (p.shots || [])[i] || {};
      const src = s.source === "photo" ? `사진 ${Number(s.photo_index) + 1}` : s.source === "generate" ? "AI 이미지" : "배경";
      const img = rec.preview ? `<img src="${media(rec.preview)}?v=${encodeURIComponent(rec.still || "p")}" alt="컷 ${i + 1}" loading="lazy" />` : `<div class="ar-ph">준비 중</div>`;
      const clip = rec.clip ? "클립 완료" : rec.error ? "실패" : "";
      return `<article class="ar-shot ${rec.error ? "failed" : ""}">
        <div class="ar-shot-img">${img}<span class="ar-num">${i + 1}</span></div>
        <div class="ar-shot-body">
          <strong>${esc(s.overlay_text || "(자막 없음)")}</strong>
          ${s.sub_text ? `<span>${esc(s.sub_text)}</span>` : ""}
          <span class="muted">${esc(src)} · ${esc(looks[s.look] || s.look)} · ${Number(s.duration).toFixed(1)}초</span>
          <span class="muted ar-motion">${esc(p.mode === "kling" ? s.motion_prompt : s.camera)}</span>
          ${clip ? `<span class="ar-chip ${rec.error ? "bad" : ""}">${esc(clip)}</span>` : ""}
          ${rec.error ? `<span class="ar-err">${esc(rec.error)}</span>` : ""}
        </div>
      </article>`;
    })
    .join("");
}

function renderConfirm(p) {
  const est = p.estimate || {};
  const need = !p.running && (p.status === "awaiting_confirm" || (p.status === "error" && p.mode === "kling" && est.total_jobs > 0));
  $("ar-confirm").hidden = !need;
  if (!need) return;
  $("ar-cost").innerHTML = `Kling 유료 작업 <strong>${esc(est.total_jobs)}건</strong> (이미지 ${esc(est.image_jobs)} · 영상 ${esc(est.video_jobs)})을 실행합니다. 실패한 작업은 자동으로 다시 시도하지 않습니다.`;
  $("ar-confirm-go").textContent = `${est.total_jobs}건 생성 시작`;
  $("ar-cost-ok").checked = false;
  $("ar-confirm-go").disabled = true;
}

function renderResult(p) {
  const ready = p.status === "ready" && p.reel_path;
  $("ar-result").hidden = !ready;
  if (!ready) return;
  const stage = $("ar-video");
  const src = media(p.reel_path);
  if (stage.dataset.src !== src) {
    stage.dataset.src = src;
    stage.innerHTML = `<video src="${src}" controls playsinline></video>`;
  }
  const sb = p.storyboard || {};
  $("ar-caption").textContent = sb.caption || "";
  $("ar-tags").textContent = (sb.hashtags || []).map((t) => `#${t}`).join(" ");
  const jobStatus = p.job?.status || "ready";
  $("ar-approve").disabled = jobStatus !== "ready";
  $("ar-reject").disabled = jobStatus !== "ready";
}

async function confirmRender(mode) {
  const p = ar.project;
  if (!p || ar.busy) return;
  ar.busy = true;
  const fd = new FormData();
  fd.append("confirm_jobs", String(mode === "free" ? 0 : p.estimate?.total_jobs || 0));
  if (mode) fd.append("mode", mode);
  try {
    const next = await api(`/api/aireels/${encodeURIComponent(p.job_id)}/render`, { method: "POST", body: fd });
    render(next);
    poll(next.job_id);
  } catch (err) {
    toast(err.message);
  } finally {
    ar.busy = false;
  }
}

async function decide(action) {
  const p = ar.project;
  if (!p || ar.busy) return;
  if (action === "approve" && !confirm("인스타그램에 이 릴스를 게시할까요?")) return;
  ar.busy = true;
  $("ar-publish-status").textContent = action === "approve" ? "업로드 중…" : "처리 중…";
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(p.job_id)}/${action}`, { method: "POST" });
    p.job = job;
    $("ar-publish-status").textContent =
      job.status === "published" ? `게시 완료: ${(job.media_ids || []).join(", ")}` : "거절됨 — 로컬에만 보관";
    renderResult(p);
  } catch (err) {
    $("ar-publish-status").textContent = err.message;
    toast(err.message);
  } finally {
    ar.busy = false;
  }
}

function bind() {
  document.querySelectorAll('[data-view="aireels"]').forEach((b) => b.addEventListener("click", onShown));
  $("btn-choose-aireels")?.addEventListener("click", goView);

  const drop = $("ar-drop");
  const input = $("ar-photos");
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    ar.files = [...input.files];
    renderThumbs();
  });
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("drag");
    })
  );
  drop.addEventListener("drop", (e) => {
    ar.files = [...(e.dataTransfer?.files || [])].filter((f) => f.type.startsWith("image/"));
    renderThumbs();
  });

  document.querySelectorAll("#view-aireels .ar-mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("ar-go").addEventListener("click", start);
  $("ar-cost-ok").addEventListener("change", (e) => {
    $("ar-confirm-go").disabled = !e.target.checked;
  });
  $("ar-confirm-go").addEventListener("click", () => confirmRender(""));
  $("ar-free-go").addEventListener("click", () => confirmRender("free"));
  $("ar-approve").addEventListener("click", () => decide("approve"));
  $("ar-reject").addEventListener("click", () => decide("reject"));
}

export function mountAiReels() {
  inject();
  bind();
}
