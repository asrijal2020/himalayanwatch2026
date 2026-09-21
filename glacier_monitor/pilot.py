import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from .auth import error_response, require_access
from .db import get_db
from .services import freshness_limit_hours, parse_timestamp


bp = Blueprint("pilot", __name__)

CATCHMENT = "rasuwa-trishuli"
CATCHMENT_ASSETS = (
    "syabrubesi-hydromet-reference",
    "yala-aws",
    "yala-glacier",
    "langtang-glacier",
)
HAZARD_METRICS = {
    "flood": {"river_level_m", "precipitation_mm"},
    "lake_outburst": {"water_level_m", "precipitation_mm"},
    "glacier_movement": {"glacier_velocity_m_day"},
    "cascade": {
        "river_level_m",
        "precipitation_mm",
        "water_level_m",
        "glacier_velocity_m_day",
    },
}


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def serialize_evidence(row):
    item = dict(row)
    item["is_synthetic"] = bool(item["is_synthetic"])
    item["uncertainty"] = json.loads(item.pop("uncertainty_json"))
    item["quality_checks"] = json.loads(item.pop("quality_checks_json"))
    return item


def serialize_model_run(row):
    item = dict(row)
    item["input_evidence_ids"] = json.loads(item.pop("input_evidence_json"))
    item["output"] = json.loads(item.pop("output_json"))
    item["uncertainty"] = json.loads(item.pop("uncertainty_json"))
    item["feature_schema"] = json.loads(item.pop("feature_schema_json"))
    item["training_domain"] = json.loads(item.pop("training_domain_json"))
    item["research_only"] = bool(item["research_only"])
    return item


@bp.get("/pilot")
def pilot_page():
    return render_template("pilot.html")


@bp.get("/api/evidence")
def list_evidence():
    review_status = request.args.get("review_status")
    if review_status not in {None, "pending", "approved", "rejected"}:
        return error_response("review_status must be pending, approved, or rejected")
    limit_raw = request.args.get("limit", "100")
    try:
        limit = int(limit_raw)
    except ValueError:
        return error_response("limit must be an integer")
    if not 1 <= limit <= 500:
        return error_response("limit must be between 1 and 500")
    query = """
        SELECT evidence.*, assets.slug AS asset_slug, assets.name AS asset_name,
               assets.country
        FROM evidence_records evidence
        JOIN assets ON assets.id = evidence.asset_id
    """
    params = []
    if review_status:
        query += " WHERE evidence.review_status = ?"
        params.append(review_status)
    query += " ORDER BY evidence.observed_at DESC LIMIT ?"
    params.append(limit)
    rows = [serialize_evidence(row) for row in get_db().execute(query, params)]
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/evidence/<int:evidence_id>/review")
def review_evidence(evidence_id):
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    decision = payload.get("decision")
    reviewer = payload.get("reviewed_by")
    note = payload.get("note", "")
    if decision not in {"approved", "rejected"}:
        return error_response("decision must be approved or rejected")
    if not isinstance(reviewer, str) or not 2 <= len(reviewer.strip()) <= 100:
        return error_response("reviewed_by must be between 2 and 100 characters")
    if not isinstance(note, str) or len(note) > 1000:
        return error_response("note must be a string of at most 1000 characters")
    database = get_db()
    evidence = database.execute(
        "SELECT * FROM evidence_records WHERE id = ?", (evidence_id,)
    ).fetchone()
    if not evidence:
        return error_response("evidence record not found", 404)
    if decision == "approved" and evidence["quality_status"] != "validated":
        return error_response("only provider-validated evidence can be scientifically approved", 409)
    if decision == "approved" and evidence["is_synthetic"]:
        return error_response("synthetic evidence cannot be approved for operational use", 409)
    database.execute(
        """UPDATE evidence_records
           SET review_status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?
           WHERE id = ?""",
        (decision, utcnow().isoformat(), reviewer.strip(), note.strip(), evidence_id),
    )
    database.commit()
    updated = database.execute(
        """SELECT evidence.*, assets.slug AS asset_slug, assets.name AS asset_name,
                  assets.country
           FROM evidence_records evidence JOIN assets ON assets.id = evidence.asset_id
           WHERE evidence.id = ?""",
        (evidence_id,),
    ).fetchone()
    return jsonify({"data": serialize_evidence(updated)})


