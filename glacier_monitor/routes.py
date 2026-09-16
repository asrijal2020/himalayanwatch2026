import hmac
import json
import re
import sqlite3

from flask import Blueprint, current_app, jsonify, render_template, request

from .db import get_db
from .integrations import process_event
from .notifications import authorize_alert, mask_destination
from .providers import ADAPTERS
from .source_adapters import ADAPTER_STATUS, validate_public_https_url
from .services import evaluate_asset, serialize_asset, validate_reading

bp = Blueprint("main", __name__)


def error_response(message, status=400):
    return jsonify({"error": {"message": message, "status": status}}), status


def require_write_access(scope="operator"):
    key_name = "INGEST_API_KEY" if scope == "ingest" else "OPERATOR_API_KEY"
    expected = current_app.config.get(key_name) or current_app.config.get("WRITE_API_KEY", "")
    if expected and not hmac.compare_digest(request.headers.get("X-API-Key", ""), expected):
        return error_response("missing or invalid X-API-Key", 401)
    return None


@bp.get("/")
def dashboard():
    return render_template("dashboard.html")


@bp.get("/healthz")
def health():
    get_db().execute("SELECT 1").fetchone()
    return jsonify({"status": "ok"})


@bp.get("/alerts")
def alerts_page():
    return render_template(
        "alerts.html",
        operator_auth_required=bool(
            current_app.config.get("OPERATOR_API_KEY")
            or current_app.config.get("WRITE_API_KEY")
        ),
    )


@bp.get("/about")
def about():
    return render_template("about.html", adapters=ADAPTERS, source_adapters=ADAPTER_STATUS)


@bp.get("/api/assets")
def list_assets():
    database = get_db()
    clauses = []
    params = []
    filters = {
        "country": {"Nepal", "Tibet", "India", "Pakistan"},
        "asset_type": {"glacier", "lake", "station"},
        "risk_level": {"low", "moderate", "high", "critical", "unknown"},
        "operational_status": {"online", "offline", "unknown", "not_applicable"},
    }
    for key, allowed in filters.items():
        value = request.args.get(key)
        if value:
            if value not in allowed:
                return error_response(f"invalid {key}; expected one of {', '.join(sorted(allowed))}")
            clauses.append(f"{key} = ?")
            params.append(value)
    query = "SELECT * FROM assets"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY country, asset_type, name"
    assets = [serialize_asset(database, row) for row in database.execute(query, params)]
    return jsonify({"data": assets, "count": len(assets), "demo_mode": True})


@bp.get("/api/assets/<slug>")
def get_asset(slug):
    database = get_db()
    asset = database.execute("SELECT * FROM assets WHERE slug = ?", (slug,)).fetchone()
    if not asset:
        return error_response("asset not found", 404)
    result = serialize_asset(database, asset)
    result["readings"] = [
        dict(row) for row in database.execute(
            "SELECT * FROM readings WHERE asset_id = ? ORDER BY observed_at",
            (asset["id"],),
        ).fetchall()
    ]
    return jsonify({"data": result})


@bp.get("/api/readings")
def list_readings():
    database = get_db()
    slug = request.args.get("asset")
    if not slug:
        return error_response("asset query parameter is required")
    asset = database.execute("SELECT id FROM assets WHERE slug = ?", (slug,)).fetchone()
    if not asset:
        return error_response("asset not found", 404)
    limit = request.args.get("limit", "100")
    try:
        limit = int(limit)
    except ValueError:
        return error_response("limit must be an integer")
    if not 1 <= limit <= 500:
        return error_response("limit must be between 1 and 500")
    rows = database.execute(
        """SELECT id, asset_id, metric, value, units, observed_at, available_at,
                  quality_status, source_url, is_synthetic, metadata_json, created_at
           FROM readings WHERE asset_id = ? ORDER BY observed_at DESC LIMIT ?""",
        (asset["id"], limit),
    ).fetchall()
    data = []
    for row in rows:
        item = dict(row)
        item["is_synthetic"] = bool(item["is_synthetic"])
        item["metadata"] = json.loads(item.pop("metadata_json"))
        data.append(item)
    return jsonify({"data": data, "count": len(data)})


