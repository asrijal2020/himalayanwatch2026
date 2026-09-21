"use strict";

const gates = document.querySelector("#pilot-gates");
const coverage = document.querySelector("#pilot-coverage");
const evidence = document.querySelector("#pilot-evidence");
const models = document.querySelector("#pilot-models");
const integrations = document.querySelector("#integration-catalog");
const archiveSummary = document.querySelector("#archive-summary");
const acquisitionReceipts = document.querySelector("#acquisition-receipts");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[char]);
}

function badge(value) {
  return `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
}

function formatDate(value) {
  return value ? new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short"
  }).format(new Date(value)) : "Unavailable";
}

async function loadPilot() {
  try {
    const [
      readinessResponse,
      evidenceResponse,
      modelResponse,
      integrationResponse,
      importsResponse,
      receiptsResponse
    ] = await Promise.all([
      fetch("/api/pilot-readiness"),
      fetch("/api/evidence?limit=25"),
      fetch("/api/model-runs"),
      fetch("/api/integrations/catalog"),
      fetch("/api/integrations/imports"),
      fetch("/api/acquisition-receipts")
    ]);
    if (!readinessResponse.ok || !evidenceResponse.ok || !modelResponse.ok ||
        !integrationResponse.ok || !importsResponse.ok || !receiptsResponse.ok) {
      throw new Error("Unable to load shadow-pilot state.");
    }
    const readiness = await readinessResponse.json();
    const evidencePayload = await evidenceResponse.json();
    const modelPayload = await modelResponse.json();
    const integrationPayload = await integrationResponse.json();
    const importsPayload = await importsResponse.json();
    const receiptsPayload = await receiptsResponse.json();

    gates.innerHTML = readiness.gates.map(item => `
      <article class="pilot-card ${escapeHtml(item.state)}">
        ${badge(item.state)}
        <h3>${escapeHtml(item.name)}</h3>
        <p>${escapeHtml(item.detail)}</p>
      </article>`).join("");

    coverage.innerHTML = readiness.coverage.map(item => `
      <article class="pilot-card ${escapeHtml(item.state)}">
        ${badge(item.state)}
        <h3>${escapeHtml(item.name)}</h3>
        <p>${escapeHtml(item.metric || "No observation")}</p>
        <p>${item.age_hours == null ? escapeHtml(item.reason) :
          `${item.age_hours} hours old · limit ${item.freshness_limit_hours} hours`}</p>
        ${item.is_synthetic ? '<p class="synthetic-note">Synthetic exercise evidence</p>' : ""}
      </article>`).join("");

    evidence.innerHTML = evidencePayload.count ? evidencePayload.data.map(item => `
      <article class="pilot-row">
        <div>
          <div class="badges">${badge(item.review_status)} ${badge(item.quality_status)}
            ${item.is_synthetic ? badge("research_only") : ""}</div>
          <h3>${escapeHtml(item.asset_name)} · ${escapeHtml(item.metric)}</h3>
          <p>${escapeHtml(item.measurement_reference)}</p>
          <p>Observed ${formatDate(item.observed_at)} · available ${formatDate(item.available_at)}</p>
        </div>
        <div class="pilot-hash">
          <span>${escapeHtml(item.contract_version)}</span>
          <code>${escapeHtml(item.payload_sha256.slice(0, 16))}…</code>
        </div>
      </article>`).join("") :
      '<div class="loading-card">No source-event evidence receipts yet.</div>';

    models.innerHTML = modelPayload.count ? modelPayload.data.map(item => `
      <article class="pilot-row">
        <div>
          <div class="badges">${badge(item.status)} ${badge(item.hazard_type)}</div>
          <h3>${escapeHtml(item.model_name)} · ${escapeHtml(item.model_version)}</h3>
          <p>${escapeHtml(item.withholding_reason || "Candidate output awaiting scientific acceptance.")}</p>
        </div>
        <div class="pilot-hash"><span>${escapeHtml(item.run_id)}</span>
          <code>${escapeHtml(item.input_hash.slice(0, 16))}…</code></div>
      </article>`).join("") :
      '<div class="loading-card">No workbench model runs registered.</div>';

    archiveSummary.innerHTML = importsPayload.count ? importsPayload.data.map(item => `
      <article class="pilot-card">
        ${badge("research_only")}
        <h3>${escapeHtml(item.import_id)}</h3>
        <p>Commit <code>${escapeHtml(item.source_commit.slice(0, 12))}…</code></p>
        <p>${item.acquisition_receipt_count} receipts ·
          ${item.observation_comparison_count} comparison ·
          ${item.research_record_count} archived research records</p>
        <p>Backup created ${formatDate(item.backup_created_at)} · imported
          ${formatDate(item.imported_at)}</p>
      </article>`).join("") :
      '<div class="loading-card">No local research backup has been imported.</div>';

    acquisitionReceipts.innerHTML = receiptsPayload.count ? receiptsPayload.data.map(item => `
      <article class="pilot-row">
        <div>
          <div class="badges">${badge(item.eligibility_state)} ${badge(item.source_role)}
            ${badge(item.quality_status)}</div>
          <h3>${escapeHtml(item.source_id)} · ${escapeHtml(item.source_item_id)}</h3>
          <p>${escapeHtml(item.limitations.join(" "))}</p>
        </div>
        <div class="pilot-hash">
          <span>${escapeHtml(item.availability_basis)}</span>
          <code>${escapeHtml(item.content_sha256.slice(0, 16))}…</code>
        </div>
      </article>`).join("") :
      '<div class="loading-card">No acquisition receipts imported.</div>';

    integrations.innerHTML = integrationPayload.data.map(item => `
      <article class="pilot-card">
        ${badge(item.status)}
        <h3>${escapeHtml(item.provider)}</h3>
        <p>${escapeHtml(item.priority)} · ${escapeHtml(item.role)} ·
          ${escapeHtml(item.access)}</p>
        <p><strong>${escapeHtml(item.capability)}</strong></p>
        <p>${escapeHtml(item.integration)}</p>
        <p>${escapeHtml(item.limitation)}</p>
        ${item.endpoint ? `<a href="${escapeHtml(item.endpoint)}" target="_blank"
          rel="noopener noreferrer">Source endpoint ↗</a>` : ""}
      </article>`).join("");
  } catch (error) {
    gates.innerHTML = `<div class="error-state">${escapeHtml(error.message)}</div>`;
    coverage.innerHTML = "";
    evidence.innerHTML = "";
    models.innerHTML = "";
    integrations.innerHTML = "";
    archiveSummary.innerHTML = "";
    acquisitionReceipts.innerHTML = "";
  }
}

loadPilot();
