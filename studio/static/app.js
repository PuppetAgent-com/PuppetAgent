/**
 * PuppetAgent Studio — Frontend
 *
 * Vanilla JS, no framework.
 * Polls /api/jobs every 8s; uses SSE for running-job progress.
 */

// ── State ─────────────────────────────────────────────────────────────────────

let allJobs     = [];
let activeFilter = "all";
let expandedId   = null;
let pendingRating = {};  // {job_id: number}
let sseMap       = {};   // {job_id: EventSource}
let lastProgress = {};   // {job_id: {pct, step, msg}}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  loadJobs();
  setInterval(loadJobs, 8000);

  // Filter tabs
  document.querySelectorAll(".filter-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".filter-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      activeFilter = tab.dataset.status;
      renderJobs();
    });
  });

  document.getElementById("btn-refresh").addEventListener("click", loadJobs);
  document.getElementById("btn-upload").addEventListener("click", openUploadModal);
  setupUploadModal();
});

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadJobs() {
  try {
    const url = "/api/jobs?limit=100";
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    allJobs = await resp.json();
    renderJobs();
  } catch (e) {
    console.error("loadJobs failed:", e);
  }
}

// ── Render ────────────────────────────────────────────────────────────────────

function filterJobs() {
  if (activeFilter === "all") return allJobs;
  return allJobs.filter(j => j.status === activeFilter);
}

function renderJobs() {
  const container = document.getElementById("job-list");
  const jobs = filterJobs();

  if (jobs.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>No jobs yet.</p></div>';
    return;
  }

  // Preserve expanded state and scroll position
  const scrollY = window.scrollY;

  // Sync SSE connections for running jobs
  jobs.forEach(job => {
    const isRunning = job.status === "stage1_running" || job.status === "stage2_running";
    if (isRunning && !sseMap[job.job_id]) {
      startSSE(job.job_id);
    }
    if (!isRunning && sseMap[job.job_id]) {
      sseMap[job.job_id].close();
      delete sseMap[job.job_id];
    }
  });

  container.innerHTML = "";
  jobs.forEach(job => {
    container.appendChild(buildCard(job));
  });

  window.scrollTo(0, scrollY);
}

// ── SSE ───────────────────────────────────────────────────────────────────────

function startSSE(jobId) {
  const es = new EventSource(`/api/jobs/${jobId}/progress`);
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      lastProgress[jobId] = data;
      updateCardProgress(jobId, data);
    } catch {}
  };
  es.onerror = () => {
    es.close();
    delete sseMap[jobId];
  };
  sseMap[jobId] = es;
}

function updateCardProgress(jobId, data) {
  // Update collapsed progress bar
  const bar = document.querySelector(`.job-card[data-id="${jobId}"] .card-progress-bar`);
  const lbl = document.querySelector(`.job-card[data-id="${jobId}"] .card-progress-label`);
  if (bar) bar.style.width = `${data.pct || 0}%`;
  if (lbl) lbl.textContent = `${data.step || ""} — ${data.pct || 0}%`;

  // Update expanded progress
  const expBar = document.querySelector(`#exp-${jobId} .progress-bar-fill`);
  const expMsg = document.querySelector(`#exp-${jobId} .progress-msg`);
  if (expBar) expBar.style.width = `${data.pct || 0}%`;
  if (expMsg) expMsg.textContent = `${data.step || ""}: ${data.msg || ""}`;

  // Update step chips
  updateStepChips(jobId, data);
}

function updateStepChips(jobId, data) {
  const container = document.querySelector(`#steps-${jobId}`);
  if (!container) return;
  const step = data.step;
  const stage = data.stage || 1;
  const STAGE1_ORDER = ["auto_edit", "voice_enhance", "face_swap", "overlay"];
  const STAGE2_ORDER = ["lip_sync", "gaze_correct", "bg_replace", "amix"];
  const order = stage === 2 ? STAGE2_ORDER : STAGE1_ORDER;

  const chips = container.querySelectorAll(".step-chip");
  chips.forEach(chip => {
    const cs = chip.dataset.step;
    const ci = order.indexOf(cs);
    const si = order.indexOf(step);
    chip.classList.remove("done", "active");
    if (ci < si) chip.classList.add("done");
    else if (ci === si) chip.classList.add("active");
  });
}

