"use strict";

const explorerMap = L.map("imagery-map", { scrollWheelZoom: false }).setView([31.0, 84.0], 5);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(explorerMap);
const inventoryLayer = L.layerGroup().addTo(explorerMap);
const results = document.querySelector("#imagery-results");
const loadMore = document.querySelector("#imagery-load-more");
const initialGlacierId = document.querySelector("#imagery-filters").dataset.initialGlacier;
let glaciers = [];
let nextCursor = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
    .format(new Date(value));
}

function quicklook(item) {
  if (!item.thumbnail_url || item.thumbnail_kind === "none") {
    return `<div class="imagery-placeholder" role="img" aria-label="No thumbnail available">
      <span>NO QUICKLOOK</span><small>Metadata record only</small></div>`;
  }
  try {
    const host = new URL(item.thumbnail_url).hostname;
    if (!host.endsWith(".dataspace.copernicus.eu") && host !== "dataspace.copernicus.eu") {
      return `<div class="imagery-placeholder"><span>EXTERNAL PREVIEW</span><small>Open at source</small></div>`;
    }
  } catch {
    return `<div class="imagery-placeholder"><span>INVALID PREVIEW URL</span></div>`;
  }
  return `<img src="${escapeHtml(item.thumbnail_url)}" alt="Provider quicklook for ${escapeHtml(item.display_name)}"
    loading="lazy" referrerpolicy="no-referrer"
    onerror="this.replaceWith(Object.assign(document.createElement('div'), {className:'imagery-placeholder', textContent:'Quicklook unavailable'}))">`;
}

function renderGlacierSummary(glacier) {
  const panel = document.querySelector("#glacier-summary");
  panel.innerHTML = `<p class="eyebrow">${escapeHtml(glacier.country)} · ${escapeHtml(glacier.region)}</p>
    <h2>${escapeHtml(glacier.display_name)}</h2>
    <div class="badges"><span class="badge ${glacier.is_demo ? "warning" : "fresh"}">${glacier.is_demo ? "demo inventory" : "source inventory"}</span></div>
    <dl class="data-list">
      <div><dt>Inventory ID</dt><dd>${escapeHtml(glacier.inventory_id)}</dd></div>
      <div><dt>Catalog</dt><dd>${escapeHtml(glacier.catalog_version)}</dd></div>
      <div><dt>Latitude</dt><dd>${glacier.latitude.toFixed(4)}</dd></div>
      <div><dt>Longitude</dt><dd>${glacier.longitude.toFixed(4)}</dd></div>
    </dl>
    <p>${glacier.name ? "Named record from the configured catalog." : "No source name is available; the inventory ID is shown instead."}</p>
    <p><a href="${escapeHtml(glacier.source_url)}" target="_blank" rel="noopener noreferrer">Inventory source ↗</a></p>`;
}

function chooseGlacier(id, updateHistory = true) {
  const glacier = glaciers.find(item => item.inventory_id === id);
  if (!glacier) return;
  document.querySelector("#imagery-glacier").value = id;
  renderGlacierSummary(glacier);
  explorerMap.setView([glacier.latitude, glacier.longitude], 9);
  if (updateHistory) history.replaceState(null, "", `/imagery/glaciers/${encodeURIComponent(id)}`);
  loadImagery(false);
}

