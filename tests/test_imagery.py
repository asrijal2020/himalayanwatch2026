import json
from datetime import date, timedelta

from glacier_monitor.imagery import rolling_start


def test_imagery_pages_load(client):
    response = client.get("/imagery")
    assert response.status_code == 200
    assert b"Glacier imagery explorer" in response.data
    assert b"not satellite images or hazard detections" in response.data

    detail = client.get("/imagery/glaciers/HW-DEMO-NP-0001")
    assert detail.status_code == 200
    assert b"HW-DEMO-NP-0001" in detail.data


def test_catalog_metadata_pagination_and_unnamed_glaciers(client):
    first = client.get("/api/glaciers?limit=5").get_json()
    assert first["count"] == 5
    assert first["total"] == 48
    assert first["next_cursor"] == 5
    assert first["catalog"]["is_authoritative"] is False
    assert "not a complete glacier inventory" in first["catalog"]["coverage_statement"]

    second = client.get("/api/glaciers?limit=5&cursor=5").get_json()
    assert second["data"][0]["inventory_id"] != first["data"][0]["inventory_id"]
    by_page = client.get("/api/glaciers?limit=5&page=2").get_json()
    assert by_page["data"][0]["inventory_id"] == second["data"][0]["inventory_id"]

    unnamed = client.get("/api/glaciers?q=HW-DEMO-NP-0007").get_json()["data"][0]
    assert unnamed["name"] is None
    assert unnamed["display_name"] == unnamed["inventory_id"]


def test_catalog_filters_and_bbox_validation(client):
    nepal = client.get("/api/glaciers?country=Nepal&region=Koshi%20/%20Everest").get_json()
    assert nepal["count"] == 3
    assert all(item["country"] == "Nepal" for item in nepal["data"])

    bounded = client.get("/api/glaciers?bbox=86.5,27.5,87.0,28.2").get_json()
    assert bounded["count"] > 0
    assert all(86.5 <= item["longitude"] <= 87.0 for item in bounded["data"])

    assert client.get("/api/glaciers?bbox=90,30,80,20").status_code == 400
    assert client.get("/api/glaciers?country=India").status_code == 200
    assert client.get("/api/glaciers?country=Pakistan").status_code == 200
    assert client.get("/api/glaciers?country=Bhutan").status_code == 400
    assert client.get("/api/glaciers?limit=101").status_code == 400
    assert client.get("/api/glaciers?cursor=2&page=2").status_code == 400


def test_imagery_rolling_window_and_labels(client):
    payload = client.get("/api/glacier-imagery?limit=100").get_json()
    assert payload["total"] == 144
    assert payload["window"]["start"] == rolling_start(date.today()).isoformat()
    assert payload["window"]["end"] == date.today().isoformat()
    assert all(item["is_synthetic"] is True for item in payload["data"])
    assert all(item["processing_level"] == "DEMO-NOT-SATELLITE" for item in payload["data"])
    assert all(item["thumbnail_kind"] == "none" for item in payload["data"])
    assert all(item["available_at"] >= item["acquired_at"] for item in payload["data"])


def test_imagery_filters_and_pagination(client):
    first = client.get(
        "/api/glacier-imagery?country=Tibet&max_cloud_cover=25&limit=5"
    ).get_json()
    assert first["count"] <= 5
    assert first["total"] > 5
    assert all(item["country"] == "Tibet" for item in first["data"])
    assert all(item["cloud_cover"] <= 25 for item in first["data"])
    assert first["next_cursor"] == 5

    glacier = client.get(
        "/api/glacier-imagery?glacier_id=HW-DEMO-NP-0001"
    ).get_json()
    assert glacier["total"] == 3
    assert all(item["glacier_id"] == "HW-DEMO-NP-0001" for item in glacier["data"])


def test_imagery_date_validation(client):
    today = date.today()
    too_old = rolling_start(today) - timedelta(days=1)
    response = client.get(
        f"/api/glacier-imagery?start_date={too_old.isoformat()}&end_date={today.isoformat()}"
    )
    assert response.status_code == 400
    assert "rolling three-month window" in response.get_json()["error"]["message"]

    future = today + timedelta(days=1)
    assert client.get(
        f"/api/glacier-imagery?start_date={today.isoformat()}&end_date={future.isoformat()}"
    ).status_code == 400
    assert client.get("/api/glacier-imagery?max_cloud_cover=101").status_code == 400
    assert client.get("/api/glacier-imagery?start_date=not-a-date").status_code == 400


def test_glacier_detail_api(client):
    response = client.get("/api/glaciers/HW-DEMO-NP-0001")
    assert response.status_code == 200
    assert response.get_json()["data"]["imagery_count"] == 3
    assert client.get("/api/glaciers/missing").status_code == 404


def test_geojson_inventory_import_command(app, tmp_path):
    inventory = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"RGIId": "RGI2000-v7.0-G-TEST", "Name": None, "region": "Test"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[86.0, 28.0], [86.1, 28.0], [86.1, 28.1], [86.0, 28.0]]],
            },
        }],
    }
    path = tmp_path / "inventory.geojson"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "import-glacier-inventory", str(path), "--catalog-name", "RGI test",
            "--catalog-version", "7.0", "--source-url", "https://doi.org/10.5067/F6JMOVY5NAVZ",
            "--country", "Nepal", "--replace",
        ]
    )
    assert result.exit_code == 0
    assert "Imported 1 glaciers" in result.output
    response = app.test_client().get("/api/glaciers")
    assert response.get_json()["data"][0]["inventory_id"] == "RGI2000-v7.0-G-TEST"
    assert response.get_json()["catalog"]["is_authoritative"] is True


def test_demo_imagery_refresh_command(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["refresh-imagery", "--provider", "demo"])
    assert result.exit_code == 0
    assert "Refreshed 144 deterministic demo imagery metadata records" in result.output
