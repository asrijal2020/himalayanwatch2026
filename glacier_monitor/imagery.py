import calendar
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from flask import Blueprint, current_app, jsonify, render_template, request
from flask.cli import with_appcontext

from .db import get_db, refresh_demo_imagery

bp = Blueprint("imagery", __name__)

COUNTRIES = {"Nepal", "Tibet", "India", "Pakistan"}
DEFAULT_LIMIT = 24
MAX_LIMIT = 100


def month_shift(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def rolling_start(today=None):
    return month_shift(today or date.today(), -3)


def parse_date_arg(value, name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date") from exc


def parse_pagination(args):
    try:
        limit = int(args.get("limit", DEFAULT_LIMIT))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    cursor_value = args.get("cursor")
    page_value = args.get("page")
    if cursor_value and page_value:
        raise ValueError("use cursor or page, not both")
    try:
        if cursor_value is not None:
            offset = int(cursor_value)
        else:
            page = int(page_value or 1)
            if page < 1:
                raise ValueError
            offset = (page - 1) * limit
    except ValueError as exc:
        raise ValueError("cursor must be non-negative and page must be at least 1") from exc
    if offset < 0 or offset > 100000:
        raise ValueError("cursor is outside the accepted range")
    return limit, offset


def parse_bbox(value):
    if not value:
        return None
    try:
        parts = [float(item) for item in value.split(",")]
    except ValueError as exc:
        raise ValueError("bbox must contain four comma-separated numbers") from exc
    if len(parts) != 4:
        raise ValueError("bbox must contain min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = parts
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError("bbox coordinates or ordering are invalid")
    return parts


def error(message):
    return jsonify({"error": {"message": message, "status": 400}}), 400


@bp.get("/imagery")
def imagery_page():
    return render_template("imagery.html", initial_glacier_id="")


@bp.get("/imagery/glaciers/<glacier_id>")
def imagery_glacier_page(glacier_id):
    return render_template("imagery.html", initial_glacier_id=glacier_id)


@bp.get("/api/glaciers")
def glaciers_api():
    try:
        limit, offset = parse_pagination(request.args)
        bbox = parse_bbox(request.args.get("bbox"))
    except ValueError as exc:
        return error(str(exc))

    clauses = []
    params = []
    country = request.args.get("country")
    if country:
        if country not in COUNTRIES:
            return error("country must be Nepal, Tibet, India, or Pakistan")
        clauses.append("country = ?")
        params.append(country)
    region = request.args.get("region")
    if region:
        if len(region) > 100:
            return error("region must be 100 characters or fewer")
        clauses.append("region = ?")
        params.append(region)
    query_text = request.args.get("q", "").strip()
    if len(query_text) > 100:
        return error("q must be 100 characters or fewer")
    if query_text:
        clauses.append("(name LIKE ? OR inventory_id LIKE ?)")
        params.extend((f"%{query_text}%", f"%{query_text}%"))
    if bbox:
        clauses.append(
            "longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?"
        )
        params.extend((bbox[0], bbox[2], bbox[1], bbox[3]))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    database = get_db()
    total = database.execute(
        f"SELECT COUNT(*) FROM glacier_inventory{where}", params
    ).fetchone()[0]
    rows = [
        dict(row) for row in database.execute(
            f"""SELECT * FROM glacier_inventory{where}
                ORDER BY country, region, inventory_id LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
    ]
    for item in rows:
        item["display_name"] = item["name"] or item["inventory_id"]
        item["is_demo"] = bool(item["is_demo"])
    catalog = database.execute(
        "SELECT * FROM glacier_catalogs WHERE catalog_key = 'default'"
    ).fetchone()
    catalog_data = dict(catalog) if catalog else None
    if catalog_data:
        catalog_data["is_authoritative"] = bool(catalog_data["is_authoritative"])
    next_cursor = offset + len(rows) if offset + len(rows) < total else None
    return jsonify({
        "data": rows,
        "count": len(rows),
        "total": total,
        "next_cursor": next_cursor,
        "catalog": catalog_data,
    })


@bp.get("/api/glaciers/<glacier_id>")
def glacier_detail_api(glacier_id):
    database = get_db()
    glacier = database.execute(
        "SELECT * FROM glacier_inventory WHERE inventory_id = ?", (glacier_id,)
    ).fetchone()
    if not glacier:
        return jsonify({"error": {"message": "glacier not found", "status": 404}}), 404
    item = dict(glacier)
    item["display_name"] = item["name"] or item["inventory_id"]
    item["is_demo"] = bool(item["is_demo"])
    item["imagery_count"] = database.execute(
        "SELECT COUNT(*) FROM glacier_imagery WHERE glacier_id = ?", (glacier_id,)
    ).fetchone()[0]
    return jsonify({"data": item})


@bp.get("/api/glacier-imagery")
def imagery_api():
    today = date.today()
    earliest = rolling_start(today)
    try:
        limit, offset = parse_pagination(request.args)
        bbox = parse_bbox(request.args.get("bbox"))
        start = parse_date_arg(request.args.get("start_date", earliest.isoformat()), "start_date")
        end = parse_date_arg(request.args.get("end_date", today.isoformat()), "end_date")
    except ValueError as exc:
        return error(str(exc))
    if start > end:
        return error("start_date cannot be after end_date")
    if start < earliest or end > today:
        return error(
            f"date range must remain within the rolling three-month window "
            f"{earliest.isoformat()} to {today.isoformat()}"
        )

    clauses = ["date(i.acquired_at) BETWEEN ? AND ?"]
    params = [start.isoformat(), end.isoformat()]
    country = request.args.get("country")
    if country:
        if country not in COUNTRIES:
            return error("country must be Nepal, Tibet, India, or Pakistan")
        clauses.append("g.country = ?")
        params.append(country)
    for key, column in (("region", "g.region"), ("glacier_id", "i.glacier_id"), ("source", "i.source")):
        value = request.args.get(key)
        if value:
            if len(value) > 150:
                return error(f"{key} must be 150 characters or fewer")
            clauses.append(f"{column} = ?")
            params.append(value)
    cloud_value = request.args.get("max_cloud_cover")
    if cloud_value is not None:
        try:
            cloud = float(cloud_value)
        except ValueError:
            return error("max_cloud_cover must be a number")
        if not 0 <= cloud <= 100:
            return error("max_cloud_cover must be between 0 and 100")
        clauses.append("i.cloud_cover <= ?")
        params.append(cloud)
    query_text = request.args.get("q", "").strip()
    if len(query_text) > 100:
        return error("q must be 100 characters or fewer")
    if query_text:
        clauses.append("(g.name LIKE ? OR g.inventory_id LIKE ? OR i.provider_item_id LIKE ?)")
        params.extend((f"%{query_text}%",) * 3)
    if bbox:
        clauses.append("g.longitude BETWEEN ? AND ? AND g.latitude BETWEEN ? AND ?")
        params.extend((bbox[0], bbox[2], bbox[1], bbox[3]))

    where = " AND ".join(clauses)
    database = get_db()
    base = f"""FROM glacier_imagery i
        JOIN glacier_inventory g ON g.inventory_id = i.glacier_id WHERE {where}"""
    total = database.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    rows = database.execute(
        f"""SELECT i.*, g.name AS glacier_name, g.country, g.region,
                   g.latitude, g.longitude, g.is_demo AS glacier_is_demo
            {base} ORDER BY i.acquired_at DESC, i.id DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    data = []
    for row in rows:
        item = dict(row)
        item["display_name"] = item["glacier_name"] or item["glacier_id"]
        item["is_synthetic"] = bool(item["is_synthetic"])
        item["glacier_is_demo"] = bool(item["glacier_is_demo"])
        item["metadata"] = json.loads(item.pop("metadata_json"))
        data.append(item)
    next_cursor = offset + len(data) if offset + len(data) < total else None
    return jsonify({
        "data": data,
        "count": len(data),
        "total": total,
        "next_cursor": next_cursor,
        "window": {"start": earliest.isoformat(), "end": today.isoformat()},
        "notice": (
            "Observation metadata does not predict hazards. Optical imagery may be "
            "obscured by cloud, shadow, seasonal snow, or terrain."
        ),
    })


def geometry_points(coordinates):
    if not isinstance(coordinates, list):
        return []
    if coordinates and isinstance(coordinates[0], (int, float)):
        return [coordinates]
    points = []
    for child in coordinates:
        points.extend(geometry_points(child))
    return points


@click.command("import-glacier-inventory")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--catalog-name", required=True)
@click.option("--catalog-version", required=True)
@click.option("--source-url", required=True)
@click.option("--country", type=click.Choice(sorted(COUNTRIES)))
@click.option("--id-field", default="RGIId", show_default=True)
@click.option("--name-field", default="Name", show_default=True)
@click.option("--country-field", default="country", show_default=True)
@click.option("--region-field", default="region", show_default=True)
@click.option("--replace", is_flag=True, help="Replace the current glacier and imagery catalog.")
@with_appcontext
def import_inventory_command(
    path, catalog_name, catalog_version, source_url, country,
    id_field, name_field, country_field, region_field, replace
):
    """Import a licensed GLIMS/RGI-style GeoJSON FeatureCollection."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise click.ClickException("Input must be a GeoJSON FeatureCollection.")
    database = get_db()
    if replace:
        database.execute("DELETE FROM glacier_imagery")
        database.execute("DELETE FROM glacier_inventory")
        database.execute("DELETE FROM glacier_catalogs")
    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    imported = 0
    skipped = 0
    for feature in payload["features"]:
        properties = feature.get("properties") or {}
        glacier_id = properties.get(id_field)
        record_country = country or properties.get(country_field)
        points = geometry_points((feature.get("geometry") or {}).get("coordinates"))
        if not glacier_id or record_country not in COUNTRIES or not points:
            skipped += 1
            continue
        longitudes = [float(point[0]) for point in points]
        latitudes = [float(point[1]) for point in points]
        min_lon, max_lon = min(longitudes), max(longitudes)
        min_lat, max_lat = min(latitudes), max(latitudes)
        database.execute(
            """INSERT OR REPLACE INTO glacier_inventory
               (inventory_id, name, country, region, latitude, longitude,
                min_longitude, min_latitude, max_longitude, max_latitude,
                source_name, source_url, catalog_version, ingested_at, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                str(glacier_id), properties.get(name_field) or None, record_country,
                properties.get(region_field) or "Unspecified",
                (min_lat + max_lat) / 2, (min_lon + max_lon) / 2,
                min_lon, min_lat, max_lon, max_lat,
                catalog_name, source_url, catalog_version, ingested_at,
            ),
        )
        imported += 1
    database.execute(
        """INSERT OR REPLACE INTO glacier_catalogs
           (catalog_key, catalog_name, catalog_version, spatial_boundary, ingested_at,
            coverage_statement, is_authoritative)
           VALUES ('default', ?, ?, ?, ?, ?, 1)""",
        (
            catalog_name, catalog_version,
            f"Imported records for {country or 'supported country values in source properties'}",
            ingested_at,
            f"{imported} source records imported; {skipped} records skipped due to "
            "missing IDs, country mapping, or geometry.",
        ),
    )
    database.commit()
    click.echo(f"Imported {imported} glaciers; skipped {skipped}.")


@click.command("refresh-imagery")
@click.option("--provider", type=click.Choice(["demo", "sentinel"]), default="demo")
@click.option("--glacier-id")
@click.option("--limit", type=click.IntRange(1, 100), default=20)
@click.option("--max-cloud-cover", type=click.FloatRange(0, 100), default=80.0)
@with_appcontext
def refresh_imagery_command(provider, glacier_id, limit, max_cloud_cover):
    """Refresh imagery metadata; Sentinel mode stores metadata/URLs, not image files."""
    if provider == "demo":
        database = get_db()
        refresh_demo_imagery(database)
        database.commit()
        count = database.execute(
            "SELECT COUNT(*) FROM glacier_imagery WHERE is_synthetic = 1"
        ).fetchone()[0]
        click.echo(f"Refreshed {count} deterministic demo imagery metadata records.")
        return
    if not glacier_id:
        raise click.ClickException("--glacier-id is required for bounded Sentinel refreshes.")
    database = get_db()
    glacier = database.execute(
        "SELECT * FROM glacier_inventory WHERE inventory_id = ?", (glacier_id,)
    ).fetchone()
    if not glacier:
        raise click.ClickException("Glacier ID not found.")
    endpoint = current_app.config["SENTINEL_STAC_URL"]
    today = date.today()
    request_body = json.dumps({
        "collections": ["sentinel-2-l2a"],
        "bbox": [
            glacier["min_longitude"], glacier["min_latitude"],
            glacier["max_longitude"], glacier["max_latitude"],
        ],
        "datetime": f"{rolling_start(today).isoformat()}T00:00:00Z/{today.isoformat()}T23:59:59Z",
        "limit": limit,
        "query": {"eo:cloud_cover": {"lte": max_cloud_cover}},
    }).encode("utf-8")
    stac_request = Request(
        endpoint, data=request_body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "HimalayaWatch/0.1"},
    )
    try:
        with urlopen(stac_request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Sentinel STAC request failed: {exc}") from exc
    available_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stored = 0
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        acquired_at = properties.get("datetime")
        if not acquired_at:
            continue
        assets = feature.get("assets") or {}
        thumbnail = (assets.get("thumbnail") or {}).get("href")
        self_link = next(
            (link.get("href") for link in feature.get("links", []) if link.get("rel") == "self"),
            "https://browser.dataspace.copernicus.eu/",
        )
        database.execute(
            """INSERT OR REPLACE INTO glacier_imagery
               (glacier_id, provider_item_id, acquired_at, available_at, source,
                processing_level, cloud_cover, thumbnail_url, thumbnail_kind,
                external_url, quality_status, is_synthetic, metadata_json, created_at)
               VALUES (?, ?, ?, ?, 'Copernicus Sentinel-2 STAC', ?, ?, ?, ?, ?,
                       'validated_metadata', 0, ?, ?)""",
            (
                glacier_id, feature["id"], acquired_at, available_at,
                properties.get("processing:level", "L2A"),
                properties.get("eo:cloud_cover"), thumbnail,
                "provider_quicklook" if thumbnail else "none",
                self_link,
                json.dumps({"collection": feature.get("collection")}),
                available_at,
            ),
        )
        stored += 1
    database.commit()
    click.echo(f"Stored {stored} Sentinel metadata records for {glacier_id}.")


def init_app(app):
    app.cli.add_command(import_inventory_command)
    app.cli.add_command(refresh_imagery_command)