@bp.post("/api/readings")
def post_reading():
    auth_error = require_write_access("ingest")
    if auth_error:
        return auth_error
    if not request.is_json:
        return error_response("Content-Type must be application/json", 415)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    slug = payload.get("asset")
    if not slug:
        return error_response("missing required field: asset")
    database = get_db()
    asset = database.execute("SELECT * FROM assets WHERE slug = ?", (slug,)).fetchone()
    if not asset:
        return error_response("asset not found", 404)
    try:
        reading = validate_reading(payload, asset)
    except ValueError as exc:
        return error_response(str(exc))
    cursor = database.execute(
        """INSERT INTO readings
           (asset_id, metric, value, units, observed_at, available_at, quality_status,
            source_url, is_synthetic, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset["id"], *reading.values()),
    )
    database.execute(
        "UPDATE assets SET observation_time = ?, operational_status = CASE "
        "WHEN asset_type = 'station' THEN 'online' ELSE operational_status END WHERE id = ?",
        (reading["observed_at"], asset["id"]),
    )
    updated_asset = database.execute(
        "SELECT * FROM assets WHERE id = ?", (asset["id"],)
    ).fetchone()
    evaluate_asset(database, updated_asset)
    database.commit()
    created = database.execute(
        "SELECT * FROM readings WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return jsonify({"data": dict(created)}), 201


@bp.get("/api/cases")
def list_cases():
    database = get_db()
    cases = []
    for row in database.execute("SELECT * FROM hazard_cases ORDER BY occurred_at DESC"):
        item = dict(row)
        item["downstream_locations"] = [
            dict(location) for location in database.execute(
                """SELECT name, district, latitude, longitude, exposure, sequence_order
                   FROM downstream_locations WHERE case_id = ? ORDER BY sequence_order""",
                (row["id"],),
            )
        ]
        item["forecast_baseline"] = {
            "model": "Persistence / linear river-level trend baseline",
            "state": "withheld",
            "reason": (
                "No validated, source-area river telemetry with known availability time "
                "is connected for this retrospective case."
            ),
            "evaluation": "Historical replay required against event and non-event periods.",
        }
        cases.append(item)
    return jsonify({"data": cases, "count": len(cases)})


@bp.get("/api/discovery-status")
def discovery_status():
    rows = [dict(row) for row in get_db().execute(
        "SELECT * FROM discovery_checks ORDER BY provider"
    )]
    return jsonify({
        "data": rows,
        "count": len(rows),
        "interpretation": (
            "Discovery confirms access to records or metadata only. "
            "It does not establish validated hazard detection."
        ),
    })


@bp.get("/api/alerts")
def list_alerts():
    acknowledged = request.args.get("acknowledged")
    if acknowledged not in (None, "true", "false"):
        return error_response("acknowledged must be true or false")
    query = """
        SELECT alerts.*, assets.slug AS asset_slug, assets.name AS asset_name,
               assets.latitude, assets.longitude, assets.country,
               (SELECT readings.quality_status FROM readings
                WHERE readings.id = COALESCE(
                    alerts.evidence_reading_id,
                    (SELECT fallback.id FROM readings fallback
                     WHERE fallback.asset_id = alerts.asset_id
                     ORDER BY fallback.observed_at DESC LIMIT 1)
                )) AS evidence_quality,
               (SELECT readings.is_synthetic FROM readings
                WHERE readings.id = COALESCE(
                    alerts.evidence_reading_id,
                    (SELECT fallback.id FROM readings fallback
                     WHERE fallback.asset_id = alerts.asset_id
                     ORDER BY fallback.observed_at DESC LIMIT 1)
                )) AS evidence_is_synthetic
        FROM alerts JOIN assets ON assets.id = alerts.asset_id
    """
    params = []
    if acknowledged == "true":
        query += " WHERE acknowledged_at IS NOT NULL"
    elif acknowledged == "false":
        query += " WHERE acknowledged_at IS NULL"
    query += " ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, created_at DESC"
    rows = [dict(row) for row in get_db().execute(query, params)]
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/source-events")
def post_source_event():
    auth_error = require_write_access("ingest")
    if auth_error:
        return auth_error
    if not request.is_json:
        return error_response("Content-Type must be application/json", 415)
    try:
        result = process_event(request.get_json(silent=True))
    except ValueError as exc:
        return error_response(str(exc))
    except RuntimeError as exc:
        current_app.logger.error("Source event processing failed: %s", exc)
        return error_response(str(exc), 503)
    return jsonify({"data": result}), 202 if result["status"] == "accepted" else 201


@bp.get("/api/source-events")
def list_source_events():
    limit = request.args.get("limit", "100")
    try:
        limit = int(limit)
    except ValueError:
        return error_response("limit must be an integer")
    if not 1 <= limit <= 500:
        return error_response("limit must be between 1 and 500")
    rows = [
        dict(row)
        for row in get_db().execute(
            """SELECT event_id, provider, asset_slug, event_type, received_at,
                      processed_at, reading_id, status, error_message
               FROM source_events ORDER BY received_at DESC LIMIT ?""",
            (limit,),
        )
    ]
    return jsonify({"data": rows, "count": len(rows)})


@bp.get("/api/notification-subscriptions")
def list_notification_subscriptions():
    rows = []
    for row in get_db().execute(
        """SELECT id, name, channel, destination, country, minimum_severity,
                  enabled, created_at
           FROM notification_subscriptions ORDER BY name"""
    ):
        item = dict(row)
        item["destination"] = mask_destination(item["channel"], item["destination"])
        item["enabled"] = bool(item["enabled"])
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/notification-subscriptions")
def create_notification_subscription():
    auth_error = require_write_access()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    name = payload.get("name")
    channel = payload.get("channel")
    destination = payload.get("destination")
    country = payload.get("country")
    minimum_severity = payload.get("minimum_severity", "critical")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        return error_response("name must be between 1 and 100 characters")
    if channel not in {"sms", "email", "webhook"}:
        return error_response("channel must be sms, email, or webhook")
    if not isinstance(destination, str) or len(destination) > 500:
        return error_response("destination must be a string of at most 500 characters")
    if channel == "sms" and not re.fullmatch(r"\+[1-9]\d{7,14}", destination):
        return error_response("SMS destination must use E.164 format")
    if channel == "email" and not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        destination,
    ):
        return error_response("email destination must be a valid email address")
    if channel == "webhook":
        try:
            validate_public_https_url(destination)
        except ValueError as exc:
            return error_response(str(exc))
    if country not in {None, "Nepal", "Tibet", "India", "Pakistan"}:
        return error_response("country must be Nepal, Tibet, India, Pakistan, or null")
    if minimum_severity not in {"warning", "critical"}:
        return error_response("minimum_severity must be warning or critical")
    database = get_db()
    duplicate = database.execute(
        """SELECT id FROM notification_subscriptions
           WHERE channel = ? AND destination = ? AND country IS ?""",
        (channel, destination, country),
    ).fetchone()
    if duplicate:
        return error_response("an equivalent notification subscription already exists", 409)
    try:
        cursor = database.execute(
            """INSERT INTO notification_subscriptions
               (name, channel, destination, country, minimum_severity, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, 1, datetime('now'))""",
            (name.strip(), channel, destination, country, minimum_severity),
        )
        database.commit()
    except sqlite3.IntegrityError:
        return error_response("an equivalent notification subscription already exists", 409)
    return jsonify(
        {
            "data": {
                "id": cursor.lastrowid,
                "name": name.strip(),
                "channel": channel,
                "destination": mask_destination(channel, destination),
                "country": country,
                "minimum_severity": minimum_severity,
                "enabled": True,
            }
        }
    ), 201


@bp.post("/api/alerts/<int:alert_id>/authorize")
def authorize_alert_notification(alert_id):
    auth_error = require_write_access()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("request body must be a JSON object")
    actor = payload.get("authorized_by")
    note = payload.get("note", "")
    if not isinstance(actor, str) or not 2 <= len(actor.strip()) <= 100:
        return error_response("authorized_by must be between 2 and 100 characters")
    if not isinstance(note, str) or len(note) > 500:
        return error_response("note must be a string of at most 500 characters")
    try:
        queued = authorize_alert(get_db(), alert_id, actor.strip(), note.strip())
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 409)
    return jsonify({"data": {"alert_id": alert_id, "queued_deliveries": queued}})


@bp.get("/api/notification-deliveries")
def list_notification_deliveries():
    rows = []
    for row in get_db().execute(
        """SELECT deliveries.id, deliveries.alert_id, deliveries.status,
                  deliveries.attempts, deliveries.queued_at,
                  deliveries.last_attempt_at, deliveries.sent_at,
                  deliveries.provider_message_id, deliveries.error_message,
                  subscriptions.name AS subscription_name,
                  subscriptions.channel, subscriptions.destination
           FROM notification_deliveries deliveries
           JOIN notification_subscriptions subscriptions
             ON subscriptions.id = deliveries.subscription_id
           ORDER BY deliveries.queued_at DESC"""
    ):
        item = dict(row)
        item["destination"] = mask_destination(item["channel"], item["destination"])
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/alerts/<int:alert_id>/acknowledge")
def acknowledge_alert(alert_id):
    auth_error = require_write_access()
    if auth_error:
        return auth_error
    database = get_db()
    row = database.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if not row:
        return error_response("alert not found", 404)
    if row["acknowledged_at"]:
        return jsonify({"data": dict(row)})
    database.execute(
        "UPDATE alerts SET acknowledged_at = datetime('now') WHERE id = ?", (alert_id,)
    )
    database.commit()
    updated = database.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return jsonify({"data": dict(updated)})


@bp.errorhandler(413)
def too_large(_error):
    return error_response("request body exceeds 64 KiB", 413)


@bp.app_errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return error_response("endpoint not found", 404)
    return render_template("404.html"), 404
