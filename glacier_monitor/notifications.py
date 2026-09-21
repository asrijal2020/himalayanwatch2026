import hashlib
import hmac
import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import click
from flask import current_app

from .db import get_db
from .services import parse_timestamp
from .source_adapters import validate_public_https_url


SEVERITY_RANK = {"warning": 1, "critical": 2}


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def mask_destination(channel, destination):
    if channel == "sms":
        return f"***{destination[-4:]}" if len(destination) >= 4 else "***"
    if channel == "email":
        local, _, domain = destination.partition("@")
        return f"{local[:1]}***@{domain}"
    return destination.split("?", 1)[0]


def latest_evidence(database, alert):
    if alert["evidence_reading_id"]:
        return database.execute(
            """SELECT quality_status, is_synthetic, observed_at
               FROM readings WHERE id = ?""",
            (alert["evidence_reading_id"],),
        ).fetchone()
    return database.execute(
        """SELECT quality_status, is_synthetic, observed_at
           FROM readings WHERE asset_id = ?
           ORDER BY observed_at DESC LIMIT 1""",
        (alert["asset_id"],),
    ).fetchone()


def authorize_alert(database, alert_id, actor, note=""):
    alert = database.execute(
        """SELECT alerts.*, assets.stale_after_hours
           FROM alerts JOIN assets ON assets.id = alerts.asset_id
           WHERE alerts.id = ?""",
        (alert_id,),
    ).fetchone()
    if not alert:
        raise LookupError("alert not found")
    if alert["authorized_at"]:
        return 0
    evidence = latest_evidence(database, alert)
    if not evidence:
        raise ValueError("alert cannot be authorized without supporting evidence")
    if evidence["is_synthetic"]:
        raise ValueError("synthetic evidence cannot authorize end-user notification")
    if evidence["quality_status"] != "validated":
        raise ValueError("only validated evidence can authorize end-user notification")
    age_hours = (utcnow() - parse_timestamp(evidence["observed_at"])).total_seconds() / 3600
    if alert["alert_type"] != "sensor_stale" and age_hours > alert["stale_after_hours"]:
        raise ValueError("stale evidence cannot authorize end-user notification")
    reviewed_evidence = database.execute(
        """SELECT review_status FROM evidence_records
           WHERE reading_id = ? ORDER BY created_at DESC LIMIT 1""",
        (alert["evidence_reading_id"],),
    ).fetchone()
    operational_ready = bool(
        reviewed_evidence and reviewed_evidence["review_status"] == "approved"
    )
    rows = database.execute(
        """SELECT id FROM notification_subscriptions
           WHERE enabled = 1
             AND (country IS NULL OR country = (
                 SELECT country FROM assets WHERE id = ?
             ))""",
        (alert["asset_id"],),
    ).fetchall()
    matching_subscriptions = []
    for row in rows:
        subscription = database.execute(
            "SELECT * FROM notification_subscriptions WHERE id = ?", (row["id"],)
        ).fetchone()
        if SEVERITY_RANK[alert["severity"]] < SEVERITY_RANK[subscription["minimum_severity"]]:
            continue
        matching_subscriptions.append(subscription)
    if (
        any(item["environment"] == "operational" for item in matching_subscriptions)
        and not operational_ready
    ):
        raise ValueError(
            "operational recipients require separate scientific approval of the "
            "supporting evidence"
        )
    authorized_at = utcnow().isoformat()
    database.execute(
        """UPDATE alerts SET authorized_at = ?, authorized_by = ?, authorization_note = ?
           WHERE id = ?""",
        (authorized_at, actor, note, alert_id),
    )
    queued = 0
    for subscription in matching_subscriptions:
        cursor = database.execute(
            """INSERT OR IGNORE INTO notification_deliveries
               (alert_id, subscription_id, status, queued_at)
               VALUES (?, ?, 'pending', ?)""",
            (alert_id, subscription["id"], authorized_at),
        )
        queued += cursor.rowcount
    database.commit()
    return queued


def notification_payload(database, delivery):
    row = database.execute(
        """SELECT alerts.id AS alert_id, alerts.severity, alerts.alert_type, alerts.reason,
                  alerts.created_at, alerts.authorized_at, alerts.authorized_by,
                  assets.name AS asset_name, assets.country, assets.latitude, assets.longitude,
                  subscriptions.channel, subscriptions.destination,
                  subscriptions.environment
           FROM notification_deliveries deliveries
           JOIN alerts ON alerts.id = deliveries.alert_id
           JOIN assets ON assets.id = alerts.asset_id
           JOIN notification_subscriptions subscriptions
             ON subscriptions.id = deliveries.subscription_id
           WHERE deliveries.id = ?""",
        (delivery["id"],),
    ).fetchone()
    if not row:
        raise LookupError("notification delivery not found")
    return dict(row)


def _message(payload):
    return (
        f"HimalayaWatch {payload['severity'].upper()} research alert for "
        f"{payload['asset_name']}, {payload['country']}: {payload['reason']} "
        "Verify locally and follow instructions from authorized emergency officials."
    )