// ── Card builder ──────────────────────────────────────────────────────────────

function buildCard(job) {
  const isExpanded = expandedId === job.job_id;
  const isRunning  = job.status === "stage1_running" || job.status === "stage2_running";
  const prog       = lastProgress[job.job_id] || {};

  const card = document.createElement("div");
  card.className = `job-card${isExpanded ? " expanded" : ""}`;
  card.dataset.id = job.job_id;

  // ── Header ──
  const header = document.createElement("div");
  header.className = "card-header";
  header.innerHTML = `
    <span class="status-badge ${job.status}">${fmtStatus(job.status)}</span>
    <div class="card-title">
      <span class="influencer">${cap(job.influencer)}</span>
      <span class="title-text">${esc(job.title || "untitled")}</span>
      <span class="job-id">${job.job_id}</span>
    </div>
    <span class="card-time">${fmtDate(job.created_at)}</span>
    <span class="card-chevron">▾</span>
  `;
  header.addEventListener("click", () => toggleExpand(job.job_id));
  card.appendChild(header);

  // ── Collapsed progress (running only) ──
  if (isRunning) {
    const pct = prog.pct || 0;
    const wrap = document.createElement("div");
    wrap.innerHTML = `
      <div class="card-progress">
        <div class="card-progress-bar" style="width:${pct}%"></div>
      </div>
      <div class="card-progress-label">${prog.step || job.status} — ${pct}%</div>
    `;
    card.appendChild(wrap);
  }

  // ── Expanded body ──
  if (isExpanded) {
    card.appendChild(buildCardBody(job, prog));
  }

  return card;
}

function buildCardBody(job, prog) {
  const body = document.createElement("div");
  body.className = "card-body";
  body.id = `exp-${job.job_id}`;

  // Error message
  if (job.error_msg) {
    body.innerHTML += `<div class="error-msg">${esc(job.error_msg)}</div>`;
  }

  // Three video players
  body.appendChild(buildVideoGrid(job));

  // Progress bar (running jobs)
  const isRunning = job.status === "stage1_running" || job.status === "stage2_running";
  if (isRunning) {
    const pct = prog.pct || 0;
    body.innerHTML += `
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" style="width:${pct}%"></div>
      </div>
      <div class="progress-msg">${prog.step || ""}: ${prog.msg || "Processing…"}</div>
    `;
  }

  // Step chips
  body.appendChild(buildStepChips(job, prog));

  // Options row
  body.appendChild(buildOptions(job));

  // Star rating (show for awaiting_review; show read-only for done)
  if (job.status === "awaiting_review" || job.status === "done" || job.status === "stage2_running") {
    body.appendChild(buildRating(job));
  }

  // Action buttons
  body.appendChild(buildActions(job));

  // Log viewer
  const logBtn = document.createElement("button");
  logBtn.className = "log-toggle";
  logBtn.textContent = "▸ Show logs";
  const logDiv = document.createElement("div");
  logDiv.className = "log-viewer";
  logDiv.style.display = "none";
  logBtn.addEventListener("click", async () => {
    if (logDiv.style.display === "none") {
      logDiv.style.display = "block";
      logBtn.textContent = "▾ Hide logs";
      await loadLogs(job.job_id, logDiv);
    } else {
      logDiv.style.display = "none";
      logBtn.textContent = "▸ Show logs";
    }
  });
  body.appendChild(logBtn);
  body.appendChild(logDiv);

  return body;
}

