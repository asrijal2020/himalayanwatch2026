from datetime import datetime, timezone

from glacier_monitor import earth_engine
from glacier_monitor.db import get_db


def test_earth_engine_status_is_explicit_when_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        earth_engine,
        "initialize_earth_engine",
        lambda: (_ for _ in ()).throw(RuntimeError("authentication required")),
    )
    response = client.get("/api/earth-engine/status")
    assert response.status_code == 200
    body = response.get_json()
    assert body["available"] is False
    assert body["authenticated"] is False
    assert body["message"] == "authentication required"
    assert "USGS/SRTMGL1_003" in {
        item["dataset_id"] for item in body["datasets"].values()
    }


def test_stored_earth_engine_features_are_exposed(app):
    with app.app_context():
        database = get_db()
        asset = database.execute(
            "SELECT * FROM assets WHERE slug = 'imja-tsho'"
        ).fetchone()
        result = {
            "observed_start": "2026-09-01T00:00:00+00:00",
            "observed_end": "2026-09-15T00:00:00+00:00",
            "buffer_m": 5000,
            "values": {
                "elevation_m": 4985.0,
                "temperature_mean_c": -1.2,
                "precipitation_total_mm": 41.0,
                "snow_cover_mean_pct": 73.0,
            },
        }
        earth_engine.store_features(
            database,
            asset,
            result,
            fetched_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )

    response = app.test_client().get("/api/assets/imja-tsho/earth-engine")
    assert response.status_code == 200
    body = response.get_json()
    assert body["count"] == 4
    by_metric = {item["metric"]: item for item in body["data"]}
    assert by_metric["elevation_m"]["value"] == 4985.0
    assert by_metric["temperature_mean_c"]["data_classification"] == "modeled"
    assert by_metric["snow_cover_mean_pct"]["scale_m"] == 500


def test_earth_engine_cli_rejects_unknown_asset(app):
    result = app.test_cli_runner().invoke(
        args=["refresh-gee-features", "--asset", "not-an-asset"]
    )
    assert result.exit_code != 0
    assert "Unknown asset" in result.output
