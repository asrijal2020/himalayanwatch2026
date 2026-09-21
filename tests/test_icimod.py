import csv
import io
import zipfile

from glacier_monitor.db import get_db
from glacier_monitor import icimod
from glacier_monitor.icimod import import_dataset_file, register_dataset


def create_csv_zip(path):
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=["event_id", "event_date", "latitude", "longitude", "impact"]
    )
    writer.writeheader()
    writer.writerow({
        "event_id": "GLOF-1",
        "event_date": "2020-07-01",
        "latitude": "28.1",
        "longitude": "85.4",
        "impact": "test record",
    })
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("events.csv", buffer.getvalue())


def create_main_and_removed_zip(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Database/GLOFs/HMAGLOFDB.csv",
            "GF_ID,event_date,latitude,longitude\nGF-1,2020-07-01,28.1,85.4\n",
        )
        archive.writestr(
            "Database/GLOFs/HMAGLOFDB_removed.csv",
            "GF_ID,event_date,latitude,longitude\nGF-X,1900,28.0,85.0\n",
        )


def test_icimod_tabular_import_is_idempotent(app, tmp_path):
    archive_path = tmp_path / "glof.zip"
    create_csv_zip(archive_path)
    with app.app_context():
        database = get_db()
        register_dataset(
            database,
            "hkh-glof-inventory",
            "https://rds.icimod.org/download/test.zip",
        )
        assert import_dataset_file(
            database, "hkh-glof-inventory", archive_path
        ) == 1
        assert import_dataset_file(
            database, "hkh-glof-inventory", archive_path
        ) == 1
        dataset = database.execute(
            "SELECT * FROM external_datasets WHERE dataset_key = 'hkh-glof-inventory'"
        ).fetchone()
        assert dataset["status"] == "imported"
        assert dataset["record_count"] == 1
        record = database.execute(
            "SELECT * FROM external_dataset_records WHERE dataset_id = ?",
            (dataset["id"],),
        ).fetchone()
        assert record["record_key"] == "events.csv:GLOF-1"
        assert record["observed_at"] == "2020-07-01"


def test_icimod_dataset_and_record_apis(app, tmp_path):
    archive_path = tmp_path / "glof.zip"
    create_csv_zip(archive_path)
    with app.app_context():
        database = get_db()
        register_dataset(database, "hkh-glof-inventory")
        import_dataset_file(database, "hkh-glof-inventory", archive_path)

    client = app.test_client()
    datasets = client.get("/api/icimod/datasets").get_json()
    assert datasets["count"] == 1
    assert datasets["data"][0]["license"] == "CC BY 4.0"
    records = client.get(
        "/api/icimod/datasets/hkh-glof-inventory/records"
    ).get_json()
    assert records["count"] == 1
    assert records["data"][0]["properties"]["impact"] == "test record"


def test_icimod_records_validate_pagination(client):
    response = client.get(
        "/api/icimod/datasets/hkh-glof-inventory/records?limit=1000"
    )
    assert response.status_code == 400


def test_removed_glof_rows_are_not_positive_events(app, tmp_path):
    archive_path = tmp_path / "glof.zip"
    create_main_and_removed_zip(archive_path)
    with app.app_context():
        database = get_db()
        register_dataset(database, "hkh-glof-inventory")
        assert import_dataset_file(database, "hkh-glof-inventory", archive_path) == 2
        record_types = {
            row["record_type"]
            for row in database.execute("SELECT record_type FROM external_dataset_records")
        }
        assert record_types == {"glof_event", "excluded_glof_event"}


def test_duplicate_source_ids_preserve_every_row(app, tmp_path):
    archive_path = tmp_path / "glof.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "events.csv",
            "GF_ID,event_date,latitude,longitude\n"
            "GF-1,2020-07-01,28.1,85.4\n"
            "GF-1,2020-07-02,28.2,85.5\n",
        )
    with app.app_context():
        database = get_db()
        register_dataset(database, "hkh-glof-inventory")
        assert import_dataset_file(database, "hkh-glof-inventory", archive_path) == 2
        keys = [
            row["record_key"]
            for row in database.execute(
                "SELECT record_key FROM external_dataset_records ORDER BY id"
            )
        ]
        assert keys == ["events.csv:GF-1", "events.csv:GF-1:row-2"]


def test_hmaglof_coordinate_field_names_are_normalized(app, tmp_path):
    archive_path = tmp_path / "glof.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "HMAGLOFDB.csv",
            "GF_ID,Year_approx,Lat_lake,Lon_lake\nGF-1,2020,28.1,85.4\n",
        )
    with app.app_context():
        database = get_db()
        register_dataset(database, "hkh-glof-inventory")
        import_dataset_file(database, "hkh-glof-inventory", archive_path)
        record = database.execute(
            "SELECT latitude, longitude, observed_at FROM external_dataset_records"
        ).fetchone()
        assert record["latitude"] == 28.1
        assert record["longitude"] == 85.4
        assert record["observed_at"] == "2020"


def test_landcover_sync_samples_all_published_years(app, monkeypatch):
    service = {
        "layers": [
            {"id": 0, "name": "Landcover_2000"},
            {"id": 1, "name": "Landcover_2001"},
            {"id": 2, "name": "Landcover_2013"},
        ]
    }

    def fake_fetch(url):
        if url.endswith("?f=pjson"):
            return service
        if url.endswith("/legend?f=pjson"):
            return {"layers": []}
        return {
            "results": [
                {
                    "layerId": layer["id"],
                    "layerName": layer["name"],
                    "attributes": {"Raster.Value": "2"},
                }
                for layer in service["layers"]
            ]
        }

    monkeypatch.setattr(icimod, "_fetch_json", fake_fetch)
    with app.app_context():
        result = icimod.sync_landcover_service(get_db())
        nepal_assets = get_db().execute(
            "SELECT COUNT(*) FROM assets WHERE country = 'Nepal'"
        ).fetchone()[0]
        assert result["records"] == nepal_assets * 3

    response = app.test_client().get("/api/assets/imja-tsho/land-cover")
    assert response.status_code == 200
    body = response.get_json()
    assert body["available"] is True
    assert body["count"] == 3
    assert body["data"][0]["properties"]["class_name"] == "Glacier"