function buildVideoGrid(job) {
  const grid = document.createElement("div");
  grid.className = "video-grid";

  const panels = [
    { label: "RAW",     url: job.raw_url     },
    { label: "PREVIEW", url: job.preview_url },
    { label: "FINAL",   url: job.final_url   },
  ];

  panels.forEach(({ label, url }) => {
    const panel = document.createElement("div");
    panel.className = "video-panel";
    if (url) {
      panel.innerHTML = `
        <h3>${label}</h3>
        <div class="video-wrap">
          <video controls preload="metadata" src="${url}"></video>
        </div>
      `;
    } else {
      panel.innerHTML = `
        <h3>${label}</h3>
        <div class="video-wrap">
          <div class="video-placeholder">Not yet available</div>
        </div>
      `;
    }
    grid.appendChild(panel);
  });

  return grid;
}

function buildStepChips(job, prog) {
  const section = document.createElement("div");
  section.className = "steps-section";
  section.innerHTML = "<h3>Pipeline steps</h3>";

  const chips = document.createElement("div");
  chips.className = "steps-grid";
  chips.id = `steps-${job.job_id}`;

  const STAGE1_STEPS = [
    { id: "auto_edit",     label: "auto-edit" },
    { id: "voice_enhance", label: "voice-enhance" },
    { id: "face_swap",     label: "face-swap" },
    { id: "overlay",       label: "overlay" },
  ];
  const STAGE2_STEPS = [
    { id: "lip_sync",     label: "lip-sync", s2: true },
    { id: "gaze_correct", label: "gaze",     s2: true },
    { id: "bg_replace",   label: "bg",       s2: true },
    { id: "amix",         label: "amix",     s2: true },
  ];

  const allSteps = [...STAGE1_STEPS, ...STAGE2_STEPS];
  const curStep  = prog.step || "";
  const curStage = prog.stage || 1;
  const S1ORDER  = STAGE1_STEPS.map(s => s.id);
  const S2ORDER  = STAGE2_STEPS.map(s => s.id);

  allSteps.forEach(({ id, label, s2 }) => {
    const chip = document.createElement("span");
    chip.className = `step-chip${s2 ? " s2" : ""}`;
    chip.dataset.step = id;
    const order = s2 ? S2ORDER : S1ORDER;
    const ci = order.indexOf(id);
    const si = order.indexOf(curStep);

    if ((s2 && curStage === 2 && ci < si) ||
        (!s2 && curStage === 1 && ci < si) ||
        (s2 && curStage > 2) ||
        (!s2 && job.status !== "uploaded" && curStage >= 1 && (curStage === 2 || ci < si))) {
      chip.classList.add("done");
    } else if (id === curStep) {
      chip.classList.add("active");
    }

    const icon = chip.classList.contains("done") ? "✅"
               : chip.classList.contains("active") ? "🕐"
               : "○";
    chip.textContent = `${icon} ${label}`;
    chips.appendChild(chip);
  });

  section.appendChild(chips);
  return section;
}

function buildOptions(job) {
  const row = document.createElement("div");
  row.className = "options-row";

  const opts = [
    { key: "opt_auto_edit",     label: "auto-edit" },
    { key: "opt_voice_enhance", label: "voice-enhance" },
    { key: "opt_face_swap",     label: "face-swap" },
    { key: "opt_lip_sync",      label: "lip-sync (S2)" },
    { key: "opt_gaze_correct",  label: "gaze-correct (S2)" },
    { key: "opt_bg_replace",    label: "bg-replace (S2)" },
  ];

  opts.forEach(({ key, label }) => {
    const lbl = document.createElement("label");
    lbl.className = "option-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!job[key];
    cb.addEventListener("change", async () => {
      await patchOptions(job.job_id, { [key]: cb.checked ? 1 : 0 });
    });
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(label));
    row.appendChild(lbl);
  });

  return row;
}

