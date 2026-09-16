from datetime import datetime, timedelta, timezone
from threading import Barrier


def create_live_alert(client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        "/api/source-events",
        json={
            "event_id": "live-critical-imja",
            "provider": "test-validated-feed",
            "event_type": "sensor.reading",
            "asset": "imja-tsho",
            "metric": "water_level_m",
            "value": 6.2,
            "observed_at": (now - timedelta(minutes=2)).isoformat(),
            "available_at": (now - timedelta(minutes=1)).isoformat(),
            "quality_status": "validated",
            "source_url": "https://example.org/live/imja",
            "is_synthetic": False,
        },
    )
    assert response.status_code == 201
    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    return next(
        alert
        for alert in alerts
        if alert["asset_slug"] == "imja-tsho" and "6.20 m" in alert["reason"]
    )


def test_authorization_queues_matching_masked_subscription(client):
    subscription = client.post(
        "/api/notification-subscriptions",
        json={
            "name": "Nepal duty team",
            "channel": "sms",
            "destination": "+9779800001234",
            "country": "Nepal",
            "minimum_severity": "critical",
        },
    )
    assert subscription.status_code == 201
    assert subscription.get_json()["data"]["destination"] == "***1234"

    alert = create_live_alert(client)
    response = client.post(
        f"/api/alerts/{alert['id']}/authorize",
        json={"authorized_by": "National emergency duty officer"},
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["queued_deliveries"] == 1

    deliveries = client.get("/api/notification-deliveries").get_json()
    assert deliveries["count"] == 1
    assert deliveries["data"][0]["status"] == "pending"
    assert deliveries["data"][0]["destination"] == "***1234"


def test_synthetic_evidence_cannot_be_authorized(client):
    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    synthetic = next(alert for alert in alerts if alert["evidence_is_synthetic"])
    response = client.post(
        f"/api/alerts/{synthetic['id']}/authorize",
        json={"authorized_by": "Test operator"},
    )
    assert response.status_code == 409
    assert "synthetic evidence" in response.get_json()["error"]["message"]


def test_subscription_validation(client):
    response = client.post(
        "/api/notification-subscriptions",
        json={
            "name": "Unsafe webhook",
            "channel": "webhook",
            "destination": "http://localhost/notify",
        },
    )
    assert response.status_code == 400
    assert "HTTPS" in response.get_json()["error"]["message"]

    response = client.post(
        "/api/notification-subscriptions",
        json={
            "name": "Invalid email",
            "channel": "email",
            "destination": "not-an-email",
        },
    )
    assert response.status_code == 400
    assert "valid email" in response.get_json()["error"]["message"]


def test_pending_sms_delivery_is_processed(client, database, monkeypatch):
    client.post(
        "/api/notification-subscriptions",
        json={
            "name": "Delivery test",
            "channel": "sms",
            "destination": "+9779800005678",
            "country": "Nepal",
            "minimum_severity": "critical",
        },
    )
    alert = create_live_alert(client)
    client.post(
        f"/api/alerts/{alert['id']}/authorize",
        json={"authorized_by": "Delivery test officer"},
    )

    monkeypatch.setattr(
        "glacier_monitor.notifications.send_sms",
        lambda payload: f"provider-{payload['alert_id']}",
    )
    from glacier_monitor.notifications import process_pending_deliveries

    summary = process_pending_deliveries(database)
    assert summary == {"sent": 1, "failed": 0}
    delivery = client.get("/api/notification-deliveries").get_json()["data"][0]
    assert delivery["status"] == "sent"
    assert delivery["provider_message_id"].startswith("provider-")


def test_ingestion_key_cannot_authorize_operator_action(app):
    app.config.update(
        INGEST_API_KEY="sensor-secret",
        OPERATOR_API_KEY="operator-secret",
        WRITE_API_KEY="",
    )
    client = app.test_client()
    alert = client.get("/api/alerts?acknowledged=false").get_json()["data"][0]
    response = client.post(
        f"/api/alerts/{alert['id']}/authorize",
        headers={"X-API-Key": "sensor-secret"},
        json={"authorized_by": "Unauthorized sensor"},
    )
    assert response.status_code == 401


def test_sms_and_email_deliver_in_parallel(client, database, monkeypatch):
    for name, channel, destination in (
        ("SMS roster", "sms", "+9779800004321"),
        ("Email roster", "email", "alerts@example.org"),
    ):
        response = client.post(
            "/api/notification-subscriptions",
            json={
                "name": name,
                "channel": channel,
                "destination": destination,
                "country": "Nepal",
                "minimum_severity": "critical",
            },
        )
        assert response.status_code == 201
    subscriptions = client.get("/api/notification-subscriptions").get_json()["data"]
    assert {item["channel"] for item in subscriptions} == {"sms", "email"}
    assert next(item for item in subscriptions if item["channel"] == "email")[
        "destination"
    ] == "a***@example.org"

    alert = create_live_alert(client)
    authorization = client.post(
        f"/api/alerts/{alert['id']}/authorize",
        json={"authorized_by": "Parallel delivery officer"},
    )
    assert authorization.get_json()["data"]["queued_deliveries"] == 2

    barrier = Barrier(2)

    def concurrent_send(payload):
        barrier.wait(timeout=2)
        return f"{payload['channel']}-{payload['alert_id']}"

    monkeypatch.setattr("glacier_monitor.notifications.send_sms", concurrent_send)
    monkeypatch.setattr("glacier_monitor.notifications.send_email", concurrent_send)
    from glacier_monitor.notifications import process_pending_deliveries

    summary = process_pending_deliveries(database)
    assert summary == {"sent": 2, "failed": 0}
    deliveries = client.get("/api/notification-deliveries").get_json()["data"]
    assert {item["channel"] for item in deliveries} == {"sms", "email"}
    assert all(item["status"] == "sent" for item in deliveries)


def test_azure_email_payload_uses_configured_sender(app, monkeypatch):
    from azure.communication.email import EmailClient
    from glacier_monitor.notifications import send_email

    sent = {}

    class Poller:
        def result(self):
            return {"id": "email-message-1"}

    class Client:
        def begin_send(self, message):
            sent.update(message)
            return Poller()

    monkeypatch.setattr(
        EmailClient,
        "from_connection_string",
        staticmethod(lambda _connection_string: Client()),
    )
    app.config.update(
        AZURE_COMMUNICATION_CONNECTION_STRING="endpoint=https://example.org/;accesskey=test",
        AZURE_EMAIL_SENDER="DoNotReply@alerts.example.org",
    )
    with app.app_context():
        message_id = send_email(
            {
                "alert_id": 1,
                "severity": "critical",
                "asset_name": "Syabrubesi gauge",
                "country": "Nepal",
                "reason": "River level exceeded the critical threshold.",
                "latitude": 28.162,
                "longitude": 85.334,
                "destination": "team@example.org",
            }
        )
    assert message_id == "email-message-1"
    assert sent["senderAddress"] == "DoNotReply@alerts.example.org"
    assert sent["recipients"]["to"] == [{"address": "team@example.org"}]
    assert "CRITICAL" in sent["content"]["subject"]
