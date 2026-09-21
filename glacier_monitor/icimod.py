import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click
import shapefile
from flask import Blueprint, current_app, jsonify, request
from flask.cli import with_appcontext
from openpyxl import load_workbook

from .db import get_db


bp = Blueprint("icimod", __name__)

DATASETS = {
    "hkh-glof-inventory": {
        "uuid": "8881454b-6f7c-461b-95c2-eaf7618230d9",
        "title": "High Mountain Asia GLOF inventory, 1533–2025",
        "doi": "https://doi.org/10.26066/RDS.1973283",
        "license": "CC BY 4.0",
        "record_type": "glof_event",
    },
    "hkh-glacier-change-1990-2020": {
        "uuid": "b80287b9-2978-48c2-b386-bd5f1449d84f",
        "title": "Decadal glacier changes from 1990 to 2020 in the Hindu Kush Himalaya",
        "doi": "https://doi.org/10.26066/rds.1973447",
        "license": "CC BY 4.0",
        "record_type": "glacier_change",
    },
    "nepal-land-cover-2000-2022": {
        "uuid": "073f1390-3b0e-4320-8446-8466ad118c59",
        "title": "Annual land cover of Nepal, 2000–2022",
        "doi": "https://doi.org/10.26066/RDS.1972729",
        "license": "CC BY 4.0",
        "record_type": "land_cover",
    },
    "kavrepalanchok-springs": {
        "uuid": "83dbf4e1-b048-426d-9284-36a982a8f639",
        "title": "Kavrepalanchok springshed water-source inventory",
        "doi": "https://doi.org/10.26066/RDS.1974131",
        "license": "CC BY 4.0",
        "record_type": "spring",
    },
}

MAX_RECORDS = 500000
LANDCOVER_SERVICE = (
    "https://geoapps.icimod.org/icimodarcgis/rest/services/"
    "Nepal/Landcover/MapServer"
)
LANDCOVER_CLASSES = {
    1: "Water",
    2: "Glacier",
    3: "Snow",
    4: "Forest",
    5: "Riverbed",
    6: "Built-up",
    7: "Cropland",
    8: "Bare soil",
    9: "Bare rock",
    10: "Grassland",
    11: "Other wooded land",
}


def metadata_url(dataset):
    return f"https://rds.icimod.org/metadata/{dataset['uuid']}"


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not cleaned:
        raise ValueError("download filename is invalid")
    return cleaned[:180]


def download_dataset(url, destination, max_bytes):
    request_object = Request(
        url, headers={"User-Agent": "HimalayaWatch/0.4 research-decision-support"}
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    downloaded = 0
    try:
        with urlopen(request_object, timeout=60) as response, temporary.open("wb") as output:
            header_size = response.headers.get("Content-Length")
            if header_size and int(header_size) > max_bytes:
                raise ValueError("dataset exceeds configured download limit")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise ValueError("dataset exceeds configured download limit")
                output.write(chunk)
    except (HTTPError, URLError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"ICIMOD download failed: {exc}") from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(destination)
    return {
        "path": destination,
        "bytes": downloaded,
        "sha256": sha256_file(destination),
    }


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _record_key(properties, index, prefix):
    candidates = (
        "event_id", "Event_ID", "GLOF_ID", "GF_ID", "GL_ID", "glacier_id",
        "RGIId", "id", "ID",
        "OBJECTID", "ObjectID", "FID", "fid", "source_id", "Source_ID",
    )
    for field in candidates:
        value = properties.get(field)
        if value not in (None, ""):
            return f"{prefix}:{value}"
    canonical = json.dumps(properties, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{index}:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def _observed_at(properties):
    for key, value in properties.items():
        lowered = key.lower()
        if value not in (None, "") and any(
            token in lowered for token in ("event_date", "occurrence", "observed", "acquisition", "year")
        ):
            return str(value)[:40]
    return None


def _geometry_summary(geometry):
    if not geometry:
        return None, None, None
    coordinates = []

    def visit(value):
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and all(isinstance(item, (int, float)) for item in value[:2])
        ):
            coordinates.append((float(value[0]), float(value[1])))
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(geometry.get("coordinates"))
    if not coordinates:
        return None, None, json.dumps({"type": geometry.get("type")})
    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]
    bbox = [min(longitudes), min(latitudes), max(longitudes), max(latitudes)]
    longitude = (bbox[0] + bbox[2]) / 2
    latitude = (bbox[1] + bbox[3]) / 2
    return latitude, longitude, json.dumps({
        "type": geometry.get("type"),
        "bbox": bbox,
        "centroid": [longitude, latitude],
    })


