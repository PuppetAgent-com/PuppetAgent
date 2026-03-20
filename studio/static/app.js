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
let srtCache     = {};   // {srt_key: [{start, end, text}]}
let _activeDrag  = null; // active caption drag state
let _tlDrag      = null; // active timeline handle drag state

// ── Module-level caption drag handlers ────────────────────────────────────────

document.addEventListener("mousemove", (e) => {
  if (!_activeDrag) return;
  const { overlay, container } = _activeDrag;
  const rect = container.getBoundingClientRect();
  const dx = (e.clientX - _activeDrag.startX) / rect.width * 100;
  const dy = (e.clientY - _activeDrag.startY) / rect.height * 100;
  overlay.style.left = `${Math.max(5, Math.min(95, _activeDrag.startLeft + dx))}%`;
  overlay.style.top  = `${Math.max(5, Math.min(95, _activeDrag.startTop  + dy))}%`;
});

document.addEventListener("mouseup", () => {
  if (!_activeDrag) return;
  const { overlay, job } = _activeDrag;
  overlay.classList.remove("dragging");
  const nx = Math.round(parseFloat(overlay.style.left));
  const ny = Math.round(parseFloat(overlay.style.top));
  job.caption_x = String(nx);
  job.caption_y = String(ny);
  patchOptions(job.job_id, { caption_x: String(nx), caption_y: String(ny) });
  _activeDrag = null;
});

document.addEventListener("touchmove", (e) => {
  if (!_activeDrag) return;
  e.preventDefault();
  const touch = e.touches[0];
  const { overlay, container } = _activeDrag;
  const rect = container.getBoundingClientRect();
  const dx = (touch.clientX - _activeDrag.startX) / rect.width * 100;
  const dy = (touch.clientY - _activeDrag.startY) / rect.height * 100;
  overlay.style.left = `${Math.max(5, Math.min(95, _activeDrag.startLeft + dx))}%`;
  overlay.style.top  = `${Math.max(5, Math.min(95, _activeDrag.startTop  + dy))}%`;
}, { passive: false });

document.addEventListener("touchend", () => {
  if (!_activeDrag) return;
  const { overlay, job } = _activeDrag;
  overlay.classList.remove("dragging");
  const nx = Math.round(parseFloat(overlay.style.left));
  const ny = Math.round(parseFloat(overlay.style.top));
  job.caption_x = String(nx);
  job.caption_y = String(ny);
  patchOptions(job.job_id, { caption_x: String(nx), caption_y: String(ny) });
  _activeDrag = null;
});

// ── Module-level timeline handle drag handlers ────────────────────────────────

document.addEventListener("mousemove", (e) => {
  if (!_tlDrag) return;
  const { wrap, state, idx, side, redraw } = _tlDrag;
  const rect = wrap.getBoundingClientRect();
  const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const t    = pct * state.duration;
  const ivs  = state.intervals;
  if (side === "left") {
    ivs[idx][0] = Math.max(idx > 0 ? ivs[idx-1][1] + 0.05 : 0,
                           Math.min(t, ivs[idx][1] - 0.5));
  } else {
    ivs[idx][1] = Math.min(idx < ivs.length-1 ? ivs[idx+1][0] - 0.05 : state.duration,
                           Math.max(t, ivs[idx][0] + 0.5));
  }
  redraw();
});

