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

function formatUtcDate(value) {
  return value ? `${new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC"
  }).format(new Date(value))} UTC` : "Unavailable";
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

function weatherChart(days) {
  if (!days || days.length < 2) return "<p>No two-week weather series is stored yet.</p>";
  const valid = days.map((item, index) => ({ ...item, index }))
    .filter(item => item.temperature_mean_c != null);
  if (valid.length < 2) return "<p>Temperature history is incomplete.</p>";
  const width = 360, height = 210;
  const margin = { left: 48, right: 12, top: 14, bottom: 44 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const temperatures = valid.map(item => item.temperature_mean_c);
  let yMin = Math.floor(Math.min(...temperatures));
  let yMax = Math.ceil(Math.max(...temperatures));
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const yRange = yMax - yMin;
  const x = index => margin.left + (index / (days.length - 1)) * plotWidth;
  const y = value => margin.top + (1 - (value - yMin) / yRange) * plotHeight;
  const points = valid.map(item => `${x(item.index).toFixed(1)},${y(item.temperature_mean_c).toFixed(1)}`).join(" ");
  const yTicks = Array.from({ length: 5 }, (_, index) => yMin + (yRange * index / 4));
  const xTickIndices = [...new Set([0, Math.round((days.length - 1) / 3),
    Math.round((days.length - 1) * 2 / 3), days.length - 1])];
  const gridLines = yTicks.map(value => `<g>
    <line class="weather-grid-line" x1="${margin.left}" y1="${y(value)}" x2="${width - margin.right}" y2="${y(value)}"></line>
    <text class="weather-axis-label" x="${margin.left - 7}" y="${y(value) + 4}" text-anchor="end">${value.toFixed(1)}</text>
  </g>`).join("");
  const dateTicks = xTickIndices.map(index => {
    const label = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
      .format(new Date(`${days[index].date}T00:00:00Z`));
    return `<g><line class="weather-axis-tick" x1="${x(index)}" y1="${height - margin.bottom}" x2="${x(index)}" y2="${height - margin.bottom + 5}"></line>
      <text class="weather-axis-label" x="${x(index)}" y="${height - 20}" text-anchor="middle">${escapeHtml(label)}</text></g>`;
  }).join("");
  const circles = valid.map(item => `<circle class="weather-point" cx="${x(item.index)}" cy="${y(item.temperature_mean_c)}" r="3.5" tabindex="0">
    <title>${escapeHtml(item.date)}: ${item.temperature_mean_c.toFixed(1)} °C daily mean from ${item.hours} hourly samples</title>
  </circle>`).join("");
  return `<figure class="weather-chart">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="weather-chart-title weather-chart-desc">
      <title id="weather-chart-title">Daily modeled mean temperature</title>
      <desc id="weather-chart-desc">UTC dates on the horizontal axis and degrees Celsius on the vertical axis. Values range from ${Math.min(...temperatures).toFixed(1)} to ${Math.max(...temperatures).toFixed(1)} degrees Celsius.</desc>
      ${gridLines}
      <line class="weather-axis" x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}"></line>
      <line class="weather-axis" x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}"></line>
      ${dateTicks}
      <text class="weather-axis-title" x="13" y="${margin.top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 13 ${margin.top + plotHeight / 2})">Mean temperature (°C)</text>
      <text class="weather-axis-title" x="${margin.left + plotWidth / 2}" y="${height - 3}" text-anchor="middle">Date (UTC)</text>
      <polyline class="chart-line weather-line" points="${points}"></polyline>
      ${circles}
    </svg>
    <figcaption>Each point is one UTC calendar day’s mean of the available hourly modeled temperatures. Hover or focus a point for its date, value, and sample count.</figcaption>
  </figure>`;
}

function weatherPanel(weather) {
  if (!weather.count) {
    return `<section class="asset-weather"><h3>Weather · last 14 days</h3>
      <div class="empty-state compact">No weather has been refreshed for this asset.
      Run <code>python -m flask --app app refresh-weather --asset ${escapeHtml(weather.asset.slug)}</code>.</div></section>`;
  }
  const latest = weather.latest;
  const basis = weather.collection_basis;
  const precipitation = weather.daily.reduce((sum, day) => sum + day.precipitation_total_mm, 0);
  const snowfall = weather.daily.reduce((sum, day) => sum + day.snowfall_total_cm, 0);
  return `<section class="asset-weather">
    <div class="section-heading"><h3>Weather · last 14 days</h3>${badge(weather.data_classification)}</div>
    ${weatherChart(weather.daily)}
    <div class="weather-basis">
      <strong>Collection basis</strong>
      <span>${escapeHtml(basis.provider)} hourly gridded model · rolling ${weather.period_days}-day window · UTC</span>
      <span>Grid ${basis.grid_latitude.toFixed(3)}, ${basis.grid_longitude.toFixed(3)} at ${basis.grid_elevation_m == null ? "unknown elevation" : `${basis.grid_elevation_m.toFixed(0)} m`} · ${escapeHtml(formatUtcDate(weather.period_start))} to ${escapeHtml(formatUtcDate(weather.period_end))}</span>
      <span>Daily line = arithmetic mean of available hourly temperatures. The first and current UTC dates may be partial days.</span>
    </div>
    <div class="weather-metrics">
      <div><span>Latest temperature</span><strong>${latest.temperature_c == null ? "—" : `${latest.temperature_c.toFixed(1)} °C`}</strong></div>
      <div><span>Latest humidity</span><strong>${latest.relative_humidity_pct == null ? "—" : `${latest.relative_humidity_pct.toFixed(0)}%`}</strong></div>
      <div><span>14-day precipitation</span><strong>${precipitation.toFixed(1)} mm</strong></div>
      <div><span>14-day snowfall</span><strong>${snowfall.toFixed(1)} cm</strong></div>
      <div><span>Maximum wind</span><strong>${Math.max(...weather.daily.map(day => day.wind_speed_max_kmh || 0)).toFixed(1)} km/h</strong></div>
      <div><span>Hourly samples</span><strong>${weather.count}</strong></div>
    </div>
    <details><summary>Daily model-ready weather features</summary>
      <div class="weather-table"><table><thead><tr><th>Date</th><th>Temperature °C</th><th>Precipitation</th><th>Snowfall</th><th>Humidity</th><th>Wind max</th></tr></thead>
      <tbody>${weather.daily.map(day => `<tr><td>${escapeHtml(day.date)}</td>
        <td>${day.temperature_min_c?.toFixed(1) ?? "—"} / ${day.temperature_mean_c?.toFixed(1) ?? "—"} / ${day.temperature_max_c?.toFixed(1) ?? "—"}</td>
        <td>${day.precipitation_total_mm.toFixed(1)} mm</td><td>${day.snowfall_total_cm.toFixed(1)} cm</td>
        <td>${day.relative_humidity_mean_pct?.toFixed(0) ?? "—"}%</td><td>${day.wind_speed_max_kmh?.toFixed(1) ?? "—"} km/h</td></tr>`).join("")}</tbody></table></div>
    </details>
    <p class="readiness-note"><strong>Modeling boundary:</strong> ${escapeHtml(weather.interpretation)}
      Define the target event, prediction horizon, event-family split, probability calibration, and held-out evaluation before displaying any risk probability.</p>
    <p><a href="${escapeHtml(latest.source_url)}" target="_blank" rel="noopener noreferrer">Open-Meteo request and provenance ↗</a></p>
  </section>`;
}

function landCoverPanel(landCover) {
  if (!landCover.available) {
    return `<section class="asset-weather"><h3>ICIMOD land cover</h3>
      <div class="empty-state compact">${escapeHtml(landCover.message)}</div></section>`;
  }
  const changes = landCover.data.filter((item, index, records) =>
    index === 0 || item.properties.class_value !== records[index - 1].properties.class_value
  );
  return `<section class="asset-weather">
    <div class="section-heading"><h3>ICIMOD annual land cover</h3>${badge("satellite derived")}</div>
    <p>${escapeHtml(landCover.message)} The source has 22 annual layers; 2012 is explicitly absent.</p>
    <div class="weather-table"><table><thead><tr><th>Year</th><th>Class</th><th>Code</th></tr></thead>
      <tbody>${changes.map(item => `<tr><td>${escapeHtml(item.properties.year)}</td>
        <td>${escapeHtml(item.properties.class_name)}</td><td>${escapeHtml(item.properties.class_value ?? "—")}</td></tr>`).join("")}</tbody>
    </table></div>
    <p><small>Rows show the first year and each subsequent class change at this monitored coordinate. All ${landCover.count} annual samples remain available through the API.</small></p>
    <p><a href="${escapeHtml(landCover.dataset.doi)}" target="_blank" rel="noopener noreferrer">ICIMOD dataset and provenance ↗</a></p>
  </section>`;
}

async function showDetail(asset) {
  detail.innerHTML = '<div class="loading-card">Loading details…</div>';
  try {
    const [response, weatherResponse, landCoverResponse] = await Promise.all([
      fetch(`/api/assets/${encodeURIComponent(asset.slug)}`),
      fetch(`/api/assets/${encodeURIComponent(asset.slug)}/weather?days=14`),
      fetch(`/api/assets/${encodeURIComponent(asset.slug)}/land-cover`)
    ]);
    if (!response.ok || !weatherResponse.ok || !landCoverResponse.ok) throw new Error("Unable to load asset details.");
    const payload = await response.json();
    const weather = (await weatherResponse.json()).data;
    const landCover = await landCoverResponse.json();
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
      <p><a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">View public source ↗</a></p>
      ${weatherPanel(weather)}
      ${landCoverPanel(landCover)}`;
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