def _property_coordinates(properties):
    lowered = {str(key).lower(): value for key, value in properties.items()}
    latitude = next(
        (
            lowered[key]
            for key in ("latitude", "lat", "lat_lake", "lat_impact", "y")
            if key in lowered
        ),
        None,
    )
    longitude = next(
        (
            lowered[key]
            for key in ("longitude", "lon", "lng", "long", "lon_lake", "lon_impact", "x")
            if key in lowered
        ),
        None,
    )
    try:
        latitude = float(latitude) if latitude not in (None, "") else None
        longitude = float(longitude) if longitude not in (None, "") else None
    except (TypeError, ValueError):
        return None, None
    if latitude is not None and not -90 <= latitude <= 90:
        latitude = None
    if longitude is not None and not -180 <= longitude <= 180:
        longitude = None
    return latitude, longitude


def _csv_records(stream, member_name):
    text = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace")
    for index, row in enumerate(csv.DictReader(text), 1):
        yield member_name, index, {key: _json_safe(value) for key, value in row.items()}, None


def _json_records(stream, member_name):
    document = json.load(io.TextIOWrapper(stream, encoding="utf-8-sig"))
    if isinstance(document, dict) and document.get("type") == "FeatureCollection":
        for index, feature in enumerate(document.get("features", []), 1):
            yield member_name, index, feature.get("properties") or {}, feature.get("geometry")
    elif isinstance(document, list):
        for index, item in enumerate(document, 1):
            yield member_name, index, item if isinstance(item, dict) else {"value": item}, None
    elif isinstance(document, dict):
        records = next(
            (value for value in document.values() if isinstance(value, list)),
            None,
        )
        if records is None:
            yield member_name, 1, document, None
        else:
            for index, item in enumerate(records, 1):
                yield member_name, index, item if isinstance(item, dict) else {"value": item}, None