document.addEventListener("mouseup", () => {
  if (!_tlDrag) return;
  _tlDrag.handle.classList.remove("dragging");
  _tlDrag = null;
});

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
  document.getElementById("btn-create").addEventListener("click", openCreateModal);
  setupUploadModal();
  setupCreateModal();

  // Delete modal
  document.getElementById("btn-cancel-delete").addEventListener("click", () => {
    _deleteTargetId = null;
    document.getElementById("delete-modal").classList.add("hidden");
  });
  document.getElementById("btn-confirm-delete").addEventListener("click", async () => {
    const jobId = _deleteTargetId;
    if (!jobId) return;
    document.getElementById("delete-modal").classList.add("hidden");
    _deleteTargetId = null;
    try {
      const r = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
      if (!r.ok) throw new Error((await r.json()).error);
      toast("Job deleted", "success");
      if (expandedId === jobId) expandedId = null;
      await loadJobs();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, "error");
    }
  });
  document.getElementById("delete-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) {
      _deleteTargetId = null;
      e.currentTarget.classList.add("hidden");
    }
  });
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

  // Build a map of existing cards by job_id
  const existing = {};
  container.querySelectorAll(".job-card[data-id]").forEach(el => {
    existing[el.dataset.id] = el;
  });

  // Track which job_ids should be in the list (for removal of deleted jobs)
  const currentIds = new Set(jobs.map(j => j.job_id));

  // Remove cards for deleted jobs
  Object.keys(existing).forEach(id => {
    if (!currentIds.has(id)) existing[id].remove();
  });

  // Update or insert cards in order
  jobs.forEach((job, i) => {
    const isRunning = job.status === "stage1_running" || job.status === "stage2_running";
    const card = existing[job.job_id];
    const fingerprint = `${job.status}|${job.error_msg || ""}|${job.rating || ""}|${job.srt_key || ""}|${job.caption_style || ""}|${expandedId === job.job_id}`;
    const prevFingerprint = card ? card.dataset.fingerprint : null;

    if (card && !isRunning && fingerprint === prevFingerprint) {
      // Nothing changed — keep the existing card (and its live <video>) in place.
      // Just ensure it is in the right position.
      const expectedPrev = i === 0 ? null : container.children[i - 1];
      if (card !== container.children[i]) {
        container.insertBefore(card, container.children[i] || null);
      }
      return;
    }

    // Build a fresh card and insert it
    const newCard = buildCard(job);
    newCard.dataset.fingerprint = fingerprint;
    if (card) {
      container.replaceChild(newCard, card);
    } else {
      container.insertBefore(newCard, container.children[i] || null);
    }
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
  card.className = `job-card s-${job.status}${isExpanded ? " expanded" : ""}`;
  card.dataset.id = job.job_id;

  // ── Header ──
  const header = document.createElement("div");
  header.className = "card-header";
  header.innerHTML = `
    <span class="status-badge ${job.status}">${fmtStatus(job.status)}</span>
    <div class="card-title">
      <span class="card-title-line"><span class="influencer">${cap(job.influencer)}</span><span class="title-text">${esc(job.title || "untitled")}</span></span>
      <span class="job-id">${job.job_id}</span>
    </div>
    <span class="card-time">${fmtDate(job.created_at)}</span>
    <span class="card-chevron">▾</span>
  `;
  header.addEventListener("click", () => toggleExpand(job.job_id));
  if (!isRunning) {
    const forkHdr = document.createElement("button");
    forkHdr.type = "button";
    forkHdr.className = "btn-header-fork";
    forkHdr.title = "Fork — duplicate with same settings";
    forkHdr.textContent = "⧉";
    forkHdr.addEventListener("click", (e) => { e.stopPropagation(); forkJob(job.job_id); });
    header.insertBefore(forkHdr, header.querySelector(".card-chevron"));
  }
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

  // Cut timeline (shown when intervals_json is available from Stage 1 auto-edit)
  const tlSection = buildTimeline(job);
  if (tlSection) body.appendChild(tlSection);

  // Options row
  body.appendChild(buildOptions(job));

  // Captions section
  body.appendChild(buildCaptions(job));

  // Star rating (show for awaiting_review; show read-only for done)
  if (job.status === "awaiting_review" || job.status === "done" || job.status === "stage2_running") {
    body.appendChild(buildRating(job));
  }

  // Action buttons
  body.appendChild(buildActions(job));

  // Script viewer / editor
  body.appendChild(buildScriptSection(job));

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
    { label: "RAW",     url: job.raw_url,     cls: "panel-raw"     },
    { label: "PREVIEW", url: job.preview_url, cls: "panel-preview" },
    { label: "FINAL",   url: job.final_url,   cls: "panel-final"   },
  ];

  panels.forEach(({ label, url, cls }) => {
    const panel = document.createElement("div");
    panel.className = `video-panel ${cls}`;

    // PREVIEW panel gets caption overlay when SRT is available
    if (cls === "panel-preview" && url && job.srt_key) {
      const h3 = document.createElement("h3");
      h3.textContent = label;
      panel.appendChild(h3);

      const wrap = document.createElement("div");
      wrap.className = "video-wrap";
      wrap.style.position = "relative";
      wrap.style.overflow = "visible";

      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      video.setAttribute("playsinline", "");
      video.src = url;
      wrap.appendChild(video);

      const captionX = parseFloat(job.caption_x || 50);
      const captionY = parseFloat(job.caption_y || 85);
      const overlay = document.createElement("div");
      overlay.className = "caption-overlay";
      overlay.style.left = `${captionX}%`;
      overlay.style.top  = `${captionY}%`;
      const captionText = document.createElement("div");
      captionText.className = "caption-text";
      overlay.appendChild(captionText);
      wrap.appendChild(overlay);
      panel.appendChild(wrap);

      // Size control row
      const sizeRow = document.createElement("div");
      sizeRow.className = "caption-size-row";
      const curSize = job.caption_size || "medium";
      [["S", "small"], ["M", "medium"], ["L", "large"]].forEach(([lbl, sz]) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `opt-pill caption-size-btn${sz === curSize ? " active" : ""}`;
        btn.textContent = lbl;
        btn.title = sz;
        btn.addEventListener("click", () => {
          sizeRow.querySelectorAll(".caption-size-btn").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          job.caption_size = sz;
          patchOptions(job.job_id, { caption_size: sz });
        });
        sizeRow.appendChild(btn);
      });
      panel.appendChild(sizeRow);

      fetchAndSetupSRT(job.srt_key, job.job_id, video, captionText);
      setupCaptionDrag(overlay, wrap, job);

    } else if (url) {
      panel.innerHTML = `
        <h3>${label}</h3>
        <div class="video-wrap">
          <video controls preload="metadata" playsinline src="${url}"></video>
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

function makeHelpBtn(text) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "opt-help-btn";
  btn.textContent = "?";
  btn.setAttribute("aria-label", "Help");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    document.querySelectorAll(".opt-tooltip.visible").forEach(t => {
      if (t !== btn._tip) t.classList.remove("visible");
    });
    if (!btn._tip) {
      const tip = document.createElement("div");
      tip.className = "opt-tooltip";
      tip.textContent = text;
      btn.parentNode.appendChild(tip);
      btn._tip = tip;
    }
    btn._tip.classList.toggle("visible");
  });
  return btn;
}

// Close tooltips on outside click/tap
document.addEventListener("click", () => {
  document.querySelectorAll(".opt-tooltip.visible").forEach(t => t.classList.remove("visible"));
});

function buildOptions(job) {
  const panel = document.createElement("div");
  panel.className = "opts-panel";

  const GROUPS = [
    {
      label: "Video",
      items: [
        { type: "check",  key: "opt_auto_edit",    label: "auto-edit",    help: "AI trims silences, removes filler words, and cuts dead air from the raw video before processing." },
        { type: "check",  key: "opt_noise_reduce",  label: "noise-reduce", help: "Temporal video denoising (hqdn3d). Reduces grain/noise in the final output. Minimal processing cost." },
      ]
    },
    {
      label: "Voice",
      items: [
        { type: "check",  key: "opt_voice_enhance", label: "enhance", help: "Run DeepFilterNet audio enhancement before voice cloning \u2014 removes background noise and improves clarity." },
        { type: "select-dynamic", key: "opt_voice_conv", label: "voice",
          staticOpts: [["none","off"]], apiUrl: "/api/rvc-models",
          uploadUrl: "/api/rvc-models/upload",
          help: "RVC voice conversion model. Replaces cloned voice timbre with selected celebrity/character voice.",
          noModelsLabel: "no models" },
      ]
    },
    {
      label: "Face",
      items: [
        { type: "select", key: "opt_face_mode", label: "mode",
          help: "FaceFusion: swaps reference face onto video. LivePortrait: animates your portrait.jpg using the video as driver. Off: no face processing.",
          opts: [["facefusion","FaceFusion"],["liveportrait","LivePortrait"],["none","off"]] },
        { type: "select", key: "opt_face_model", label: "model",
          help: "Face swap model. Hyper 1A/1B/1C are newer and better; Inswapper is the classic. Ignored when mode is not FaceFusion.",
          opts: [["hyperswap_1a","Hyper 1A"],["hyperswap_1b","Hyper 1B"],["hyperswap_1c","Hyper 1C"],["inswapper_128_fp16","Inswapper"]] },
        { type: "select", key: "opt_face_enhancer", label: "enhance",
          help: "Post-process the swapped face. CodeFormer and GFPGan sharpen detail and reduce artifacts. Adds processing time.",
          opts: [["none","off"],["codeformer","CodeFormer"],["gfpgan","GFPGan"]] },
        { type: "select", key: "opt_face_mask_type", label: "mask",
          help: "Edge mask around the swapped face. Box: rectangular crop. Neural: AI-detected face outline \u2014 reduces aliasing at hair/face edges.",
          opts: [["box","Box"],["occlusion","Neural"]] },
        { type: "select", key: "opt_face_mask_blur", label: "blend",
          help: "Softness of the face edge blend. Higher = smoother transition, less aliasing, but slight glow at edges.",
          opts: [["0.2","Sharp"],["0.3","Normal"],["0.5","Soft"],["0.7","Very soft"]] },
      ]
    },
    {
      label: "Stage 2",
      items: [
        { type: "check",  key: "opt_lip_sync",     label: "lip-sync",  help: "MuseTalk lip-sync in Stage 2: re-renders mouth to match the cloned voice. Best quality but adds 30\u201360 min processing." },
        { type: "check",  key: "opt_gaze_correct", label: "gaze",      help: "Redirect eye gaze to look straight at the camera (Stage 2). Uses MediaPipe face landmarks." },
        { type: "check",  key: "opt_bg_replace",   label: "bg",        help: "Replace background in Stage 2." },
        { type: "check",  key: "opt_interpolate",  label: "60fps",     help: "RIFE frame interpolation doubles framerate to 60fps. Uses Vulkan \u2014 fast on AMD and NVIDIA." },
      ]
    },
  ];

  GROUPS.forEach(group => {
    const card = document.createElement("div");
    card.className = "section-card";

    const hdr = document.createElement("div");
    hdr.className = "section-header";
    const lbl = document.createElement("span");
    lbl.className = "section-label";
    lbl.textContent = group.label;
    hdr.appendChild(lbl);
    card.appendChild(hdr);

    const body = document.createElement("div");
    body.className = "section-body";

    group.items.forEach(item => {
      const row = document.createElement("div");
      row.className = "opt-row";

      const rowLbl = document.createElement("span");
      rowLbl.className = "opt-row-label";
      rowLbl.textContent = item.label;
      row.appendChild(rowLbl);

      const pillGroup = document.createElement("div");
      pillGroup.className = "opt-pill-group";

      if (item.type === "check") {
        let active = !!job[item.key];
        const makePill = (text, val) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "opt-pill" + (active === val ? " active" : "");
          btn.textContent = text;
          btn.addEventListener("click", () => {
            active = val;
            pillGroup.querySelectorAll(".opt-pill").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            patchOptions(job.job_id, { [item.key]: val ? 1 : 0 });
          });
          return btn;
        };
        pillGroup.appendChild(makePill("On", true));
        pillGroup.appendChild(makePill("Off", false));

      } else if (item.type === "select" || item.type === "select-dynamic") {
        const baseOpts = item.opts || item.staticOpts || [];
        let activePillVal = job[item.key] != null ? String(job[item.key]) : (baseOpts[0] || ["none"])[0];

        const renderPills = (allOpts) => {
          pillGroup.innerHTML = "";
          allOpts.forEach(([val, text]) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "opt-pill" + (activePillVal === val ? " active" : "");
            btn.dataset.val = val;
            btn.textContent = text;
            btn.addEventListener("click", () => {
              activePillVal = val;
              pillGroup.querySelectorAll(".opt-pill").forEach(b => b.classList.remove("active"));
              btn.classList.add("active");
              patchOptions(job.job_id, { [item.key]: val });
            });
            pillGroup.appendChild(btn);
          });
        };

        renderPills(baseOpts);
        if (item.type === "select-dynamic" && item.apiUrl) {
          fetch(item.apiUrl).then(r => r.json()).then(models => {
            const extras = models.map(m => [m, m]);
            const allOpts = [...baseOpts, ...extras];
            renderPills(allOpts);
          }).catch(() => {});
        }
      }

      row.appendChild(pillGroup);
      if (item.help) row.appendChild(makeHelpBtn(item.help));

      if (item.type === "select-dynamic" && item.uploadUrl) {
        const upBtn = document.createElement("button");
        upBtn.type = "button";
        upBtn.className = "opt-upload-btn";
        upBtn.title = "Upload custom .pth model";
        upBtn.textContent = "\uff0b";
        const fileIn = document.createElement("input");
        fileIn.type = "file"; fileIn.accept = ".pth"; fileIn.style.display = "none";
        fileIn.addEventListener("change", async () => {
          const file = fileIn.files[0]; if (!file) return;
          const name = (prompt("Model name:", file.name.replace(/\.pth$/i, "")) || "").trim();
          if (!name) return;
          const fd = new FormData(); fd.append("file", file); fd.append("name", name);
          upBtn.disabled = true; upBtn.textContent = "\u2026";
          try {
            const r = await fetch(item.uploadUrl, { method: "POST", body: fd });
            const data = await r.json();
            if (!r.ok) throw new Error(data.error);
            toast("Model '" + data.name + "' uploaded", "success");
            fetch(item.apiUrl).then(r2 => r2.json()).then(models => {
              const extras = models.map(m => [m, m]);
              const allOpts = [...(item.staticOpts || []), ...extras];
              pillGroup.innerHTML = "";
              allOpts.forEach(([val, text]) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "opt-pill" + (data.name === val ? " active" : "");
                btn.dataset.val = val;
                btn.textContent = text;
                btn.addEventListener("click", () => {
                  pillGroup.querySelectorAll(".opt-pill").forEach(b => b.classList.remove("active"));
                  btn.classList.add("active");
                  patchOptions(job.job_id, { [item.key]: val });
                });
                pillGroup.appendChild(btn);
              });
              patchOptions(job.job_id, { [item.key]: data.name });
            });
          } catch(e) { toast("Upload failed: " + e.message, "error"); }
          finally { upBtn.disabled = false; upBtn.textContent = "\uff0b"; fileIn.value = ""; }
        });
        upBtn.addEventListener("click", () => fileIn.click());
        row.appendChild(fileIn);
        row.appendChild(upBtn);
      }

      body.appendChild(row);
    });
    card.appendChild(body);
    panel.appendChild(card);
  });

  return panel;
}

function buildCaptions(job) {
  const section = document.createElement("div");
  section.className = "section-card";

  const header = document.createElement("div");
  header.className = "section-header";
  const lbl = document.createElement("span");
  lbl.className = "section-label";
  lbl.textContent = "Captions";
  header.appendChild(lbl);

  if (job.srt_key) {
    const badge = document.createElement("span");
    badge.className = "srt-badge";
    badge.textContent = "SRT ready";
    header.appendChild(badge);
  }

  section.appendChild(header);

  const body = document.createElement("div");
  body.className = "section-body";

  // Style row reusing opt-row pattern
  const styleRow = document.createElement("div");
  styleRow.className = "opt-row";
  const styleLbl = document.createElement("span");
  styleLbl.className = "opt-row-label";
  styleLbl.textContent = "burn-in";
  styleRow.appendChild(styleLbl);

  const STYLES = [
    { value: "none",  label: "None",    desc: "No burn-in (SRT sidecar only)" },
    { value: "clean", label: "Clean",   desc: "White text, thin black outline" },
    { value: "bold",  label: "Bold",    desc: "Large bold white, thick stroke" },
    { value: "gray",  label: "Gray",    desc: "Classic small gray subtitles" },
  ];

  const currentStyle = job.caption_style || "none";
  const btnGroup = document.createElement("div");
  btnGroup.className = "opt-pill-group";

  STYLES.forEach(({ value, label, desc }) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `opt-pill${value === currentStyle ? " active" : ""}`;
    btn.title = desc;
    btn.textContent = label;
    btn.addEventListener("click", async () => {
      btnGroup.querySelectorAll(".opt-pill").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      await patchOptions(job.job_id, { caption_style: value });
      job.caption_style = value;
    });
    btnGroup.appendChild(btn);
  });

  styleRow.appendChild(btnGroup);
  body.appendChild(styleRow);

  // SRT status row
  const srtRow = document.createElement("div");
  srtRow.className = "captions-srt-row";

  if (job.srt_key) {
    srtRow.innerHTML = `
      <span class="srt-info">\u2713 SRT generated after Stage 1</span>
      <a class="btn btn-secondary btn-sm" href="/api/jobs/${job.job_id}/srt" download="${job.job_id}.srt">\u2b07 Download SRT</a>
    `;
  } else {
    srtRow.innerHTML = `<span class="srt-info srt-missing">SRT will be generated during Stage 1 (auto-edit must be enabled)</span>`;
  }

  body.appendChild(srtRow);
  section.appendChild(body);
  return section;
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

function buildScriptSection(job) {
  const wrap = document.createElement("div");
  wrap.className = "script-section";

  // ── Header (tabs + edit button) ──
  const header = document.createElement("div");
  header.className = "script-header";

  const tabBar = document.createElement("div");
  tabBar.className = "script-tab-bar";

  const scriptTab = document.createElement("button");
  scriptTab.type = "button";
  scriptTab.className = "script-tab active";
  scriptTab.textContent = "Script";

  const subsTab = document.createElement("button");
  subsTab.type = "button";
  subsTab.className = "script-tab";
  subsTab.textContent = "Subtitles";

  tabBar.appendChild(scriptTab);
  tabBar.appendChild(subsTab);

  const editBtn = document.createElement("button");
  editBtn.className = "btn btn-secondary btn-sm";
  editBtn.textContent = "Edit";

  header.appendChild(tabBar);
  header.appendChild(editBtn);
  wrap.appendChild(header);

  // ── Script pane ──
  const scriptPane = document.createElement("div");
  scriptPane.className = "script-pane";

  const pre = document.createElement("pre");
  pre.className = "script-pre";
  pre.textContent = job.script || "(no script)";
  scriptPane.appendChild(pre);

  const textarea = document.createElement("textarea");
  textarea.className = "script-textarea hidden";
  textarea.value = job.script || "";
  textarea.rows = 12;
  scriptPane.appendChild(textarea);

  const saveRow = document.createElement("div");
  saveRow.className = "script-save-row hidden";
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-secondary btn-sm";
  cancelBtn.textContent = "Cancel";
  const saveBtn = document.createElement("button");
  saveBtn.className = "btn btn-primary btn-sm";
  saveBtn.textContent = "Save";
  saveRow.appendChild(cancelBtn);
  saveRow.appendChild(saveBtn);
  scriptPane.appendChild(saveRow);
  wrap.appendChild(scriptPane);

  // ── Subtitles pane ──
  const subsPane = document.createElement("div");
  subsPane.className = "script-pane hidden";

  if (job.srt_key) {
    subsPane.innerHTML = '<div class="subs-loading">Loading subtitles…</div>';
    let subsLoaded = false;
    const loadSubs = async () => {
      if (subsLoaded) return;
      subsLoaded = true;
      try {
        let subs = srtCache[job.srt_key];
        if (!subs) {
          const r = await fetch(`/api/jobs/${job.job_id}/srt`);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          subs = parseSRT(await r.text());
          srtCache[job.srt_key] = subs;
        }
        if (subs.length === 0) {
          subsPane.innerHTML = '<div class="subs-empty">No subtitle entries found.</div>';
          return;
        }
        const tableWrap = document.createElement("div");
        tableWrap.className = "subs-table-wrap";
        const table = document.createElement("table");
        table.className = "subs-table";
        table.innerHTML = "<thead><tr><th>#</th><th>Start</th><th>End</th><th>Text</th></tr></thead>";
        const tbody = document.createElement("tbody");
        subs.forEach((s, i) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td>${i + 1}</td><td>${fmtSRTTime(s.start)}</td><td>${fmtSRTTime(s.end)}</td><td>${esc(s.text)}</td>`;
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        tableWrap.appendChild(table);
        subsPane.innerHTML = "";
        subsPane.appendChild(tableWrap);
      } catch (e) {
        subsPane.innerHTML = `<div class="subs-error">Failed to load subtitles: ${esc(e.message)}</div>`;
      }
    };
    subsTab.addEventListener("click", loadSubs);
  } else {
    subsPane.innerHTML = '<div class="subs-empty">Subtitles available after Stage 1 (auto-edit must be enabled)</div>';
  }

  wrap.appendChild(subsPane);

  // ── Tab switching ──
  scriptTab.addEventListener("click", () => {
    scriptTab.classList.add("active");
    subsTab.classList.remove("active");
    scriptPane.classList.remove("hidden");
    subsPane.classList.add("hidden");
    editBtn.classList.remove("hidden");
  });
  subsTab.addEventListener("click", () => {
    subsTab.classList.add("active");
    scriptTab.classList.remove("active");
    subsPane.classList.remove("hidden");
    scriptPane.classList.add("hidden");
    editBtn.classList.add("hidden");
  });

  // ── Edit functionality ──
  function enterEdit() {
    pre.classList.add("hidden");
    textarea.classList.remove("hidden");
    saveRow.classList.remove("hidden");
    editBtn.classList.add("hidden");
    textarea.focus();
  }

  function exitEdit() {
    textarea.classList.add("hidden");
    saveRow.classList.add("hidden");
    pre.classList.remove("hidden");
    editBtn.classList.remove("hidden");
  }

  editBtn.addEventListener("click", enterEdit);
  cancelBtn.addEventListener("click", () => {
    textarea.value = job.script || "";
    exitEdit();
  });
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving\u2026";
    try {
      const r = await fetch(`/api/jobs/${job.job_id}/script`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: textarea.value }),
      });
      if (!r.ok) throw new Error((await r.json()).error);
      job.script = textarea.value;
      pre.textContent = textarea.value || "(no script)";
      toast("Script saved", "success");
      exitEdit();
    } catch (e) {
      toast(`Save failed: ${e.message}`, "error");
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
    }
  });

  return wrap;
}

