const $ = (id) => document.getElementById(id);

const TITLES = {
  home: "시작",
  create: "만들기",
  preview: "미리보기",
  library: "보관함",
  settings: "설정",
  monetize: "수익화",
};

const EYEBROWS = {
  home: "만들기",
  create: "Instagram",
  preview: "승인",
  library: "기록",
  settings: "환경설정",
  monetize: "제휴 · 댓글 DM",
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
  window.dispatchEvent(new CustomEvent("studio:job", { detail: state.job })); // 수익화 패널(monetize.js)
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
  $("preview-meta").textContent = `${state.job.job_id} · ${state.job.status} · ${state.job.fmt}`;
  $("preview-caption").textContent = state.job.caption || "";
  $("preview-tags").textContent = (state.job.hashtags || [])
    .map((t) => `#${String(t).replace(/^#/, "")}`)
    .join(" ");

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

  const canPublish = state.job.status === "ready";
  $("btn-approve").disabled = !canPublish || state.busy;
  $("btn-reject").disabled = !canPublish || state.busy;
  $("preview-status").textContent =
    state.job.status === "published"
      ? `게시됨: ${(state.job.media_ids || []).join(", ")}`
      : state.job.status === "rejected"
        ? "거절됨 — 로컬에만 보관"
        : "승인 전에는 업로드되지 않습니다.";
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
      const chipClass =
        job.status === "published"
          ? "published"
          : job.status === "rejected"
            ? "rejected"
            : "";
      btn.innerHTML = `
        <strong>${job.topic || job.job_id}</strong>
        <span class="muted">${job.job_id} · ${job.fmt}</span>
        <span class="chip ${chipClass}">${job.status}</span>
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
    ];
    $("settings-status").innerHTML = lines.map((l) => `<li>${l}</li>`).join("");
    $("setup-bar").hidden = !!(s.has_claude || s.has_ig);
    if (!s.has_claude) {
      $("input-offline").checked = true;
    }
  } catch (err) {
    $("settings-status").innerHTML = `<li>API 연결 실패: ${err.message}</li>`;
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

async function approve() {
  if (!state.job || state.busy) return;
  state.busy = true;
  $("preview-status").textContent = "업로드 중…";
  try {
    state.job = await api(`/api/jobs/${state.job.job_id}/approve`, { method: "POST" });
    toast("게시 완료");
    renderPreview();
  } catch (err) {
    toast(err.message);
    $("preview-status").textContent = err.message;
  } finally {
    state.busy = false;
    renderPreview();
  }
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
  $("btn-reject")?.addEventListener("click", reject);
  $("btn-refresh-library")?.addEventListener("click", refreshLibrary);
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
  bindInstall();
  setFmt("photo");
  setView("home");
  refreshSettings();
}