@bp.get("/api/model-runs")
def list_model_runs():
    rows = [
        serialize_model_run(row)
        for row in get_db().execute(
            "SELECT * FROM model_runs ORDER BY created_at DESC LIMIT 200"
        )
    ]
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/model-runs")
def create_model_run():
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    required = ("run_id", "model_name", "model_version", "hazard_type", "catchment",
                "input_evidence_ids", "created_by")
    missing = [field for field in required if field not in payload]
    if missing:
        return error_response(f"missing required field(s): {', '.join(missing)}")
    for field in ("run_id", "model_name", "model_version", "catchment", "created_by"):
        value = payload[field]
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 150:
            return error_response(f"{field} must be a string between 1 and 150 characters")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", payload["run_id"]):
        return error_response("run_id may contain only letters, numbers, dot, underscore, colon, and hyphen")
    hazard_type = payload["hazard_type"]
    if hazard_type not in HAZARD_METRICS:
        return error_response("hazard_type must be flood, lake_outburst, glacier_movement, or cascade")
    evidence_ids = payload["input_evidence_ids"]
    if (
        not isinstance(evidence_ids, list)
        or not 1 <= len(evidence_ids) <= 100
        or any(not isinstance(item, str) for item in evidence_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        return error_response("input_evidence_ids must contain 1-100 unique evidence IDs")
    output = payload.get("output", {})
    uncertainty = payload.get("uncertainty", {})
    feature_schema = payload.get("feature_schema", [])
    training_domain = payload.get("training_domain", {})
    if (
        not isinstance(output, dict)
        or not isinstance(uncertainty, dict)
        or not isinstance(feature_schema, list)
        or not all(isinstance(item, str) for item in feature_schema)
        or not isinstance(training_domain, dict)
    ):
        return error_response(
            "output, uncertainty, and training_domain must be JSON objects; "
            "feature_schema must be an array of strings"
        )
    for field in ("dataset_sha256", "protocol_sha256"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value)
        ):
            return error_response(f"{field} must be a hexadecimal SHA-256")
    fit_available_at = payload.get("fit_available_at")
    if fit_available_at is not None:
        try:
            fit_available_at = parse_timestamp(fit_available_at).isoformat()
        except ValueError as exc:
            return error_response(f"fit_available_at: {exc}")

    database = get_db()
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = database.execute(
        f"SELECT * FROM evidence_records WHERE evidence_id IN ({placeholders})",
        evidence_ids,
    ).fetchall()
    by_id = {row["evidence_id"]: row for row in rows}
    withholding_reasons = []
    if len(rows) != len(evidence_ids):
        withholding_reasons.append("one or more input evidence IDs do not exist")
    selected = [by_id[item] for item in evidence_ids if item in by_id]
    if any(row["review_status"] != "approved" for row in selected):
        withholding_reasons.append("all input evidence must have scientific approval")
    if any(row["is_synthetic"] for row in selected):
        withholding_reasons.append("synthetic evidence cannot support an operational model result")
    invalid_metrics = sorted(
        {row["metric"] for row in selected} - HAZARD_METRICS[hazard_type]
    )
    if invalid_metrics:
        withholding_reasons.append(
            f"metrics are not valid for {hazard_type}: {', '.join(invalid_metrics)}"
        )
    canonical_inputs = [
        {"evidence_id": item, "payload_sha256": by_id[item]["payload_sha256"]}
        for item in evidence_ids
        if item in by_id
    ]
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "model": payload["model_name"],
                "version": payload["model_version"],
                "hazard_type": hazard_type,
                "inputs": canonical_inputs,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    status = "withheld" if withholding_reasons else "candidate"
    reason = "; ".join(withholding_reasons) or None
    try:
        cursor = database.execute(
            """INSERT INTO model_runs
               (run_id, model_name, model_version, hazard_type, catchment, input_hash,
                input_evidence_json, output_json, uncertainty_json, status,
                withholding_reason, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["run_id"].strip(),
                payload["model_name"].strip(),
                payload["model_version"].strip(),
                hazard_type,
                payload["catchment"].strip(),
                input_hash,
                json.dumps(evidence_ids),
                json.dumps(output, sort_keys=True),
                json.dumps(uncertainty, sort_keys=True),
                status,
                reason,
                utcnow().isoformat(),
                payload["created_by"].strip(),
            ),
        )
        database.execute(
            """UPDATE model_runs
               SET artifact_schema = ?, dataset_sha256 = ?, protocol_sha256 = ?,
                   feature_schema_json = ?, station_id = ?, reference_datum = ?,
                   fit_available_at = ?, training_domain_json = ?, research_only = 1
               WHERE id = ?""",
            (
                payload.get("artifact_schema"),
                payload.get("dataset_sha256"),
                payload.get("protocol_sha256"),
                json.dumps(feature_schema),
                payload.get("station_id"),
                payload.get("reference_datum"),
                fit_available_at,
                json.dumps(training_domain, sort_keys=True),
                cursor.lastrowid,
            ),
        )
        database.commit()
    except sqlite3.IntegrityError:
        return error_response("run_id already exists", 409)
    row = database.execute("SELECT * FROM model_runs WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify({"data": serialize_model_run(row)}), 201


@bp.post("/api/model-runs/<int:model_run_id>/review")
def review_model_run(model_run_id):
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    decision = payload.get("decision")
    reviewer = payload.get("reviewed_by")
    note = payload.get("note", "")
    if decision not in {"accepted", "rejected"}:
        return error_response("decision must be accepted or rejected")
    if not isinstance(reviewer, str) or not 2 <= len(reviewer.strip()) <= 100:
        return error_response("reviewed_by must be between 2 and 100 characters")
    database = get_db()
    model_run = database.execute(
        "SELECT * FROM model_runs WHERE id = ?", (model_run_id,)
    ).fetchone()
    if not model_run:
        return error_response("model run not found", 404)
    if model_run["status"] == "withheld":
        return error_response("a withheld model run cannot be accepted or rejected as a forecast", 409)
    database.execute(
        """UPDATE model_runs SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?
           WHERE id = ?""",
        (decision, utcnow().isoformat(), reviewer.strip(), str(note)[:1000], model_run_id),
    )
    database.commit()
    updated = database.execute(
        "SELECT * FROM model_runs WHERE id = ?", (model_run_id,)
    ).fetchone()
    return jsonify({"data": serialize_model_run(updated)})


@bp.get("/api/pilot-readiness")
def pilot_readiness():
    database = get_db()
    placeholders = ",".join("?" for _ in CATCHMENT_ASSETS)
    assets = database.execute(
        f"SELECT * FROM assets WHERE slug IN ({placeholders}) ORDER BY asset_type, name",
        CATCHMENT_ASSETS,
    ).fetchall()
    now = utcnow()
    coverage = []
    fresh_count = 0
    for asset in assets:
        latest = database.execute(
            "SELECT * FROM readings WHERE asset_id = ? ORDER BY observed_at DESC LIMIT 1",
            (asset["id"],),
        ).fetchone()
        if not latest:
            coverage.append({
                "asset": asset["slug"],
                "name": asset["name"],
                "state": "missing",
                "reason": "No observation is available.",
            })
            continue
        age_hours = max(
            0, (now - parse_timestamp(latest["observed_at"])).total_seconds() / 3600
        )
        limit = freshness_limit_hours(asset, latest["metric"])
        state = "fresh" if age_hours <= limit else "stale"
        fresh_count += int(state == "fresh")
        coverage.append({
            "asset": asset["slug"],
            "name": asset["name"],
            "metric": latest["metric"],
            "age_hours": round(age_hours, 1),
            "freshness_limit_hours": limit,
            "state": state,
            "quality_status": latest["quality_status"],
            "is_synthetic": bool(latest["is_synthetic"]),
        })
    approved_evidence = database.execute(
        """SELECT COUNT(*) FROM evidence_records evidence
           JOIN assets ON assets.id = evidence.asset_id
           WHERE assets.slug IN ({})
             AND evidence.review_status = 'approved'
             AND evidence.is_synthetic = 0""".format(placeholders),
        CATCHMENT_ASSETS,
    ).fetchone()[0]
    accepted_models = database.execute(
        "SELECT COUNT(*) FROM model_runs WHERE catchment = ? AND status = 'accepted'",
        (CATCHMENT,),
    ).fetchone()[0]
    failed_deliveries = database.execute(
        "SELECT COUNT(*) FROM notification_deliveries WHERE status = 'failed'"
    ).fetchone()[0]
    gates = [
        {
            "name": "Approved partner observations",
            "state": "pass" if approved_evidence else "blocked",
            "detail": f"{approved_evidence} approved non-synthetic evidence record(s).",
        },
        {
            "name": "Catchment timing coverage",
            "state": "pass" if assets and fresh_count == len(assets) else "blocked",
            "detail": f"{fresh_count} of {len(assets)} pilot assets meet decision freshness limits.",
        },
        {
            "name": "Independently reviewed model",
            "state": "pass" if accepted_models else "blocked",
            "detail": f"{accepted_models} accepted model run(s); predictive skill is not implied.",
        },
        {
            "name": "Delivery failure queue",
            "state": "attention" if failed_deliveries else "pass",
            "detail": f"{failed_deliveries} failed delivery record(s).",
        },
    ]
    return jsonify({
        "catchment": CATCHMENT,
        "mode": "shadow_pilot",
        "public_warning_enabled": False,
        "overall_state": "not_ready",
        "coverage": coverage,
        "gates": gates,
        "limitations": [
            "No approved live Nepal DHM or partner telemetry contract is configured by default.",
            "Model acceptance records software review, not demonstrated predictive accuracy.",
            "Synthetic evidence and test recipients are isolated from operational delivery.",
            "Warning-time value requires community receipt and action exercises.",
        ],
    })
