"use strict";

const map = L.map("map", { scrollWheelZoom: false }).setView([31.0, 84.0], 5);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(map);

const colors = { glacier: "#1677b8", lake: "#13a8a8", station: "#d96c27" };
const markers = L.layerGroup().addTo(map);
const eventLayer = L.layerGroup().addTo(map);
const grid = document.querySelector("#asset-grid");
const detail = document.querySelector("#asset-detail");
const loading = document.querySelector("#loading-state");
let assets = [];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

function badge(value) {
  return `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
}

function formatDate(value) {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "Unavailable";
}

function trendChart(readings) {
  if (!readings || readings.length < 2) return "<p>No trend series available.</p>";
  const values = readings.map(item => item.value);
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const points = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 300;
    const y = 120 - ((value - min) / range) * 100;
    return `${x},${y}`;
  }).join(" ");
  return `<div class="chart-wrap" role="img" aria-label="Reading trend from ${min.toFixed(2)} to ${max.toFixed(2)}">
    <svg viewBox="0 0 300 130" preserveAspectRatio="none">
      <polyline class="chart-line" points="${points}"></polyline>
    </svg></div>`;
}

async function showDetail(asset) {
  detail.innerHTML = '<div class="loading-card">Loading details…</div>';
  try {
    const response = await fetch(`/api/assets/${encodeURIComponent(asset.slug)}`);
    if (!response.ok) throw new Error("Unable to load asset details.");
    const payload = await response.json();
    const item = payload.data;
    const reading = item.latest_reading;
    const thresholds = item.asset_type === "lake"
      ? `<div><dt>Warning</dt><dd>${item.warning_threshold} m</dd></div>
         <div><dt>Critical</dt><dd>${item.critical_threshold} m</dd></div>
         <div><dt>Rapid rise</dt><dd>${item.rapid_rise_threshold} m/day</dd></div>` : "";
    detail.innerHTML = `
      <p class="eyebrow">${escapeHtml(item.asset_type)} · ${escapeHtml(item.country)}</p>
      <h2>${escapeHtml(item.name)}</h2>
      <p class="detail-meta">${item.latitude.toFixed(3)}, ${item.longitude.toFixed(3)}</p>
      <div class="badges">${badge(item.risk_level)} ${badge(item.freshness.state)} ${item.operational_status !== "not_applicable" ? badge(item.operational_status) : ""}</div>
      <p class="readiness-note"><strong>Decision state: ${escapeHtml(item.decision_readiness.state.replaceAll("_", " "))}.</strong>
        ${escapeHtml(item.decision_readiness.reason)}</p>
      ${reading ? `<div class="metric-card"><span>Latest ${escapeHtml(reading.metric.replaceAll("_", " "))}</span>
        <strong>${reading.value.toFixed(2)} ${escapeHtml(reading.units)}</strong>
        <small>Observed ${formatDate(reading.observed_at)} · available ${formatDate(reading.available_at)} · ${escapeHtml(reading.quality_status)}</small></div>` : ""}
      ${item.asset_type === "lake" ? `<h3>Water-level trend</h3>${trendChart(item.readings)}` : ""}
      <dl class="data-list">
        <div><dt>Area</dt><dd>${item.area_km2 ? `${item.area_km2} km²` : "Unavailable"}</dd></div>
        <div><dt>Elevation</dt><dd>${item.elevation_m ? `${item.elevation_m} m` : "Unavailable"}</dd></div>
        <div><dt>Trend / velocity</dt><dd>${item.trend_value ?? "See readings"} ${escapeHtml(item.trend_unit ?? "")}</dd></div>
        <div><dt>Measurement</dt><dd>${escapeHtml(item.measurement_kind ?? "N/A")}</dd></div>
        <div><dt>Device</dt><dd>${escapeHtml(item.device_type ?? "Satellite / catalog")}</dd></div>
        <div><dt>Operator</dt><dd>${escapeHtml(item.operator ?? "See source")}</dd></div>
        ${thresholds}
      </dl>
      <p class="synthetic-note"><strong>Data label:</strong> ${escapeHtml(item.data_classification)}</p>
      <p>${escapeHtml(item.notes ?? "")}</p>
      <p><a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">View public source ↗</a></p>`;
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    detail.innerHTML = `<div class="error-state">${escapeHtml(error.message)}</div>`;
  }
}

