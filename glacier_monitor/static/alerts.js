"use strict";

const list = document.querySelector("#alerts-list");
const filter = document.querySelector("#alert-filter");
const authorizedBy = document.querySelector("#authorized-by");
const operatorKey = document.querySelector("#operator-key");
const deliveryList = document.querySelector("#delivery-list");
const recipientForm = document.querySelector("#recipient-form");
const recipientName = document.querySelector("#recipient-name");
const recipientChannel = document.querySelector("#recipient-channel");
const recipientCountry = document.querySelector("#recipient-country");
const recipientSeverity = document.querySelector("#recipient-severity");
const recipientDestination = document.querySelector("#recipient-destination");
const recipientStatus = document.querySelector("#recipient-status");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

async function acknowledge(id) {
  const response = await fetch(`/api/alerts/${id}/acknowledge`, {
    method: "POST",
    headers: operatorHeaders()
  });
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.error?.message || "Could not acknowledge alert.");
  }
  await loadAlerts();
}

function operatorHeaders(json = false) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  if (operatorKey.value) headers["X-API-Key"] = operatorKey.value;
  return headers;
}

function requireOperatorKey() {
  if (!operatorKey.required || operatorKey.value.trim()) return;
  operatorKey.focus();
  throw new Error("Enter the Operator API key at the top of the page.");
}

async function authorize(id) {
  requireOperatorKey();
  const actor = authorizedBy.value.trim();
  if (actor.length < 2) throw new Error("Enter the authorized operator name or duty role.");
  const response = await fetch(`/api/alerts/${id}/authorize`, {
    method: "POST",
    headers: operatorHeaders(true),
    body: JSON.stringify({ authorized_by: actor })
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || "Could not authorize notification.");
  await Promise.all([loadAlerts(), loadDeliveries()]);
}

async function loadDeliveries() {
  try {
    const response = await fetch("/api/notification-deliveries");
    if (!response.ok) throw new Error("Unable to load delivery status.");
    const payload = await response.json();
    deliveryList.innerHTML = payload.count ? payload.data.map(item => `
      <article class="alert-card">
        <div class="alert-icon" aria-hidden="true">→</div>
        <div>
          <h3>${escapeHtml(item.subscription_name)} · ${escapeHtml(item.channel)}</h3>
          <p>${escapeHtml(item.destination)} · alert ${item.alert_id} · attempts ${item.attempts}</p>
        </div>
        <span class="badge ${item.status === "sent" ? "low" : "warning"}">${escapeHtml(item.status)}</span>
      </article>`).join("") : '<div class="loading-card">No authorized deliveries queued.</div>';
  } catch (error) {
    deliveryList.innerHTML = `<div class="error-state">${escapeHtml(error.message)}</div>`;
  }
}

async function addRecipients(event) {
  event.preventDefault();
  requireOperatorKey();
  const destinations = recipientDestination.value
    .split(/[,\n]/)
    .map(value => value.trim())
    .filter(Boolean);
  if (!destinations.length) {
    recipientStatus.textContent = "Enter at least one recipient.";
    return;
  }
  recipientStatus.textContent = `Adding ${destinations.length} recipient(s)…`;
  const results = await Promise.all(destinations.map(async (destination, index) => {
    const response = await fetch("/api/notification-subscriptions", {
      method: "POST",
      headers: operatorHeaders(true),
      body: JSON.stringify({
        name: destinations.length === 1
          ? recipientName.value.trim()
          : `${recipientName.value.trim()} ${index + 1}`,
        channel: recipientChannel.value,
        destination,
        country: recipientCountry.value || null,
        minimum_severity: recipientSeverity.value
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(`${destination}: ${payload.error?.message || "failed"}`);
    return payload.data;
  }));
  recipientDestination.value = "";
  recipientStatus.textContent = `Added ${results.length} recipient(s).`;
}

async function loadAlerts() {
  list.innerHTML = '<div class="loading-card">Loading alerts…</div>';
  try {
    const query = filter.value ? `?acknowledged=${filter.value}` : "";
    const response = await fetch(`/api/alerts${query}`);
    if (!response.ok) throw new Error("Unable to load alerts.");
    const payload = await response.json();
    if (!payload.count) {
      list.innerHTML = '<div class="loading-card">No alerts in this state.</div>';
      return;
    }
    list.innerHTML = payload.data.map(item => {
      const eligible = item.evidence_quality === "validated" && !item.evidence_is_synthetic;
      const authorization = item.authorized_at
        ? `<span class="badge low">Authorized</span>`
        : eligible
          ? `<button class="authorize" data-authorize-id="${item.id}">Authorize notification</button>`
          : `<span class="badge">Notification withheld</span>`;
      return `
      <article class="alert-card ${escapeHtml(item.severity)}">
        <div class="alert-icon" aria-hidden="true">!</div>
        <div><div class="badges">${`<span class="badge ${escapeHtml(item.severity)}">${escapeHtml(item.severity)}</span>`}
          <span class="badge">${escapeHtml(item.alert_type.replaceAll("_", " "))}</span></div>
          <h2>${escapeHtml(item.asset_name)} · ${escapeHtml(item.country)}</h2>
          <p>${escapeHtml(item.reason)}</p>
          <p>${new Date(item.created_at).toLocaleString()} · ${item.latitude.toFixed(3)}, ${item.longitude.toFixed(3)}</p>
          <p>Evidence: ${escapeHtml(item.evidence_quality || "unknown")} · ${item.evidence_is_synthetic ? "synthetic/demo" : "non-synthetic"}</p>
        </div>
        <div class="alert-actions">
          ${authorization}
          ${item.acknowledged_at ? `<span class="badge low">Acknowledged</span>` : `<button data-id="${item.id}">Acknowledge</button>`}
        </div>
      </article>`;
    }).join("");
    list.querySelectorAll("button[data-id]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try { await acknowledge(button.dataset.id); }
      catch (error) { button.disabled = false; window.alert(error.message); }
    }));
    list.querySelectorAll("button[data-authorize-id]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try { await authorize(button.dataset.authorizeId); }
      catch (error) { button.disabled = false; window.alert(error.message); }
    }));
  } catch (error) {
    list.innerHTML = `<div class="error-state">${escapeHtml(error.message)}</div>`;
  }
}

filter.addEventListener("change", loadAlerts);
recipientForm.addEventListener("submit", event => {
  addRecipients(event).catch(error => {
    recipientStatus.textContent = error.message;
  });
});
Promise.all([loadAlerts(), loadDeliveries()]);
