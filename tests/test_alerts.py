from datetime import datetime, timedelta, timezone


def post(client, asset, metric, value, observed_at):
    return client.post(
        "/api/readings",
        json={
            "asset": asset,
            "metric": metric,
            "value": value,
            "observed_at": observed_at.isoformat(),
            "is_synthetic": True,
        },
    )


def test_threshold_and_rapid_rise_alerts(client, database):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert post(client, "imja-tsho", "water_level_m", 4.8, now - timedelta(hours=25)).status_code == 201
    assert post(client, "imja-tsho", "water_level_m", 5.9, now - timedelta(hours=1)).status_code == 201

    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    imja = [item for item in alerts if item["asset_slug"] == "imja-tsho"]
    types = {item["alert_type"] for item in imja}
    assert "threshold_exceedance" in types
    assert "rapid_rise" in types
    assert any(item["severity"] == "critical" for item in imja)


def test_abnormal_glacier_movement_alert(client):
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    response = post(client, "khumbu-glacier", "glacier_velocity_m_day", 1.2, now)
    assert response.status_code == 201
    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    assert any(
        item["asset_slug"] == "khumbu-glacier"
        and item["alert_type"] == "abnormal_movement"
        for item in alerts
    )


def test_stale_offline_alert_is_seeded_and_acknowledged(client):
    alerts = client.get("/api/alerts?acknowledged=false").get_json()["data"]
    stale = next(item for item in alerts if item["asset_slug"] == "lukla-weather")
    assert stale["alert_type"] == "sensor_stale"
    assert stale["severity"] == "critical"

    response = client.post(f"/api/alerts/{stale['id']}/acknowledge")
    assert response.status_code == 200
    assert response.get_json()["data"]["acknowledged_at"] is not None

