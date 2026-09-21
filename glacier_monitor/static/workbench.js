const state = { summary: null, archive: [], catalog: [], receipts: [], comparisons: [], icimod: [], gee: null };

async function loadJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || `Request failed (${response.status})`);
  return body;
}

function safe(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[character]));
}

function words(value) { return String(value || "unknown").replaceAll("_", " "); }
function time(value) {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
function pretty(value) { return JSON.stringify(value, null, 2); }
function empty(message) { return `<div class="empty-state">${safe(message)}</div>`; }

function recordRow(item) {
  return `<article class="pilot-row">
    <div><span class="badge ${item.origin === "synthetic" ? "warning" : "research_only"}">${safe(words(item.record_type))}</span>
      <h3>${safe(item.title || item.archive_record_id)}</h3>
      <p>${safe(item.source_database)} · ${safe(time(item.issue_time || item.created_at))}</p></div>
    <div class="pilot-hash"><span>${item.research_only ? "Research only" : "Review required"}</span>
      <code>${safe((item.content_sha256 || item.input_sha256 || "no hash").slice(0, 16))}</code></div>
  </article>`;
}

function renderSummary() {
  const summary = state.summary;
  const imported = summary.latest_import?.summary || {};
  const metrics = [
    ["Archived records", Object.values(summary.archive_counts).reduce((a, b) => a + b, 0)],
    ["ICIMOD records", summary.external_record_count],
    ["Earth Engine features", summary.gee_feature_count],
    ["Public warning", summary.public_delivery_enabled ? "Enabled" : "Disabled"]
  ];
  document.querySelector("#workbench-metrics").innerHTML = metrics.map(([label, value]) =>
    `<div><span>${safe(label)}</span><strong>${safe(value)}</strong></div>`).join("");
  document.querySelector("#recent-runs").innerHTML =
    summary.latest_runs.length ? summary.latest_runs.map(recordRow).join("") : empty("No imported research runs.");
}

function renderArchive() {
  const select = document.querySelector("#archive-record");
  select.innerHTML = `<option value="">Select a record</option>` + state.archive.map(item =>
    `<option value="${item.id}">${safe(words(item.record_type))}: ${safe(item.title || item.archive_record_id)}</option>`).join("");
  const predictions = state.archive.filter(item => ["experiment", "forecast", "explanation", "model_artifact"].includes(item.record_type));
  const synthetic = state.archive.filter(item => item.origin === "synthetic" || item.source_database === "synthetic.db");
  document.querySelector("#prediction-records").innerHTML = predictions.length ? predictions.map(recordRow).join("") : empty("No archived prediction records.");
  document.querySelector("#synthetic-records").innerHTML = synthetic.length ? synthetic.map(recordRow).join("") : empty("No archived synthetic runs.");
}

function renderSources() {
  document.querySelector("#source-catalog").innerHTML = state.catalog.map(item => `<article class="discovery-card ${safe(item.status)}">
    <span class="badge ${item.status === "reachable" ? "fresh" : "unknown"}">${safe(words(item.status))}</span>
    <h3>${safe(item.provider)}</h3><p>${safe(item.capability)}</p>
    <p><strong>Priority:</strong> ${safe(item.priority)} · <strong>Role:</strong> ${safe(words(item.role))}</p>
    <p>${safe(item.limitation)}</p>
    ${item.endpoint ? `<a href="${safe(item.endpoint)}" target="_blank" rel="noreferrer">Official source</a>` : "<span>Optional integration not configured</span>"}
  </article>`).join("");
  document.querySelector("#icimod-datasets").innerHTML = state.icimod.length ? state.icimod.map(item => `<article class="pilot-row">
    <div><span class="badge ${item.status === "imported" ? "fresh" : item.status === "failed" ? "critical" : "unknown"}">${safe(words(item.status))}</span>
      <h3>${safe(item.title)}</h3><p>${safe(item.license)} · ${safe(item.record_count)} normalized records · ${item.byte_count ? `${safe(item.byte_count)} bytes` : "file not downloaded"}</p>
      <p>${item.error_message ? safe(item.error_message) : "Raw file is checksum registered; raster files may have zero normalized rows."}</p></div>
    <div class="pilot-hash"><a href="${safe(item.metadata_url)}" target="_blank" rel="noreferrer">Metadata</a>
      <code>${safe((item.file_sha256 || item.dataset_uuid || "").slice(0, 16))}</code></div>
  </article>`).join("") : empty("No ICIMOD datasets have been synchronized.");
  const gee = state.gee;
  document.querySelector("#gee-status").className = `prose-card ${gee.available ? "" : "readiness-note"}`;
  document.querySelector("#gee-status").innerHTML = `<span class="badge ${gee.available ? "fresh" : "not_configured"}">${gee.available ? "Authenticated" : "Not configured"}</span>
    <h3>Earth Engine Python API</h3><p>${safe(gee.message)}</p>
    <p><strong>Project:</strong> ${safe(gee.project || "not set")} · <strong>Stored features:</strong> ${safe(state.summary.gee_feature_count)}</p>
    <ul>${Object.values(gee.datasets).map(item => `<li><a href="${safe(item.source_url)}" target="_blank" rel="noreferrer">${safe(item.dataset_id)}</a> — ${safe(words(item.reducer))}, ${safe(item.scale_m)} m</li>`).join("")}</ul>
    <p>Earth Engine results remain satellite/reanalysis covariates. They do not become warning evidence without temporal, quality, and scientific review.</p>`;
}

function renderReceipts() {
  document.querySelector("#acquisition-receipts").innerHTML = state.receipts.length ? state.receipts.map(item => `<article class="pilot-row">
    <div><span class="badge research_only">${safe(words(item.source_role))}</span><h3>${safe(item.source_id)}</h3>
      <p>${safe(item.source_item_id || "registered case asset")} · ${safe(time(item.observed_end || item.processed_at))}</p>
      <p>${safe(words(item.quality_status))} · rights ${safe(words(item.rights_status))} · ${safe(words(item.eligibility_state))}</p></div>
    <div class="pilot-hash"><span>${item.byte_count ? `${safe(item.byte_count)} bytes` : "metadata only"}</span>
      <code>${safe((item.content_sha256 || item.receipt_id || "").slice(0, 16))}</code></div>
  </article>`).join("") : empty("No acquisition receipts.");
}

function renderComparisons() {
  document.querySelector("#observation-comparisons").innerHTML = state.comparisons.length ? state.comparisons.map(item => `<article class="pilot-row">
    <div><span class="badge ${item.source_system.includes("synthetic") ? "warning" : "research_only"}">${safe(words(item.review_status))}</span>
      <h3>${safe(item.comparison_id)}</h3><p>${safe(item.source_system)} · ${safe(item.object_count)} object(s)</p>
      <p>Common coverage: ${item.common_coverage_fraction == null ? "unknown" : `${(item.common_coverage_fraction * 100).toFixed(1)}%`} · ${safe(item.limitations[0] || "Bounded observation comparison")}</p></div>
    <div class="pilot-hash"><span>${safe(time(item.created_at))}</span><code>${safe((item.input_hash || item.comparison_id || "").slice(0, 16))}</code></div>
  </article>`).join("") : empty("No observation comparisons.");
}

function renderScenarios() {
  document.querySelector("#synthetic-scenarios").innerHTML = state.summary.synthetic_scenarios.map(item =>
    `<article class="scenario-card"><span class="badge warning">Synthetic</span><h3>${safe(item.label)}</h3><p>${safe(words(item.id))}</p></article>`).join("");
}

async function showRecord(id) {
  const detail = document.querySelector("#archive-detail");
  if (!id) {
    detail.className = "prose-card empty-state";
    detail.textContent = "Select a record to inspect its hashes, origin, result summary, and scientific role.";
    return;
  }
  detail.className = "prose-card";
  detail.innerHTML = "Loading record…";
  try {
    const item = (await loadJson(`/api/integrations/research-archive/${encodeURIComponent(id)}`)).data;
    detail.innerHTML = `<span class="badge ${item.origin === "synthetic" ? "warning" : "research_only"}">${safe(words(item.record_type))}</span>
      <h2>${safe(item.title || item.archive_record_id)}</h2>
      <dl class="data-list"><div><dt>Source database</dt><dd>${safe(item.source_database)}</dd></div>
      <div><dt>Issue time</dt><dd>${safe(time(item.issue_time))}</dd></div>
      <div><dt>Origin</dt><dd>${safe(words(item.origin))}</dd></div>
      <div><dt>Research use</dt><dd>${item.research_only ? "Research only" : "Review required"}</dd></div>
      <div><dt>Input SHA-256</dt><dd class="mono">${safe(item.input_sha256 || "Not recorded")}</dd></div>
      <div><dt>Content SHA-256</dt><dd class="mono">${safe(item.content_sha256)}</dd></div></dl>
      <h3>Result summary</h3><pre>${safe(pretty(item.result))}</pre>`;
  } catch (error) {
    detail.className = "error-state";
    detail.textContent = error.message;
  }
}

function activateTab(name) {
  document.querySelectorAll(".workbench-panel").forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
  document.querySelectorAll(".workbench-tabs button").forEach(button => {
    if (button.dataset.tab === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

async function initialize() {
  const status = document.querySelector("#workbench-status");
  try {
    const [summary, archive, catalog, receipts, comparisons, icimod, gee] = await Promise.all([
      loadJson("/api/workbench/summary"),
      loadJson("/api/integrations/research-archive?limit=200"),
      loadJson("/api/integrations/catalog"),
      loadJson("/api/acquisition-receipts"),
      loadJson("/api/observation-comparisons"),
      loadJson("/api/icimod/datasets"),
      loadJson("/api/earth-engine/status")
    ]);
    state.summary = summary;
    state.archive = archive.data;
    state.catalog = catalog.data;
    state.receipts = receipts.data;
    state.comparisons = comparisons.data;
    state.icimod = icimod.data;
    state.gee = gee;
    renderSummary(); renderArchive(); renderSources(); renderReceipts(); renderComparisons(); renderScenarios();
    status.remove();
  } catch (error) {
    status.className = "error-state";
    status.textContent = `Unable to load the integrated workbench: ${error.message}`;
  }
}

document.querySelectorAll(".workbench-tabs button").forEach(button =>
  button.addEventListener("click", () => activateTab(button.dataset.tab)));
document.querySelector("#archive-record").addEventListener("change", event => showRecord(event.target.value));
initialize();