function buildRating(job) {
  const section = document.createElement("div");
  section.className = "rating-section";

  const rating = pendingRating[job.job_id] !== undefined
    ? pendingRating[job.job_id]
    : (job.rating || 0);

  section.innerHTML = `
    <h3>Review</h3>
    <div class="stars" id="stars-${job.job_id}">
      ${[1,2,3,4,5].map(n =>
        `<span class="star${n <= rating ? " filled" : ""}" data-n="${n}">★</span>`
      ).join("")}
    </div>
    <textarea class="feedback-area" id="comment-${job.job_id}"
      placeholder="Optional feedback…">${esc(job.comment || "")}</textarea>
  `;

  // Star click
  section.querySelectorAll(".star").forEach(star => {
    star.addEventListener("click", () => {
      const n = parseInt(star.dataset.n);
      pendingRating[job.job_id] = n;
      section.querySelectorAll(".star").forEach(s => {
        s.classList.toggle("filled", parseInt(s.dataset.n) <= n);
      });
    });
  });

  return section;
}

function buildActions(job) {
  const row = document.createElement("div");
  row.className = "actions-row";

  const isRunning = job.status === "stage1_running" || job.status === "stage2_running";

  // Re-run Stage 1
  if (!isRunning && job.raw_key) {
    const btn = makeBtn("▶ Run S1", "btn-secondary btn-sm");
    btn.addEventListener("click", () => runStage(job.job_id, 1));
    row.appendChild(btn);
  }

  // Run Stage 2
  if (job.status === "awaiting_review" && !isRunning) {
    const btn = makeBtn("▶ Run S2", "btn-primary btn-sm");
    btn.addEventListener("click", () => runStage(job.job_id, 2));
    row.appendChild(btn);
  }

  // Submit review (stars)
  if (job.status === "awaiting_review") {
    const btn = makeBtn("★ Submit Review", "btn-primary btn-sm");
    btn.addEventListener("click", () => submitReview(job.job_id));
    row.appendChild(btn);
  }

  // Reject
  if (job.status === "awaiting_review" || job.status === "uploaded") {
    const btn = makeBtn("✗ Reject", "btn-danger btn-sm");
    btn.addEventListener("click", () => rejectJob(job.job_id));
    row.appendChild(btn);
  }

  // Final download link
  if (job.final_url) {
    const a = document.createElement("a");
    a.href = job.final_url;
    a.target = "_blank";
    a.className = "btn btn-secondary btn-sm";
    a.textContent = "⬇ Final";
    row.appendChild(a);
  }

  // Drive link
  if (job.drive_link) {
    const a = document.createElement("a");
    a.href = job.drive_link;
    a.target = "_blank";
    a.className = "btn btn-secondary btn-sm";
    a.textContent = "☁ Drive";
    row.appendChild(a);
  }

  return row;
}

// ── Actions ───────────────────────────────────────────────────────────────────

function toggleExpand(jobId) {
  expandedId = expandedId === jobId ? null : jobId;
  renderJobs();
}