function renderInventory(payload) {
  glaciers = payload.data;
  const catalog = payload.catalog;
  document.querySelector("#catalog-notice").innerHTML = catalog
    ? `<strong>${escapeHtml(catalog.catalog_name)} · ${escapeHtml(catalog.catalog_version)}</strong>
       <span>${escapeHtml(catalog.spatial_boundary)} · ingested ${formatDate(catalog.ingested_at)}</span>
       <span>${escapeHtml(catalog.coverage_statement)}</span>`
    : "<strong>Catalog provenance unavailable.</strong>";
  const regions = [...new Set(glaciers.map(item => item.region))].sort();
  document.querySelector("#imagery-region").innerHTML =
    '<option value="">All regions</option>' + regions.map(value =>
      `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  document.querySelector("#imagery-glacier").innerHTML =
    '<option value="">All catalog glaciers</option>' + glaciers.map(item =>
      `<option value="${escapeHtml(item.inventory_id)}">${escapeHtml(item.display_name)} · ${escapeHtml(item.inventory_id)}</option>`).join("");
  inventoryLayer.clearLayers();
  glaciers.forEach(glacier => {
    L.circleMarker([glacier.latitude, glacier.longitude], {
      radius: 5, color: glacier.is_demo ? "#8b6320" : "#087f74",
      fillColor: glacier.is_demo ? "#f3b54a" : "#50cfc0", fillOpacity: .9, weight: 1
    }).bindTooltip(glacier.display_name).on("click", () => chooseGlacier(glacier.inventory_id))
      .addTo(inventoryLayer);
  });
  if (initialGlacierId) chooseGlacier(initialGlacierId, false);
}

function queryParams(cursor) {
  const params = new URLSearchParams({ limit: "24" });
  const values = {
    q: "#imagery-search", country: "#imagery-country", region: "#imagery-region",
    glacier_id: "#imagery-glacier", start_date: "#imagery-start",
    end_date: "#imagery-end", max_cloud_cover: "#imagery-cloud", source: "#imagery-source"
  };
  Object.entries(values).forEach(([key, selector]) => {
    const value = document.querySelector(selector).value.trim();
    if (value) params.set(key, value);
  });
  if (cursor !== null) params.set("cursor", cursor);
  return params;
}

async function loadImagery(append) {
  if (!append) {
    results.innerHTML = '<div class="loading-card">Loading imagery observations…</div>';
    nextCursor = null;
  }
  loadMore.disabled = true;
  try {
    const response = await fetch(`/api/glacier-imagery?${queryParams(append ? nextCursor : null)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Unable to load imagery.");
    document.querySelector("#imagery-total").textContent = payload.total;
    document.querySelector("#imagery-window").textContent =
      `${payload.window.start} through ${payload.window.end}`;
    ["#imagery-start", "#imagery-end"].forEach(selector => {
      document.querySelector(selector).min = payload.window.start;
      document.querySelector(selector).max = payload.window.end;
    });
    const cards = payload.data.map(item => `
      <article class="imagery-card">
        <div class="imagery-thumb">${quicklook(item)}
          <span class="imagery-label ${item.is_synthetic ? "synthetic" : "acquired"}">
            ${item.is_synthetic ? "SYNTHETIC METADATA" : "ACQUIRED IMAGERY"}
          </span>
        </div>
        <div class="imagery-card-body">
          <p class="eyebrow">${escapeHtml(item.country)} · ${escapeHtml(item.region)}</p>
          <h3><a href="/imagery/glaciers/${encodeURIComponent(item.glacier_id)}">${escapeHtml(item.display_name)}</a></h3>
          <p class="mono">${escapeHtml(item.glacier_id)} · ${escapeHtml(item.provider_item_id)}</p>
          <dl class="imagery-metadata">
            <div><dt>Acquired</dt><dd>${formatDate(item.acquired_at)}</dd></div>
            <div><dt>Available</dt><dd>${formatDate(item.available_at)}</dd></div>
            <div><dt>Cloud</dt><dd>${item.cloud_cover === null ? "Not reported" : `${item.cloud_cover.toFixed(0)}%`}</dd></div>
            <div><dt>Processing</dt><dd>${escapeHtml(item.processing_level)}</dd></div>
            <div><dt>Quality</dt><dd>${escapeHtml(item.quality_status)}</dd></div>
            <div><dt>Preview</dt><dd>${escapeHtml(item.thumbnail_kind.replaceAll("_", " "))}</dd></div>
          </dl>
          <p class="source-label">${escapeHtml(item.source)}</p>
          <a href="${escapeHtml(item.external_url)}" target="_blank" rel="noopener noreferrer">Open metadata source ↗</a>
        </div>
      </article>`).join("");
    if (append) results.insertAdjacentHTML("beforeend", cards);
    else results.innerHTML = cards || '<div class="loading-card">No observations match these filters in the rolling three-month window.</div>';
    nextCursor = payload.next_cursor;
    loadMore.hidden = nextCursor === null;
  } catch (error) {
    if (!append) results.innerHTML = `<div class="error-state">${escapeHtml(error.message)}</div>`;
  } finally {
    loadMore.disabled = false;
  }
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/glaciers?limit=100");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Unable to load glacier catalog.");
    renderInventory(payload);
  } catch (error) {
    document.querySelector("#catalog-notice").innerHTML = `<strong>${escapeHtml(error.message)}</strong>`;
  }
}

document.querySelector("#imagery-filters").addEventListener("submit", event => {
  event.preventDefault(); loadImagery(false);
});
document.querySelector("#imagery-glacier").addEventListener("change", event => {
  if (event.target.value) chooseGlacier(event.target.value);
  else { history.replaceState(null, "", "/imagery"); loadImagery(false); }
});
document.querySelector("#imagery-reset").addEventListener("click", () => {
  document.querySelector("#imagery-filters").reset();
  const inputs = ["#imagery-start", "#imagery-end"];
  inputs.forEach(selector => document.querySelector(selector).value = "");
  history.replaceState(null, "", "/imagery");
  loadImagery(false);
});
loadMore.addEventListener("click", () => loadImagery(true));

loadCatalog().then(() => {
  if (!initialGlacierId) loadImagery(false);
});
window.addEventListener("load", () => explorerMap.invalidateSize());
window.addEventListener("resize", () => explorerMap.invalidateSize());