function render() {
  const country = document.querySelector("#country-filter").value;
  const type = document.querySelector("#type-filter").value;
  const risk = document.querySelector("#risk-filter").value;
  const visible = assets.filter(item =>
    (!country || item.country === country) &&
    (!type || item.asset_type === type) &&
    (!risk || item.risk_level === risk)
  );
  markers.clearLayers();
  grid.innerHTML = "";
  visible.forEach(item => {
    const marker = L.circleMarker([item.latitude, item.longitude], {
      radius: 8, color: "#fff", weight: 2, fillColor: colors[item.asset_type], fillOpacity: .95
    }).bindTooltip(item.name).addTo(markers);
    marker.on("click", () => showDetail(item));

    const card = document.createElement("article");
    card.className = "asset-card";
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.innerHTML = `${badge(item.asset_type)} ${badge(item.risk_level)}
      <h3>${escapeHtml(item.name)}</h3>
      <p>${escapeHtml(item.country)} · ${item.elevation_m ? `${item.elevation_m} m` : "elevation unavailable"}</p>
      <p>${escapeHtml(item.data_classification)}</p>`;
    card.addEventListener("click", () => showDetail(item));
    card.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showDetail(item); }
    });
    grid.appendChild(card);
  });
  document.querySelector("#asset-count").textContent = visible.length;
  loading.textContent = visible.length ? `${visible.length} records` : "No assets match these filters.";
}

async function load() {
  try {
    const [assetResponse, alertResponse, caseResponse, discoveryResponse] = await Promise.all([
      fetch("/api/assets"), fetch("/api/alerts?acknowledged=false"),
      fetch("/api/cases"), fetch("/api/discovery-status")
    ]);
    if (!assetResponse.ok || !alertResponse.ok || !caseResponse.ok || !discoveryResponse.ok) {
      throw new Error("Unable to load dashboard data.");
    }
    assets = (await assetResponse.json()).data;
    document.querySelector("#active-alert-count").textContent = (await alertResponse.json()).count;
    renderCases((await caseResponse.json()).data);
    renderDiscovery((await discoveryResponse.json()).data);
    render();
  } catch (error) {
    loading.textContent = error.message;
    grid.innerHTML = `<div class="error-state">Check the server and reload this page.</div>`;
  }

  function renderCases(cases) {
    eventLayer.clearLayers();
    cases.forEach(item => {
      const points = item.downstream_locations.map(location => [location.latitude, location.longitude]);
      if (points.length > 1) {
        L.polyline(points, { color: "#ba2d38", weight: 4, dashArray: "8 8", opacity: .9 })
          .bindTooltip(item.title, { className: "corridor-label" }).addTo(eventLayer);
      }
      item.downstream_locations.forEach(location => {
        L.circleMarker([location.latitude, location.longitude], {
          radius: 5, color: "#781f27", fillColor: "#fff", fillOpacity: 1, weight: 2
        }).bindPopup(`<strong>${escapeHtml(location.name)}</strong><br>${escapeHtml(location.exposure)}<br>
          <small>Retrospective corridor context, not a live forecast.</small>`).addTo(eventLayer);
      });
    });
  }

  function renderDiscovery(items) {
    const container = document.querySelector("#discovery-grid");
    container.innerHTML = items.map(item => `
      <article class="discovery-card ${escapeHtml(item.status)}">
        <div class="badges">${badge(item.status)}</div>
        <h3>${escapeHtml(item.provider)}</h3>
        <p><strong>${escapeHtml(item.access_scope)}</strong> · ${escapeHtml(item.evidence_type)}</p>
        <p>${escapeHtml(item.limitation)}</p>
        <a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">Official access point ↗</a>
      </article>`).join("");
  }
}

["country-filter", "type-filter", "risk-filter"].forEach(id =>
  document.querySelector(`#${id}`).addEventListener("change", render)
);
document.querySelector("#reset-filters").addEventListener("click", () => {
  ["country-filter", "type-filter", "risk-filter"].forEach(id => document.querySelector(`#${id}`).value = "");
  render();
});
load();
window.addEventListener("load", () => map.invalidateSize());
window.addEventListener("resize", () => map.invalidateSize());