def send_webhook(payload):
    validate_public_https_url(payload["destination"])
    body = json.dumps(
        {
            "schema": "himalayawatch.notification.v1",
            "alert": {key: value for key, value in payload.items() if key != "destination"},
            "message": _message(payload),
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "HimalayaWatch/0.2"}
    secret = current_app.config.get("NOTIFICATION_WEBHOOK_SECRET", "")
    if secret:
        headers["X-HimalayaWatch-Signature"] = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    request = Request(payload["destination"], data=body, headers=headers, method="POST")
    with urlopen(
        request, timeout=current_app.config.get("NOTIFICATION_TIMEOUT_SECONDS", 10)
    ) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")
        return response.headers.get("X-Request-Id")


def send_sms(payload):
    endpoint = current_app.config.get("AZURE_COMMUNICATION_ENDPOINT")
    connection_string = current_app.config.get("AZURE_COMMUNICATION_CONNECTION_STRING")
    sender = current_app.config.get("AZURE_SMS_SENDER")
    if not sender:
        raise RuntimeError("AZURE_SMS_SENDER is not configured")
    try:
        from azure.communication.sms import SmsClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("Azure Communication Services dependencies are not installed") from exc
    if connection_string:
        client = SmsClient.from_connection_string(connection_string)
    elif endpoint:
        client = SmsClient(endpoint, DefaultAzureCredential())
    else:
        raise RuntimeError(
            "configure AZURE_COMMUNICATION_ENDPOINT for managed identity or "
            "AZURE_COMMUNICATION_CONNECTION_STRING"
        )
    result = client.send(
        from_=sender,
        to=[payload["destination"]],
        message=_message(payload),
        enable_delivery_report=True,
    )[0]
    if not result.successful:
        raise RuntimeError(result.error_message or "SMS provider rejected the message")
    return result.message_id


def send_email(payload):
    endpoint = current_app.config.get("AZURE_COMMUNICATION_ENDPOINT")
    connection_string = current_app.config.get("AZURE_COMMUNICATION_CONNECTION_STRING")
    sender = current_app.config.get("AZURE_EMAIL_SENDER")
    if not sender:
        raise RuntimeError("AZURE_EMAIL_SENDER is not configured")
    try:
        from azure.communication.email import EmailClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("Azure Communication Services dependencies are not installed") from exc
    if connection_string:
        client = EmailClient.from_connection_string(connection_string)
    elif endpoint:
        client = EmailClient(endpoint, DefaultAzureCredential())
    else:
        raise RuntimeError(
            "configure AZURE_COMMUNICATION_ENDPOINT for managed identity or "
            "AZURE_COMMUNICATION_CONNECTION_STRING"
        )
    subject = (
        f"HimalayaWatch {payload['severity'].upper()}: "
        f"{payload['asset_name']}, {payload['country']}"
    )
    plain_text = _message(payload)
    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": payload["destination"]}]},
        "content": {
            "subject": subject,
            "plainText": plain_text,
            "html": (
                f"<h1>{html.escape(subject)}</h1>"
                f"<p>{html.escape(payload['reason'])}</p>"
                f"<p>Location: {payload['latitude']:.4f}, "
                f"{payload['longitude']:.4f}</p>"
                "<p><strong>Research decision support:</strong> verify locally and "
                "follow instructions from authorized emergency officials.</p>"
            ),
        },
    }
    result = client.begin_send(message).result()
    return result.get("id") if isinstance(result, dict) else getattr(result, "id", None)


def _send_delivery(app, payload):
    with app.app_context():
        if payload["channel"] == "sms":
            return send_sms(payload)
        if payload["channel"] == "email":
            return send_email(payload)
        if payload["channel"] == "webhook":
            return send_webhook(payload)
        raise RuntimeError(f"unsupported notification channel: {payload['channel']}")


def process_pending_deliveries(database, limit=100):
    max_attempts = current_app.config.get("NOTIFICATION_MAX_ATTEMPTS", 5)
    deliveries = database.execute(
        """SELECT * FROM notification_deliveries
           WHERE status IN ('pending', 'failed')
             AND attempts < ?
           ORDER BY queued_at LIMIT ?""",
        (max_attempts, limit),
    ).fetchall()
    summary = {"sent": 0, "failed": 0}
    app = current_app._get_current_object()
    ready = []
    for delivery in deliveries:
        attempted_at = utcnow().isoformat()
        database.execute(
            """UPDATE notification_deliveries
               SET status = 'sending', attempts = attempts + 1, last_attempt_at = ?,
                   error_message = NULL WHERE id = ?""",
            (attempted_at, delivery["id"]),
        )
        database.commit()
        try:
            payload = notification_payload(database, delivery)
            ready.append((delivery["id"], payload))
        except Exception as exc:
            current_app.logger.exception("Notification delivery %s failed", delivery["id"])
            database.execute(
                """UPDATE notification_deliveries SET status = 'failed', error_message = ?
                   WHERE id = ?""",
                (str(exc)[:500], delivery["id"]),
            )
            summary["failed"] += 1
        database.commit()
    worker_count = max(
        1, min(current_app.config.get("NOTIFICATION_WORKERS", 4), len(ready) or 1)
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_send_delivery, app, payload): delivery_id
            for delivery_id, payload in ready
        }
        for future in as_completed(futures):
            delivery_id = futures[future]
            try:
                message_id = future.result()
                database.execute(
                    """UPDATE notification_deliveries
                       SET status = 'sent', sent_at = ?, provider_message_id = ?
                       WHERE id = ?""",
                    (utcnow().isoformat(), message_id, delivery_id),
                )
                summary["sent"] += 1
            except Exception as exc:
                current_app.logger.exception(
                    "Notification delivery %s failed", delivery_id
                )
                database.execute(
                    """UPDATE notification_deliveries
                       SET status = 'failed', error_message = ? WHERE id = ?""",
                    (str(exc)[:500], delivery_id),
                )
                summary["failed"] += 1
            database.commit()
    return summary


@click.command("process-notifications")
@click.option("--limit", default=100, show_default=True, type=click.IntRange(1, 500))
def process_notifications_command(limit):
    """Deliver authorized pending alerts through configured channels."""
    summary = process_pending_deliveries(get_db(), limit=limit)
    click.echo(f"Notifications sent: {summary['sent']}; failed: {summary['failed']}.")


def init_app(app):
    app.cli.add_command(process_notifications_command)
