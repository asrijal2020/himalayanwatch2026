from datetime import datetime, timedelta, timezone


def valid_reading(**overrides):
    payload = {
        "asset": "imja-tsho",
        "metric": "water_level_m",
        "value": 4.7,
        "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "is_synthetic": True,
        "metadata": {"test": True},
    }
    payload.update(overrides)
    return payload


def test_lists_and_filters_assets(client):
    response = client.get("/api/assets?country=Nepal&asset_type=lake")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 2
    assert all(item["country"] == "Nepal" for item in payload["data"])
    assert all(item["asset_type"] == "lake" for item in payload["data"])


def test_rejects_invalid_filter(client):
    response = client.get("/api/assets?country=Bhutan")
    assert response.status_code == 400
    assert "invalid country" in response.get_json()["error"]["message"]


def test_post_reading_and_list_history(client):
    response = client.post("/api/readings", json=valid_reading())
    assert response.status_code == 201
    assert response.get_json()["data"]["metric"] == "water_level_m"

    history = client.get("/api/readings?asset=imja-tsho&limit=2").get_json()
    assert history["count"] == 2
    assert history["data"][0]["is_synthetic"] is True
    assert history["data"][0]["available_at"]
    assert history["data"][0]["quality_status"] == "unverified"


def test_post_reading_validation(client):
    response = client.post("/api/readings", json=valid_reading(value="high"))
    assert response.status_code == 400
    assert response.get_json()["error"]["message"] == "value must be a number"

    response = client.post("/api/readings", json=valid_reading(metric="temperature_c"))
    assert response.status_code == 400
    assert "metric must be one of" in response.get_json()["error"]["message"]

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    response = client.post("/api/readings", json=valid_reading(observed_at=future))
    assert response.status_code == 400
    assert "cannot be in the future" in response.get_json()["error"]["message"]

    observed = datetime.now(timezone.utc) - timedelta(hours=1)
    response = client.post(
        "/api/readings",
        json=valid_reading(
            observed_at=observed.isoformat(),
            available_at=(observed - timedelta(minutes=1)).isoformat(),
        ),
    )
    assert response.status_code == 400
    assert "cannot be earlier" in response.get_json()["error"]["message"]


def test_research_context_and_decision_gates(client):
    cases = client.get("/api/cases").get_json()
    assert cases["count"] == 1
    assert cases["data"][0]["peak_rise_m"] == 9.0
    assert len(cases["data"][0]["downstream_locations"]) == 5
    assert "Preliminary" in cases["data"][0]["confidence"]

    discovery = client.get("/api/discovery-status").get_json()
    assert discovery["count"] == 3
    assert "does not establish validated hazard detection" in discovery["interpretation"]

    asset = client.get("/api/assets/imja-tsho").get_json()["data"]
    assert asset["decision_readiness"]["state"] == "research_only"


def test_rasuwa_corridor_has_glacier_and_station_context(client):
    assets = client.get("/api/assets?country=Nepal").get_json()["data"]
    by_slug = {item["slug"]: item for item in assets}
    assert {"yala-glacier", "langtang-glacier", "yala-aws",
            "syabrubesi-hydromet-reference"} <= set(by_slug)
    assert by_slug["yala-aws"]["device_type"] == "automatic weather station"
    assert by_slug["yala-aws"]["latest_reading"]["is_synthetic"] is True
    assert "no verified live station feed" in (
        by_slug["syabrubesi-hydromet-reference"]["data_classification"].lower()
    )


def test_india_and_pakistan_have_each_asset_type(client):
    for country in ("India", "Pakistan"):
        assets = client.get(f"/api/assets?country={country}").get_json()["data"]
        assert {"glacier", "lake", "station"} == {
            item["asset_type"] for item in assets
        }
        assert all(item["latest_reading"]["is_synthetic"] for item in assets)
        assert all(item["source_url"].startswith("https://") for item in assets)


def test_write_api_key_is_enforced(app):
    app.config["WRITE_API_KEY"] = "secret"
    client = app.test_client()
    assert client.post("/api/readings", json=valid_reading()).status_code == 401
    assert client.post(
        "/api/readings", json=valid_reading(), headers={"X-API-Key": "secret"}
    ).status_code == 201
