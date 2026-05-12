const $ = (id) => document.getElementById(id);

// ── HTML-escape helper ──────────────────────────────────────────
// Prevents XSS when rendering vault metadata (Issue #29).
// Mirrors the _escapeHtml method used in app.js.
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function fmtCount(value) {
  return Number(value || 0).toLocaleString();
}

function fmtTime(epochSeconds) {
  if (!epochSeconds) return "Never updated";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function renderBars(target, counts) {
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  target.classList.toggle("empty", entries.length === 0);
  if (!entries.length) {
    target.textContent = "No pages yet.";
    return;
  }
  const max = Math.max(...entries.map(([, count]) => count), 1);
  target.innerHTML = entries.map(([label, count]) => `
    <div class="memory-bar-row">
      <span>${escapeHtml(label)}</span>
      <div class="memory-bar"><i style="width:${Math.max(8, (count / max) * 100)}%"></i></div>
      <strong>${fmtCount(count)}</strong>
    </div>
  `).join("");
}

function renderTags(target, tags) {
  target.classList.toggle("empty", !tags?.length);
  if (!tags?.length) {
    target.textContent = "No tags yet.";
    return;
  }
  target.innerHTML = tags.map(({ tag, count }) => `
    <span class="memory-tag"><b>${escapeHtml(tag)}</b><small>${fmtCount(count)}</small></span>
  `).join("");
}

function renderRecent(target, pages) {
  target.classList.toggle("empty", !pages?.length);
  if (!pages?.length) {
    target.textContent = "No recent pages yet.";
    return;
  }
  target.innerHTML = pages.map((page) => `
    <div class="memory-row">
      <div><strong>${escapeHtml(page.title)}</strong><span>${escapeHtml(page.path)}</span></div>
      <em>${escapeHtml(page.type)}</em>
    </div>
  `).join("");
}

async function loadMemoryOverview() {
  const status = $("memory-status");
  status.textContent = "Refreshing memory overview…";
  try {
    const resp = await fetch("/api/v1/memory/overview", { cache: "no-store" });
    const env = await resp.json();
    if (!env.ok) throw new Error(env.error || env.code || "Memory API failed");
    const data = env.data;
    $("memory-vault-path").textContent = data.exists
      ? `${data.vault_path} · updated ${fmtTime(data.last_updated)}`
      : `${data.vault_path} · vault not initialized yet`;
    $("memory-wiki-pages").textContent = fmtCount(data.wiki_pages);
    $("memory-archives").textContent = fmtCount(data.memories);
    $("memory-raw").textContent = fmtCount(data.raw_items);
    $("memory-types-count").textContent = fmtCount(Object.keys(data.page_types || {}).length);
    renderBars($("memory-page-types"), data.page_types);
    renderTags($("memory-tags"), data.top_tags);
    renderRecent($("memory-recent"), data.recent_pages);
    status.textContent = data.exists ? "Memory overview loaded." : "Memory vault not found. Run `orb memory init` to initialize it.";
  } catch (error) {
    status.textContent = `Unable to load memory overview: ${error.message}`;
  }
}

// Only run browser-side auto-initialization when not in Node.
// (happy-dom provides a window object, so we check for process instead.)
if (typeof process === 'undefined' || !process.versions || !process.versions.node) {
  $("memory-refresh")?.addEventListener("click", loadMemoryOverview);
  loadMemoryOverview();
}

// ── Expose helpers for unit tests (Node/Vitest). No-op in the browser. ──
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { escapeHtml, renderBars, renderTags, renderRecent };
}
