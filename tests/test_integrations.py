from datetime import datetime, timedelta, timezone


def source_event(**overrides):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "event_id": "sensor-event-001",
        "provider": "test-sensor-network",
        "event_type": "sensor.reading",
        "asset": "imja-tsho",
        "metric": "water_level_m",
        "value": 5.8,
        "observed_at": (now - timedelta(minutes=2)).isoformat(),
        "available_at": (now - timedelta(minutes=1)).isoformat(),
        "quality_status": "validated",
        "source_url": "https://example.org/observations/001",
        "is_synthetic": False,
    }
    payload.update(overrides)
    return payload


def test_source_event_is_idempotent_and_creates_alert(client):
    first = client.post("/api/source-events", json=source_event())
    assert first.status_code == 201
    assert first.get_json()["data"]["status"] == "processed"
    assert first.get_json()["data"]["duplicate"] is False

    duplicate = client.post("/api/source-events", json=source_event())
    assert duplicate.status_code == 201
    assert duplicate.get_json()["data"]["duplicate"] is True

    events = client.get("/api/source-events").get_json()
    assert events["count"] == 1
    assert events["data"][0]["status"] == "processed"

    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    assert any(
        alert["asset_slug"] == "imja-tsho"
        and alert["evidence_quality"] == "validated"
        and alert["evidence_is_synthetic"] == 0
        for alert in alerts
    )


def test_rejected_source_event_is_audited(client):
    response = client.post(
        "/api/source-events",
        json=source_event(event_id="bad-metric", metric="temperature_c"),
    )
    assert response.status_code == 400
    events = client.get("/api/source-events").get_json()["data"]
    rejected = next(event for event in events if event["event_id"] == "bad-metric")
    assert rejected["status"] == "rejected"
    assert "metric must be one of" in rejected["error_message"]


def test_source_event_requires_unique_identifier(client):
    payload = source_event()
    payload.pop("event_id")
    response = client.post("/api/source-events", json=payload)
    assert response.status_code == 400
    assert "event_id" in response.get_json()["error"]["message"]


def test_eventhub_mode_accepts_without_local_persistence(app, monkeypatch):
    published = []
    app.config["INGESTION_MODE"] = "eventhub"
    monkeypatch.setattr(
        "glacier_monitor.integrations.publish_event", lambda event: published.append(event)
    )
    client = app.test_client()
    response = client.post("/api/source-events", json=source_event())
    assert response.status_code == 202
    assert response.get_json()["data"]["status"] == "accepted"
    assert published[0]["event_id"] == "sensor-event-001"
    assert client.get("/api/source-events").get_json()["count"] == 0


def test_validated_river_event_creates_flood_relevant_alerts(client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        "/api/source-events",
        json={
            "event_id": "syabrubesi-river-critical",
            "provider": "test-river-gateway",
            "event_type": "sensor.reading",
            "asset": "syabrubesi-hydromet-reference",
            "metric": "river_level_m",
            "value": 5.5,
            "observed_at": (now - timedelta(minutes=2)).isoformat(),
            "available_at": (now - timedelta(minutes=1)).isoformat(),
            "quality_status": "validated",
            "source_url": "https://example.org/river/syabrubesi",
            "is_synthetic": False,
        },
    )
    assert response.status_code == 201
    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    river_types = {
        alert["alert_type"]
        for alert in alerts
        if alert["asset_slug"] == "syabrubesi-hydromet-reference"
        and alert["evidence_quality"] == "validated"
    }
    assert {"river_threshold_exceedance", "rapid_river_rise"} <= river_types