function buildActions(job) {
  const row = document.createElement("div");
  row.className = "actions-row";

  const isRunning = job.status === "stage1_running" || job.status === "stage2_running";

  // Upload video for queued (script-only) jobs
  if (job.status === "queued" && !isRunning) {
    const btn = makeBtn("📤 Upload Video", "btn-primary btn-sm");
    btn.addEventListener("click", () => openUploadModalForJob(job));
    row.appendChild(btn);
  }

  // Push script to teleprompter
  if (job.script && !isRunning) {
    const btn = makeBtn("📱 Teleprompter", "btn-secondary btn-sm");
    btn.addEventListener("click", () => pushToTeleprompter(job.job_id));
    row.appendChild(btn);
  }

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

  // Delete — always visible
  const delBtn = makeBtn("🗑 Delete", "btn-danger btn-sm");
  delBtn.style.marginLeft = "auto";
  delBtn.addEventListener("click", () => confirmDeleteJob(job.job_id, job.title));
  row.appendChild(delBtn);

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

let _deleteTargetId = null;

function confirmDeleteJob(jobId, title) {
  _deleteTargetId = jobId;
  document.getElementById("delete-modal-msg").textContent =
    `"${title || jobId}" will be permanently deleted — database record, MinIO files, and all logs. This cannot be undone.`;
  document.getElementById("delete-modal").classList.remove("hidden");
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
  document.getElementById("up-link-job-id").value = "";
  document.getElementById("up-link-notice").classList.add("hidden");
  selectedFile = null;
  document.getElementById("drop-zone").classList.remove("has-file");
  document.querySelector("#drop-zone p").textContent = "Drop video here or click to browse";
  document.getElementById("btn-confirm-upload").disabled = true;
}

function openUploadModalForJob(job) {
  document.getElementById("up-influencer").value = job.influencer || "emma";
  document.getElementById("up-title").value = job.title || "";
  document.getElementById("up-link-job-id").value = job.job_id;
  document.getElementById("up-link-title").textContent = `${job.title || job.job_id} (${job.job_id})`;
  document.getElementById("up-link-notice").classList.remove("hidden");
  document.getElementById("upload-modal").classList.remove("hidden");
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

  const linkJobId = document.getElementById("up-link-job-id").value;

  const form = new FormData();
  form.append("video", selectedFile);
  form.append("influencer", influencer);
  form.append("title", title);
  form.append("auto_start", autoStart ? "1" : "0");
  if (linkJobId) form.append("job_id", linkJobId);

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
    if (xhr.status === 200 || xhr.status === 201) {
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

async function forkJob(jobId) {
  try {
    const r = await fetch(`/api/jobs/${jobId}/fork`, { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error);
    toast(`Forked → ${data.job_id}`, "success");
    await loadJobs();
  } catch (e) {
    toast(`Fork failed: ${e.message}`, "error");
  }
}

// ── SRT / Caption helpers ──────────────────────────────────────────────────────

function parseSRT(text) {
  return text.trim().split(/\n\n+/).map(block => {
    const lines = block.split('\n');
    if (lines.length < 3) return null;
    const timeParts = lines[1].split(' --> ');
    if (timeParts.length < 2) return null;
    const start = srtTimeToSeconds(timeParts[0].trim());
    const end   = srtTimeToSeconds(timeParts[1].trim());
    const text  = lines.slice(2).join(' ').replace(/<[^>]+>/g, '').trim();
    return { start, end, text };
  }).filter(Boolean);
}

function srtTimeToSeconds(t) {
  // Format: HH:MM:SS,mmm
  const [hms, ms] = t.replace(',', '.').split('.');
  const parts = hms.split(':').map(Number);
  const [h, m, s] = parts.length === 3 ? parts : [0, ...parts];
  return h * 3600 + m * 60 + s + (parseFloat('0.' + (ms || '0')));
}

function fmtSRTTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const d = Math.round((secs % 1) * 10);
  return `${m}:${String(s).padStart(2, '0')}.${d}`;
}

async function fetchAndSetupSRT(srtKey, jobId, video, textEl) {
  if (!srtCache[srtKey]) {
    try {
      const r = await fetch(`/api/jobs/${jobId}/srt`);
      if (!r.ok) return;
      srtCache[srtKey] = parseSRT(await r.text());
    } catch { return; }
  }
  const subs = srtCache[srtKey];
  video.addEventListener("timeupdate", () => {
    const t = video.currentTime;
    const active = subs.find(s => t >= s.start && t <= s.end);
    textEl.textContent = active ? active.text : "";
  });
}

function setupCaptionDrag(overlay, container, job) {
  overlay.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    overlay.classList.add("dragging");
    _activeDrag = {
      overlay, container, job,
      startX:    e.clientX,
      startY:    e.clientY,
      startLeft: parseFloat(overlay.style.left),
      startTop:  parseFloat(overlay.style.top),
    };
  });
  overlay.addEventListener("touchstart", (e) => {
    const touch = e.touches[0];
    overlay.classList.add("dragging");
    _activeDrag = {
      overlay, container, job,
      startX:    touch.clientX,
      startY:    touch.clientY,
      startLeft: parseFloat(overlay.style.left),
      startTop:  parseFloat(overlay.style.top),
    };
  }, { passive: true });
}

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

// ── Timeline Editor ───────────────────────────────────────────────────────────

function buildTimeline(job) {
  let ivs;
  try { ivs = JSON.parse(job.intervals_json || "[]"); } catch(_) { ivs = []; }
  if (!ivs.length) return null;

  const section = document.createElement("div");
  section.className = "section-card";

  const header = document.createElement("div");
  header.className = "section-header";
  const lbl = document.createElement("span");
  lbl.className = "section-label";
  lbl.textContent = "Cut Timeline";
  header.appendChild(lbl);
  section.appendChild(header);

  const body = document.createElement("div");
  body.className = "section-body";
  body.style.padding = "10px";

  // Timeline state
  const state = {
    intervals: ivs.map(([s, e]) => [s, e]),
    origIntervals: ivs.map(([s, e]) => [s, e]),
    undoStack: [],
    duration: 0,
    waveform: [],
  };

  // Wrap containing canvas + handle layer + playhead
  const wrap = document.createElement("div");
  wrap.className = "timeline-wrap";

  const canvas = document.createElement("canvas");
  canvas.className = "waveform-canvas";
  canvas.height = 80;
  wrap.appendChild(canvas);

  const handleLayer = document.createElement("div");
  handleLayer.className = "timeline-handles";
  wrap.appendChild(handleLayer);

  const playhead = document.createElement("div");
  playhead.className = "timeline-playhead";
  playhead.style.left = "0%";
  wrap.appendChild(playhead);

  body.appendChild(wrap);

  // Footer: stats + buttons
  const footer = document.createElement("div");
  footer.className = "timeline-footer";

  const statsEl = document.createElement("span");
  statsEl.className = "timeline-stats";
  footer.appendChild(statsEl);

  const actionsEl = document.createElement("div");
  actionsEl.className = "timeline-actions";

  const btnUndo = document.createElement("button");
  btnUndo.className = "btn btn-secondary btn-sm";
  btnUndo.textContent = "↩ Undo";
  btnUndo.disabled = true;

  const btnReset = document.createElement("button");
  btnReset.className = "btn btn-secondary btn-sm";
  btnReset.textContent = "Reset";

  const btnApply = document.createElement("button");
  btnApply.className = "btn btn-primary btn-sm";
  btnApply.textContent = "Apply cuts";

  actionsEl.appendChild(btnUndo);
  actionsEl.appendChild(btnReset);
  actionsEl.appendChild(btnApply);
  footer.appendChild(actionsEl);
  body.appendChild(footer);
  section.appendChild(body);

  // ── Rendering ──────────────────────────────────────────────────────────────

  function updateStats() {
    const kept = state.intervals.reduce((s, [a,b]) => s + (b - a), 0);
    const total = state.duration || (state.waveform.length * 0.05);
    statsEl.textContent = total
      ? `${state.intervals.length} segment${state.intervals.length !== 1 ? "s" : ""} · ${kept.toFixed(1)}s kept of ${total.toFixed(1)}s`
      : `${state.intervals.length} segment${state.intervals.length !== 1 ? "s" : ""}`;
    btnUndo.disabled = state.undoStack.length === 0;
  }

  function drawWaveform() {
    const W = wrap.clientWidth || 600;
    canvas.width = W;
    const H = 80;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);

    const wf = state.waveform;
    if (!wf.length) {
      // Fallback: draw solid bars based on intervals only
      ctx.fillStyle = "#1a1a1a";
      ctx.fillRect(0, 0, W, H);
      const dur = state.duration;
      if (dur > 0) {
        state.intervals.forEach(([s, e]) => {
          ctx.fillStyle = "rgba(16,185,129,.35)";
          ctx.fillRect((s/dur)*W, 0, ((e-s)/dur)*W, H);
        });
      }
      return;
    }

    const dur = wf.length * 0.05;
    state.duration = dur;
    const barW = Math.max(1, W / wf.length);

    for (let i = 0; i < wf.length; i++) {
      const t = i * 0.05;
      const inKeep = state.intervals.some(([s, e]) => t >= s && t < e);
      const rms = wf[i];
      const barH = Math.max(2, rms * (H - 4) * 0.95);
      ctx.fillStyle = inKeep ? "#10b981" : "#2a2a2a";
      ctx.fillRect(i * barW, H - barH, Math.max(1, barW - 0.5), barH);
    }
  }

  function drawHandles() {
    handleLayer.innerHTML = "";
    const dur = state.duration;
    if (!dur) return;
    state.intervals.forEach(([s, e], idx) => {
      [["left", s], ["right", e]].forEach(([side, t]) => {
        const h = document.createElement("div");
        h.className = "trim-handle";
        h.style.left = `${(t / dur) * 100}%`;
        h.title = `${t.toFixed(2)}s`;
        h.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          // Push undo snapshot
          state.undoStack.push(state.intervals.map(([a,b]) => [a,b]));
          if (state.undoStack.length > 20) state.undoStack.shift();
          h.classList.add("dragging");
          _tlDrag = { wrap, state, idx, side, handle: h, redraw };
        });
        handleLayer.appendChild(h);
      });
    });
  }

  function redraw() {
    drawWaveform();
    drawHandles();
    updateStats();
  }

  // ── Click on timeline to seek preview video ─────────────────────────────────
  wrap.addEventListener("click", (e) => {
    if (_tlDrag) return;
    const rect = wrap.getBoundingClientRect();
    const pct  = (e.clientX - rect.left) / rect.width;
    const t    = pct * state.duration;
    // Find preview video in the same card
    const card = wrap.closest(".card");
    if (card) {
      const vid = card.querySelector(".panel-preview video");
      if (vid) vid.currentTime = t;
    }
  });

  // ── Undo / Reset / Apply ────────────────────────────────────────────────────
  btnUndo.addEventListener("click", () => {
    if (!state.undoStack.length) return;
    state.intervals = state.undoStack.pop();
    redraw();
  });

  btnReset.addEventListener("click", () => {
    state.undoStack.push(state.intervals.map(([a,b]) => [a,b]));
    state.intervals = state.origIntervals.map(([a,b]) => [a,b]);
    redraw();
  });

  btnApply.addEventListener("click", async () => {
    btnApply.disabled = true;
    btnApply.textContent = "Applying…";
    try {
      const r = await fetch(`/api/jobs/${job.job_id}/reencode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intervals: state.intervals }),
      });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      toast("Re-encoding started — preview will update shortly");
      // Poll for completion
      const poll = setInterval(async () => {
        const jr = await fetch(`/api/jobs/${job.job_id}`);
        const jd = await jr.json();
        if (jd.status === "awaiting_review") {
          clearInterval(poll);
          loadJobs();
        } else if (jd.status === "error") {
          clearInterval(poll);
          toast("Re-encode failed: " + (jd.error_msg || "unknown"), "error");
          loadJobs();
        }
      }, 3000);
    } catch (ex) {
      toast("Re-encode failed: " + ex.message, "error");
    } finally {
      btnApply.disabled = false;
      btnApply.textContent = "Apply cuts";
    }
  });

  // ── Hook playhead to preview video ─────────────────────────────────────────
  // Attach after DOM insertion via MutationObserver trick using requestAnimationFrame
  requestAnimationFrame(() => {
    const card = wrap.closest(".card");
    if (!card) return;
    const vid = card.querySelector(".panel-preview video");
    if (vid && state.duration) {
      vid.addEventListener("timeupdate", () => {
        playhead.style.left = `${(vid.currentTime / state.duration) * 100}%`;
      });
    }
  });

  // ── Load waveform async ─────────────────────────────────────────────────────
  // First do a quick render with interval data only
  // Estimate duration from last interval end
  state.duration = state.intervals[state.intervals.length - 1][1] * 1.05;
  redraw();

  fetch(`/api/jobs/${job.job_id}/waveform`)
    .then(r => r.json())
    .then(wf => {
      if (wf && wf.length) {
        state.waveform = wf;
        state.duration = wf.length * 0.05;
        redraw();
      }
    })
    .catch(() => {/* waveform not available yet */});

  return section;
}

function fmtStatus(s) {
  return {
    queued:          "Queued",
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

// ── Create Script modal ────────────────────────────────────────────────────────

function openCreateModal() {
  document.getElementById("create-modal").classList.remove("hidden");
  document.getElementById("cr-script").focus();
}

function closeCreateModal() {
  document.getElementById("create-modal").classList.add("hidden");
  document.getElementById("cr-title").value = "";
  document.getElementById("cr-script").value = "";
  document.getElementById("cr-push-teleprompter").checked = true;
}

function setupCreateModal() {
  document.getElementById("btn-cancel-create").addEventListener("click", closeCreateModal);
  document.getElementById("create-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("create-modal")) closeCreateModal();
  });
  document.getElementById("btn-confirm-create").addEventListener("click", doCreateScript);
}

async function doCreateScript() {
  const influencer  = document.getElementById("cr-influencer").value;
  const title       = document.getElementById("cr-title").value.trim();
  const script      = document.getElementById("cr-script").value.trim();
  const pushToPhone = document.getElementById("cr-push-teleprompter").checked;

  if (!script) {
    toast("Script is required", "error");
    return;
  }

  const btn = document.getElementById("btn-confirm-create");
  btn.disabled = true;
  btn.textContent = "Creating…";

  try {
    const r = await fetch("/api/jobs/script", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ influencer, title, script, auto_start: false }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error);

    const jobId = data.job_id;
    toast(`Job created: ${jobId}`, "success");

    if (pushToPhone) {
      try {
        const r2 = await fetch(`/api/jobs/${jobId}/teleprompter`, { method: "POST" });
        const d2 = await r2.json();
        if (!r2.ok) throw new Error(d2.error);
        toast("Script pushed to teleprompter", "success");
      } catch (e) {
        toast(`Teleprompter push failed: ${e.message}`, "error");
      }
    }

    closeCreateModal();
    await loadJobs();
  } catch (e) {
    toast(`Create failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Create Job";
  }
}

async function pushToTeleprompter(jobId) {
  try {
    const r = await fetch(`/api/jobs/${jobId}/teleprompter`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    toast("Script pushed to teleprompter", "success");
  } catch (e) {
    toast(`Teleprompter push failed: ${e.message}`, "error");
  }
}