async function runStage(jobId, stage) {
  const endpoint = stage === 1
    ? `/api/jobs/${jobId}/process`
    : `/api/jobs/${jobId}/process2`;
  try {
    const r = await fetch(endpoint, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    toast("Stage started", "success");
    await loadJobs();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function submitReview(jobId) {
  const rating  = pendingRating[jobId] || 0;
  const comment = document.getElementById(`comment-${jobId}`)?.value || "";
  if (!rating) { toast("Select a star rating first", "error"); return; }

  try {
    const r = await fetch(`/api/jobs/${jobId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, comment }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    toast(`Review submitted: ${rating}/5 — Stage 2 starting`, "success");
    delete pendingRating[jobId];
    await loadJobs();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function rejectJob(jobId) {
  if (!confirm("Reject this job?")) return;
  try {
    const r = await fetch(`/api/jobs/${jobId}/reject`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).error);
    toast("Job rejected", "success");
    expandedId = null;
    await loadJobs();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
  }
}

async function patchOptions(jobId, opts) {
  try {
    await fetch(`/api/jobs/${jobId}/options`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    });
  } catch (e) {
    console.error("patchOptions failed:", e);
  }
}

async function loadLogs(jobId, container) {
  try {
    const r = await fetch(`/api/jobs/${jobId}/logs`);
    const logs = await r.json();
    container.innerHTML = logs.map(l =>
      `<div><span class="log-ts">${l.ts ? l.ts.slice(11,19) : ""}</span>${esc(l.line)}</div>`
    ).join("");
    container.scrollTop = container.scrollHeight;
  } catch (e) {
    container.textContent = "Failed to load logs";
  }
}

// ── Upload modal ──────────────────────────────────────────────────────────────

function openUploadModal() {
  document.getElementById("upload-modal").classList.remove("hidden");
}

function closeUploadModal() {
  document.getElementById("upload-modal").classList.add("hidden");
  document.getElementById("upload-progress").classList.add("hidden");
  document.getElementById("upload-bar").style.width = "0%";
  document.getElementById("file-input").value = "";
  document.getElementById("up-title").value = "";
  selectedFile = null;
  document.getElementById("drop-zone").classList.remove("has-file");
  document.querySelector("#drop-zone p").textContent = "Drop video here or click to browse";
  document.getElementById("btn-confirm-upload").disabled = true;
}

let selectedFile = null;

function setupUploadModal() {
  document.getElementById("btn-cancel-upload").addEventListener("click", closeUploadModal);
  document.getElementById("upload-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("upload-modal")) closeUploadModal();
  });

  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");

  dropZone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) setFile(fileInput.files[0]);
  });

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });

  document.getElementById("btn-confirm-upload").addEventListener("click", doUpload);
}

function setFile(file) {
  selectedFile = file;
  const mb = (file.size / 1024 / 1024).toFixed(1);
  document.querySelector("#drop-zone p").textContent = `${file.name} (${mb} MB)`;
  document.getElementById("drop-zone").classList.add("has-file");
  document.getElementById("btn-confirm-upload").disabled = false;
}

async function doUpload() {
  if (!selectedFile) return;

  const influencer = document.getElementById("up-influencer").value;
  const title      = document.getElementById("up-title").value;
  const autoStart  = document.getElementById("up-autostart").checked;

  const form = new FormData();
  form.append("video", selectedFile);
  form.append("influencer", influencer);
  form.append("title", title);
  form.append("auto_start", autoStart ? "1" : "0");

  document.getElementById("upload-progress").classList.remove("hidden");
  document.getElementById("btn-confirm-upload").disabled = true;

  // XHR for upload progress
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");

  xhr.upload.addEventListener("progress", (e) => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      document.getElementById("upload-bar").style.width = `${pct}%`;
      document.getElementById("upload-status").textContent = `Uploading… ${pct}%`;
    }
  });

  xhr.onload = async () => {
    if (xhr.status === 201) {
      document.getElementById("upload-status").textContent = "Done! Processing…";
      toast("Video uploaded successfully", "success");
      closeUploadModal();
      await loadJobs();
    } else {
      let msg = "Upload failed";
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch {}
      toast(`Upload error: ${msg}`, "error");
      document.getElementById("btn-confirm-upload").disabled = false;
    }
  };

  xhr.onerror = () => {
    toast("Network error during upload", "error");
    document.getElementById("btn-confirm-upload").disabled = false;
  };

  xhr.send(form);
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function makeBtn(text, cls) {
  const btn = document.createElement("button");
  btn.className = `btn ${cls}`;
  btn.textContent = text;
  return btn;
}

function toast(msg, type = "success") {
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : ""; }

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtStatus(s) {
  return {
    uploaded:        "Uploaded",
    stage1_running:  "S1 Running",
    stage2_running:  "S2 Running",
    awaiting_review: "Awaiting Review",
    done:            "Done",
    error:           "Error",
    rejected:        "Rejected",
  }[s] || s;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "Z");
  const now = new Date();
  const diff = now - d;
  if (diff < 60000) return "just now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return d.toLocaleDateString();
}
