// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allFiles = [];
let filteredFiles = [];
let currentFile = null;        // { name, source }
let rawContent = "";           // last fetched raw text
let contentLines = [];         // rawContent split by newline
let fontSize = Number(localStorage.getItem("logFontSize") || 13);
let wrapEnabled = localStorage.getItem("logWrap") === "true";
let autoRefreshHandle = null;

const API_BASE = "/api/logs";

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function fmtBytes(n) {
  if (!isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (!isFinite(d.getTime())) return "—";
  return d.toLocaleString();
}

function showToast(message, variant = "dark") {
  const container = byId("toast");
  const id = "toast-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  const html = `
    <div id="${id}" class="toast align-items-center text-bg-${variant} border-0" role="alert">
      <div class="d-flex">
        <div class="toast-body">${escapeHtml(message)}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto"
                data-bs-dismiss="toast"></button>
      </div>
    </div>`;
  container.insertAdjacentHTML("beforeend", html);
  const el = byId(id);
  const t = new bootstrap.Toast(el, { delay: 1800 });
  t.show();
  el.addEventListener("hidden.bs.toast", () => el.remove());
}

// ---------------------------------------------------------------------------
// File list
// ---------------------------------------------------------------------------
async function loadFiles() {
  const includeModules = byId("include-modules").checked;
  byId("file-list").innerHTML =
    `<div class="list-group-item text-secondary small">Loading…</div>`;

  try {
    const res = await fetch(
      `${API_BASE}?include_modules=${includeModules ? "true" : "false"}`,
      { cache: "no-store" }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    allFiles = Array.isArray(data.files) ? data.files : [];
    renderFileList();
    byId("file-count").textContent = allFiles.length;
  } catch (err) {
    byId("file-list").innerHTML =
      `<div class="list-group-item text-danger small">
         Failed to load files: ${escapeHtml(err.message)}
       </div>`;
  }
}

function renderFileList() {
  const query = byId("file-search").value.trim().toLowerCase();
  const sourceFilter =
    document.querySelector('input[name="filter-source"]:checked')?.value || "all";

  filteredFiles = allFiles.filter((f) => {
    if (sourceFilter !== "all" && f.source !== sourceFilter) return false;
    if (query && !f.name.toLowerCase().includes(query)) return false;
    return true;
  });

  if (!filteredFiles.length) {
    byId("file-list").innerHTML =
      `<div class="list-group-item text-secondary small">No matching files.</div>`;
    return;
  }

  const html = filteredFiles
    .map((f) => {
      const active =
        currentFile &&
        currentFile.name === f.name &&
        currentFile.source === f.source;
      const badge = f.source === "combined"
        ? `<span class="badge text-bg-primary">combined</span>`
        : `<span class="badge text-bg-info">module</span>`;
      return `
        <button type="button"
                class="list-group-item list-group-item-action d-flex
                       flex-column gap-1 ${active ? "active" : ""}"
                data-name="${escapeHtml(f.name)}"
                data-source="${escapeHtml(f.source)}">
          <div class="d-flex justify-content-between align-items-start gap-2">
            <span class="fw-semibold text-truncate">${escapeHtml(f.name)}</span>
            ${badge}
          </div>
          <div class="d-flex justify-content-between small ${active ? "" : "text-secondary"}">
            <span>${fmtBytes(f.size_bytes)}</span>
            <span>${fmtDate(f.modified_at)}</span>
          </div>
        </button>`;
    })
    .join("");

  byId("file-list").innerHTML = html;

  byId("file-list")
    .querySelectorAll("button.list-group-item")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        selectFile(btn.dataset.name, btn.dataset.source);
      });
    });
}

function selectFile(name, source) {
  currentFile = { name, source };
  renderFileList();
  loadContent();
}

// ---------------------------------------------------------------------------
// Content load + render
// ---------------------------------------------------------------------------
async function loadContent() {
  if (!currentFile) return;

  const linesSel = byId("lines-select").value;
  const tail = linesSel !== "0";
  const lines = tail ? Number(linesSel) : 0;

  const path =
    currentFile.source === "module"
      ? `${API_BASE}/modules/${encodeURIComponent(currentFile.name)}`
      : `${API_BASE}/${encodeURIComponent(currentFile.name)}`;

  const params = new URLSearchParams();
  params.set("tail", tail ? "true" : "false");
  if (tail) params.set("lines", String(lines));

  byId("footer-status").textContent = "Loading…";
  byId("current-file").textContent = currentFile.name;

  try {
    const res = await fetch(`${path}?${params.toString()}`, { cache: "no-store" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();

    rawContent = data.content || "";
    contentLines = rawContent.length ? rawContent.split(/\r?\n/) : [];

    byId("current-file-meta").textContent =
      `· ${fmtBytes(data.size_bytes)} · ${fmtDate(data.modified_at)} · ${data.total_lines} total`;
    byId("footer-info").textContent =
      `${data.total_lines} total lines · showing ${data.returned_lines} · ${data.path || ""}`;
    byId("footer-status").textContent = "";

    renderContent();
  } catch (err) {
    rawContent = "";
    contentLines = [];
    byId("log-content").innerHTML =
      `<span class="text-danger">Failed to load: ${escapeHtml(err.message)}</span>`;
    byId("footer-status").textContent = "Error";
  }
}

function renderContent() {
  const needle = byId("content-search").value.trim();
  const pre = byId("log-content");

  if (!contentLines.length) {
    pre.innerHTML = `<span class="text-secondary">(empty file)</span>`;
    byId("match-count").textContent = "";
    return;
  }

  const needleLower = needle.toLowerCase();
  let matchCount = 0;
  let matchedLines = 0;

  const rendered = contentLines.map((line) => {
    if (!needle) return escapeHtml(line);

    const lower = line.toLowerCase();
    let idx = 0;
    let count = 0;
    while ((idx = lower.indexOf(needleLower, idx)) !== -1) {
      count++;
      idx += needleLower.length;
    }
    matchCount += count;
    if (count > 0) matchedLines++;

    return highlightLine(line, needle);
  });

  pre.innerHTML = rendered.join("\n");

  if (needle) {
    byId("match-count").textContent =
      `${matchCount} match${matchCount === 1 ? "" : "es"} on ${matchedLines} line${matchedLines === 1 ? "" : "s"}`;
  } else {
    byId("match-count").textContent = "";
  }

  applyFontSize();
  applyWrap();
}

function highlightLine(line, needle) {
  if (!needle) return escapeHtml(line);
  const lower = line.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let out = "";
  let i = 0;
  let idx;
  while ((idx = lower.indexOf(lowerNeedle, i)) !== -1) {
    out += escapeHtml(line.slice(i, idx));
    out += `<mark>${escapeHtml(line.slice(idx, idx + needle.length))}</mark>`;
    i = idx + needle.length;
  }
  out += escapeHtml(line.slice(i));
  return out;
}

// ---------------------------------------------------------------------------
// Zoom / wrap / copy / download
// ---------------------------------------------------------------------------
function applyFontSize() {
  const pre = byId("log-content");
  pre.style.fontSize = `${fontSize}px`;
  byId("zoom-label").textContent = `${Math.round((fontSize / 13) * 100)}%`;
  localStorage.setItem("logFontSize", String(fontSize));
}

function zoomIn()    { fontSize = Math.min(28, fontSize + 1); applyFontSize(); }
function zoomOut()   { fontSize = Math.max(8,  fontSize - 1); applyFontSize(); }
function zoomReset() { fontSize = 13; applyFontSize(); }

function applyWrap() {
  const pre = byId("log-content");
  pre.classList.toggle("log-wrap", wrapEnabled);
  byId("wrap-btn").classList.toggle("active", wrapEnabled);
  localStorage.setItem("logWrap", String(wrapEnabled));
}

async function copyContent() {
  if (!rawContent) {
    showToast("Nothing to copy", "secondary");
    return;
  }
  try {
    await navigator.clipboard.writeText(rawContent);
    showToast("Log copied to clipboard", "success");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = rawContent;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      showToast("Log copied to clipboard", "success");
    } catch {
      showToast("Copy failed", "danger");
    }
    ta.remove();
  }
}

function downloadContent() {
  if (!rawContent || !currentFile) {
    showToast("Nothing to download", "secondary");
    return;
  }
  const blob = new Blob([rawContent], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = currentFile.name || "log.txt";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  showToast("Download started", "success");
}

// ---------------------------------------------------------------------------
// Auto refresh
// ---------------------------------------------------------------------------
function toggleAutoRefresh() {
  const btn = byId("auto-refresh-btn");
  const status = byId("auto-refresh-status");

  if (autoRefreshHandle) {
    clearInterval(autoRefreshHandle);
    autoRefreshHandle = null;
    btn.classList.remove("btn-success");
    btn.classList.add("btn-outline-secondary");
    btn.innerHTML = `<i class="bi bi-play-circle"></i>`;
    status.classList.add("d-none");
    return;
  }

  if (!currentFile) {
    showToast("Select a file first", "secondary");
    return;
  }

  btn.classList.remove("btn-outline-secondary");
  btn.classList.add("btn-success");
  btn.innerHTML = `<i class="bi bi-pause-circle"></i>`;
  status.classList.remove("d-none");

  autoRefreshHandle = setInterval(() => {
    if (currentFile) loadContent();
  }, 3000);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
byId("files-refresh").addEventListener("click", loadFiles);
byId("include-modules").addEventListener("change", loadFiles);
byId("file-search").addEventListener("input", renderFileList);

document.querySelectorAll('input[name="filter-source"]').forEach((el) => {
  el.addEventListener("change", renderFileList);
});

byId("content-search").addEventListener("input", renderContent);
byId("content-search-clear").addEventListener("click", () => {
  byId("content-search").value = "";
  renderContent();
  byId("content-search").focus();
});

byId("zoom-in").addEventListener("click", zoomIn);
byId("zoom-out").addEventListener("click", zoomOut);
byId("zoom-reset").addEventListener("click", zoomReset);

byId("copy-btn").addEventListener("click", copyContent);
byId("download-btn").addEventListener("click", downloadContent);
byId("reload-btn").addEventListener("click", () => loadContent());
byId("auto-refresh-btn").addEventListener("click", toggleAutoRefresh);
byId("wrap-btn").addEventListener("click", () => {
  wrapEnabled = !wrapEnabled;
  applyWrap();
});

byId("lines-select").addEventListener("change", () => {
  if (currentFile) loadContent();
});

// Keyboard shortcuts: Ctrl/Cmd +/-/0 for zoom
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey) {
    if (e.key === "=" || e.key === "+") { e.preventDefault(); zoomIn(); }
    else if (e.key === "-")              { e.preventDefault(); zoomOut(); }
    else if (e.key === "0")              { e.preventDefault(); zoomReset(); }
  }
});

// Init
applyFontSize();
applyWrap();
loadFiles();