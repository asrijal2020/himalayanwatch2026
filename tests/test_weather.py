from datetime import datetime, timedelta, timezone

import pytest

from glacier_monitor.weather import serialize_weather, store_weather


def weather_result(start, hours=48):
    times = [
        (start + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
        for index in range(hours)
    ]
    return {
        "source_url": "https://api.open-meteo.com/v1/forecast?test=1",
        "grid_latitude": 27.94,
        "grid_longitude": 86.04,
        "grid_elevation_m": 3763.0,
        "hourly": {
            "time": times,
            "temperature_2m": [float(index % 12) for index in range(hours)],
            "relative_humidity_2m": [70.0] * hours,
            "precipitation": [0.5] * hours,
            "rain": [0.4] * hours,
            "snowfall": [0.1] * hours,
            "wind_speed_10m": [12.0] * hours,
        },
    }


def test_weather_storage_and_daily_summary(database):
    asset = database.execute("SELECT * FROM assets WHERE slug = 'imja-tsho'").fetchone()
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=2)
    result = store_weather(database, asset, weather_result(start))
    assert result == {"inserted": 48, "updated": 0, "total": 48}

    repeated = store_weather(database, asset, weather_result(start))
    assert repeated == {"inserted": 0, "updated": 48, "total": 48}

    summary = serialize_weather(database, asset, days=14)
    assert summary["count"] == 48
    assert summary["data_classification"] == "modeled"
    assert len(summary["daily"]) == 2
    assert summary["daily"][0]["precipitation_total_mm"] == 12
    assert summary["daily"][0]["snowfall_total_cm"] == pytest.approx(2.4)
    assert "not measurements" in summary["interpretation"]
    assert summary["collection_basis"]["timezone"] == "UTC"
    assert summary["collection_basis"]["sampling"] == "hourly"
    assert summary["collection_basis"]["aggregation"].startswith("Daily mean")
    assert summary["period_start"]
    assert summary["period_end"]


def test_weather_api_validation_and_output(client, database):
    assert client.get("/api/assets/missing/weather").status_code == 404
    response = client.get("/api/assets/imja-tsho/weather?days=0")
    assert response.status_code == 400
    assert "between 1 and 31" in response.get_json()["error"]["message"]
    response = client.get("/api/assets/imja-tsho/weather?days=two")
    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "days must be an integer"

    asset = database.execute("SELECT * FROM assets WHERE slug = 'imja-tsho'").fetchone()
    start = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(hours=3)
    store_weather(database, asset, weather_result(start, hours=3))
    body = client.get("/api/assets/imja-tsho/weather?days=14").get_json()["data"]
    assert body["count"] == 3
    assert body["latest"]["provider"] == "Open-Meteo"
    assert body["latest"]["data_classification"] == "modeled"
    assert len(body["hourly"]) == 3


def test_refresh_weather_cli_rejects_unknown_asset(app):
    result = app.test_cli_runner().invoke(
        args=["refresh-weather", "--asset", "not-an-asset"]
    )
    assert result.exit_code != 0
    assert "Unknown asset" in result.output