def _xlsx_records(stream, member_name):
    workbook = load_workbook(stream, read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        rows = sheet.iter_rows(values_only=True)
        headers = next(rows, None)
        if not headers:
            continue
        names = [str(value or f"column_{index + 1}") for index, value in enumerate(headers)]
        for index, values in enumerate(rows, 2):
            properties = {
                name: _json_safe(value) for name, value in zip(names, values)
            }
            if any(value not in (None, "") for value in properties.values()):
                yield f"{member_name}:{sheet.title}", index, properties, None


def _shapefile_records(archive, shp_member):
    base = shp_member[:-4]
    components = {}
    for extension in (".shp", ".shx", ".dbf"):
        name = next(
            (candidate for candidate in archive.namelist() if candidate.lower() == (base + extension).lower()),
            None,
        )
        if name:
            components[extension] = io.BytesIO(archive.read(name))
    if ".shp" not in components or ".dbf" not in components:
        return
    reader = shapefile.Reader(
        shp=components[".shp"],
        shx=components.get(".shx"),
        dbf=components[".dbf"],
        encoding="utf-8",
        encodingErrors="replace",
    )
    fields = [field[0] for field in reader.fields[1:]]
    for index, shape_record in enumerate(reader.iterShapeRecords(), 1):
        properties = {
            key: _json_safe(value)
            for key, value in zip(fields, shape_record.record)
        }
        yield shp_member, index, properties, shape_record.shape.__geo_interface__


def iter_records(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            shapefiles = [name for name in archive.namelist() if name.lower().endswith(".shp")]
            handled_bases = {name[:-4].lower() for name in shapefiles}
            for name in shapefiles:
                yield from _shapefile_records(archive, name)
            for name in archive.namelist():
                lowered = name.lower()
                if any(lowered == base + extension for base in handled_bases for extension in (".shp", ".shx", ".dbf")):
                    continue
                if lowered.endswith(".csv"):
                    with archive.open(name) as stream:
                        yield from _csv_records(stream, name)
                elif lowered.endswith((".json", ".geojson")):
                    with archive.open(name) as stream:
                        yield from _json_records(stream, name)
                elif lowered.endswith(".xlsx"):
                    with archive.open(name) as stream:
                        yield from _xlsx_records(io.BytesIO(stream.read()), name)
    elif suffix == ".csv":
        with path.open("rb") as stream:
            yield from _csv_records(stream, path.name)
    elif suffix in {".json", ".geojson"}:
        with path.open("rb") as stream:
            yield from _json_records(stream, path.name)
    elif suffix == ".xlsx":
        with path.open("rb") as stream:
            yield from _xlsx_records(stream, path.name)


def import_dataset_file(database, dataset_key, path):
    dataset = DATASETS[dataset_key]
    row = database.execute(
        "SELECT id FROM external_datasets WHERE dataset_key = ?", (dataset_key,)
    ).fetchone()
    if not row:
        raise ValueError("dataset must be registered before import")
    records = []
    record_keys = set()
    for member_name, index, raw_properties, geometry in iter_records(path):
        if len(records) >= MAX_RECORDS:
            raise ValueError(f"dataset exceeds the {MAX_RECORDS}-record import limit")
        properties = {str(key): _json_safe(value) for key, value in raw_properties.items()}
        latitude, longitude, geometry_json = _geometry_summary(geometry)
        if latitude is None or longitude is None:
            latitude, longitude = _property_coordinates(properties)
        canonical = json.dumps(
            {"properties": properties, "geometry": json.loads(geometry_json) if geometry_json else None},
            sort_keys=True, separators=(",", ":"),
        )
        record_type = dataset["record_type"]
        if record_type == "glof_event" and "removed" in member_name.lower():
            record_type = "excluded_glof_event"
        record_key = _record_key(properties, index, member_name)
        if record_key in record_keys:
            record_key = f"{record_key}:row-{index}"
        record_keys.add(record_key)
        records.append((
            row["id"], record_key,
            record_type, _observed_at(properties), latitude, longitude,
            json.dumps(properties, sort_keys=True), geometry_json,
            hashlib.sha256(canonical.encode()).hexdigest(), utcnow(),
        ))
    database.execute("DELETE FROM external_dataset_records WHERE dataset_id = ?", (row["id"],))
    database.executemany(
        """INSERT INTO external_dataset_records
           (dataset_id, record_key, record_type, observed_at, latitude, longitude,
            properties_json, geometry_json, content_sha256, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        records,
    )
    status = "imported" if records else "downloaded"
    database.execute(
        """UPDATE external_datasets
           SET status = ?, record_count = ?, imported_at = ?, error_message = NULL,
               updated_at = ? WHERE id = ?""",
        (status, len(records), utcnow(), utcnow(), row["id"]),
    )
    database.commit()
    return len(records)


def register_dataset(database, dataset_key, download_url=None):
    dataset = DATASETS[dataset_key]
    now = utcnow()
    database.execute(
        """INSERT INTO external_datasets
           (dataset_key, provider, title, dataset_uuid, doi, metadata_url,
            download_url, license, status, metadata_json, created_at, updated_at)
           VALUES (?, 'ICIMOD', ?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)
           ON CONFLICT(dataset_key) DO UPDATE SET
             title = excluded.title, doi = excluded.doi,
             metadata_url = excluded.metadata_url,
             download_url = COALESCE(excluded.download_url, external_datasets.download_url),
             license = excluded.license, metadata_json = excluded.metadata_json,
             updated_at = excluded.updated_at""",
        (
            dataset_key, dataset["title"], dataset["uuid"], dataset["doi"],
            metadata_url(dataset), download_url, dataset["license"],
            json.dumps({"record_type": dataset["record_type"]}), now, now,
        ),
    )
    database.commit()


def register_catalog(database):
    for dataset_key, dataset in DATASETS.items():
        register_dataset(database, dataset_key)
        row = database.execute(
            "SELECT status FROM external_datasets WHERE dataset_key = ?", (dataset_key,)
        ).fetchone()
        if row["status"] == "registered":
            database.execute(
                """UPDATE external_datasets
                   SET status = 'metadata_only', error_message = ?,
                       metadata_json = ?, updated_at = ?
                   WHERE dataset_key = ?""",
                (
                    "The ICIMOD RDS archive requires account authentication and "
                    "purpose-of-use acknowledgement.",
                    json.dumps({
                        "record_type": dataset["record_type"],
                        "download_flow": f"https://rds.icimod.org/download/flow/{dataset['uuid']}",
                        "machine_metadata": f"https://rds.icimod.org/geoapi/metadata/{dataset['uuid']}",
                        "authentication_required": True,
                    }),
                    utcnow(), dataset_key,
                ),
            )
    database.commit()


def _fetch_json(url):
    request_object = Request(
        url, headers={"User-Agent": "HimalayaWatch/0.4 research-decision-support"}
    )
    try:
        with urlopen(request_object, timeout=60) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ICIMOD public service request failed: {exc}") from exc


def sync_landcover_service(database):
    register_dataset(database, "nepal-land-cover-2000-2022", LANDCOVER_SERVICE)
    service = _fetch_json(f"{LANDCOVER_SERVICE}?f=pjson")
    legend = _fetch_json(f"{LANDCOVER_SERVICE}/legend?f=pjson")
    layer_years = {
        int(layer["id"]): int(layer["name"].rsplit("_", 1)[1])
        for layer in service.get("layers", [])
        if re.fullmatch(r"Landcover_\d{4}", layer.get("name", ""))
    }
    if not layer_years:
        raise RuntimeError("ICIMOD land-cover service returned no annual raster layers")
    dataset_row = database.execute(
        "SELECT id FROM external_datasets WHERE dataset_key = 'nepal-land-cover-2000-2022'"
    ).fetchone()
    database.execute(
        "DELETE FROM external_dataset_records WHERE dataset_id = ?", (dataset_row["id"],)
    )
    records = []
    samples = []
    assets = database.execute(
        "SELECT id, slug, name, latitude, longitude FROM assets WHERE country = 'Nepal'"
    ).fetchall()
    for asset in assets:
        delta = 0.01
        query = urlencode({
            "geometry": f"{asset['longitude']},{asset['latitude']}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": (
                f"{asset['longitude'] - delta},{asset['latitude'] - delta},"
                f"{asset['longitude'] + delta},{asset['latitude'] + delta}"
            ),
            "imageDisplay": "400,400,96",
            "returnGeometry": "false",
            "f": "json",
        })
        response = _fetch_json(f"{LANDCOVER_SERVICE}/identify?{query}")
        samples.append({"asset": asset["slug"], "results": response.get("results", [])})
        for item in response.get("results", []):
            layer_id = int(item["layerId"])
            if layer_id not in layer_years:
                continue
            raw_value = item.get("attributes", {}).get("Raster.Value")
            try:
                class_value = int(float(raw_value))
            except (TypeError, ValueError):
                class_value = None
            year = layer_years[layer_id]
            properties = {
                "asset_id": asset["id"],
                "asset_slug": asset["slug"],
                "asset_name": asset["name"],
                "year": year,
                "class_value": class_value,
                "class_name": LANDCOVER_CLASSES.get(class_value, "Unknown"),
                "service_layer_id": layer_id,
                "service_layer_name": item.get("layerName"),
                "source_url": f"{LANDCOVER_SERVICE}/{layer_id}",
                "sampling_method": "ArcGIS identify at monitored asset coordinate",
                "missing_2012": 2012 not in layer_years.values(),
            }
            canonical = json.dumps(properties, sort_keys=True, separators=(",", ":"))
            records.append((
                dataset_row["id"], f"{asset['slug']}:{year}", "land_cover_sample",
                f"{year}-01-01", asset["latitude"], asset["longitude"],
                json.dumps(properties, sort_keys=True),
                json.dumps({"type": "Point", "coordinates": [
                    asset["longitude"], asset["latitude"],
                ]}),
                hashlib.sha256(canonical.encode()).hexdigest(), utcnow(),
            ))
    database.executemany(
        """INSERT INTO external_dataset_records
           (dataset_id, record_key, record_type, observed_at, latitude, longitude,
            properties_json, geometry_json, content_sha256, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        records,
    )
    snapshot = {
        "retrieved_at": utcnow(),
        "service_url": LANDCOVER_SERVICE,
        "service": service,
        "legend": legend,
        "samples": samples,
        "limitations": [
            "Point samples from the public ArcGIS service; not the gated full RDS raster archive.",
            "The service publishes 2000-2011 and 2013-2022; 2012 is absent.",
        ],
    }
    destination = (
        Path(current_app.instance_path) / "external-data" / "icimod"
        / "nepal-land-cover-2000-2022" / "arcgis-service-snapshot.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot, sort_keys=True, indent=2).encode()
    destination.write_bytes(serialized)
    now = utcnow()
    database.execute(
        """UPDATE external_datasets
           SET download_url = ?, file_path = ?, file_sha256 = ?, byte_count = ?,
               media_type = '.json', status = 'imported', record_count = ?,
               downloaded_at = ?, imported_at = ?, error_message = NULL,
               metadata_json = ?, updated_at = ?
           WHERE dataset_key = 'nepal-land-cover-2000-2022'""",
        (
            LANDCOVER_SERVICE,
            str(destination.relative_to(Path(current_app.instance_path))),
            hashlib.sha256(serialized).hexdigest(), len(serialized), len(records),
            now, now,
            json.dumps({
                "record_type": "land_cover_sample",
                "years": sorted(layer_years.values()),
                "missing_years": [2012],
                "classes": LANDCOVER_CLASSES,
                "full_rds_archive_authentication_required": True,
            }),
            now,
        ),
    )
    database.commit()
    return {"records": len(records), "assets": len(assets), "years": len(layer_years)}


def sync_dataset(database, dataset_key, download_url, filename=None):
    register_dataset(database, dataset_key, download_url)
    dataset = DATASETS[dataset_key]
    filename = safe_filename(filename or Path(download_url.split("?", 1)[0]).name or f"{dataset_key}.zip")
    destination = Path(current_app.instance_path) / "external-data" / "icimod" / dataset_key / filename
    max_bytes = int(current_app.config.get("ICIMOD_MAX_DOWNLOAD_MB", 2048)) * 1024 * 1024
    try:
        downloaded = download_dataset(download_url, destination, max_bytes)
        database.execute(
            """UPDATE external_datasets SET file_path = ?, file_sha256 = ?, byte_count = ?,
                      media_type = ?, status = 'downloaded', downloaded_at = ?,
                      error_message = NULL, updated_at = ? WHERE dataset_key = ?""",
            (
                str(destination.relative_to(Path(current_app.instance_path))),
                downloaded["sha256"], downloaded["bytes"], destination.suffix.lower(),
                utcnow(), utcnow(), dataset_key,
            ),
        )
        database.commit()
        count = import_dataset_file(database, dataset_key, destination)
        return {"dataset": dataset_key, "records": count, **downloaded}
    except Exception as exc:
        database.execute(
            """UPDATE external_datasets SET status = 'failed', error_message = ?,
                      updated_at = ? WHERE dataset_key = ?""",
            (str(exc)[:1000], utcnow(), dataset_key),
        )
        database.commit()
        raise


@bp.get("/api/icimod/datasets")
def datasets_api():
    rows = []
    for row in get_db().execute(
        "SELECT * FROM external_datasets WHERE provider = 'ICIMOD' ORDER BY dataset_key"
    ):
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.get("/api/icimod/datasets/<dataset_key>/records")
def dataset_records_api(dataset_key):
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return jsonify({"error": {"message": "limit and offset must be integers", "status": 400}}), 400
    if not 1 <= limit <= 500 or not 0 <= offset <= 500000:
        return jsonify({"error": {"message": "limit or offset is outside the accepted range", "status": 400}}), 400
    dataset = get_db().execute(
        "SELECT id FROM external_datasets WHERE dataset_key = ?", (dataset_key,)
    ).fetchone()
    if not dataset:
        return jsonify({"error": {"message": "dataset not found", "status": 404}}), 404
    rows = []
    for row in get_db().execute(
        """SELECT record_key, record_type, observed_at, latitude, longitude,
                  properties_json, geometry_json, content_sha256
           FROM external_dataset_records WHERE dataset_id = ?
           ORDER BY id LIMIT ? OFFSET ?""",
        (dataset["id"], limit, offset),
    ):
        item = dict(row)
        item["properties"] = json.loads(item.pop("properties_json"))
        item["geometry"] = json.loads(item.pop("geometry_json")) if item["geometry_json"] else None
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows), "offset": offset})


@bp.get("/api/assets/<slug>/land-cover")
def asset_land_cover_api(slug):
    database = get_db()
    asset = database.execute(
        "SELECT id, slug, name, country FROM assets WHERE slug = ?", (slug,)
    ).fetchone()
    if not asset:
        return jsonify({"error": {"message": "asset not found", "status": 404}}), 404
    dataset = database.execute(
        """SELECT id, dataset_key, title, doi, license, status, imported_at,
                  metadata_json
           FROM external_datasets
           WHERE dataset_key = 'nepal-land-cover-2000-2022'"""
    ).fetchone()
    if not dataset:
        return jsonify({
            "data": [],
            "count": 0,
            "asset": dict(asset),
            "available": False,
            "message": "Run `python -m flask --app app sync-icimod-public` to load the public ICIMOD service.",
        })
    rows = []
    for row in database.execute(
        """SELECT observed_at, latitude, longitude, properties_json,
                  content_sha256
           FROM external_dataset_records
           WHERE dataset_id = ?
             AND record_type = 'land_cover_sample'
             AND json_extract(properties_json, '$.asset_slug') = ?
           ORDER BY observed_at""",
        (dataset["id"], slug),
    ):
        item = dict(row)
        item["properties"] = json.loads(item.pop("properties_json"))
        rows.append(item)
    dataset_item = dict(dataset)
    dataset_item["metadata"] = json.loads(dataset_item.pop("metadata_json"))
    return jsonify({
        "data": rows,
        "count": len(rows),
        "asset": dict(asset),
        "dataset": dataset_item,
        "available": bool(rows),
        "message": (
            "Official ICIMOD ArcGIS point samples; not the authenticated full-raster archive."
            if rows else "No Nepal land-cover samples are available for this asset."
        ),
    })


@click.command("sync-icimod-data")
@click.option("--dataset", "dataset_key", type=click.Choice(sorted(DATASETS)))
@click.option("--download-url", help="Official ICIMOD download URL; required for one-dataset manual sync.")
@click.option("--filename", help="Optional local filename override.")
@with_appcontext
def sync_icimod_data_command(dataset_key, download_url, filename):
    """Download and import an official ICIMOD dataset."""
    if not dataset_key or not download_url:
        raise click.ClickException(
            "--dataset and --download-url are required until all official endpoints are registered"
        )
    try:
        result = sync_dataset(get_db(), dataset_key, download_url, filename)
    except (RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"{dataset_key}: {result['bytes']} bytes, SHA-256 {result['sha256']}, "
        f"{result['records']} normalized records"
    )


@click.command("sync-icimod-public")
@with_appcontext
def sync_icimod_public_command():
    """Synchronize metadata plus public associated datasets and services."""
    database = get_db()
    register_catalog(database)
    glof = sync_dataset(
        database,
        "hkh-glof-inventory",
        "https://zenodo.org/api/records/18257243/files/"
        "fidelsteiner/HMAGLOFDB-v1.3.0.zip/content",
        "HMAGLOFDB-v1.3.0.zip",
    )
    database.execute(
        """UPDATE external_datasets SET license = ?, metadata_json = ?, updated_at = ?
           WHERE dataset_key = 'hkh-glof-inventory'""",
        (
            "CC BY 4.0 (ICIMOD RDS); CC0 (associated Zenodo v1.3.0 release)",
            json.dumps({
                "record_type": "glof_event",
                "download_source": "Zenodo release 18257243",
                "release_version": "v1.3.0",
                "release_doi": "https://doi.org/10.5281/zenodo.18257243",
                "icimod_rds_download_authentication_required": True,
            }),
            utcnow(),
        ),
    )
    database.commit()
    landcover = sync_landcover_service(database)
    click.echo(
        f"GLOF: {glof['records']} records; Nepal land cover: "
        f"{landcover['records']} asset-year samples. Glacier-change and springs "
        "archives remain metadata-only pending authenticated ICIMOD download."
    )


def init_app(app):
    app.config.setdefault("ICIMOD_MAX_DOWNLOAD_MB", 2048)
    app.cli.add_command(sync_icimod_data_command)
    app.cli.add_command(sync_icimod_public_command)
