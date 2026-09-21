import json
from datetime import datetime, timedelta, timezone

import click
from flask import Blueprint, current_app, jsonify
from flask.cli import with_appcontext

from .db import get_db


bp = Blueprint("earth_engine", __name__)

DATASETS = {
    "elevation_m": {
        "dataset_id": "USGS/SRTMGL1_003",
        "band": "elevation",
        "units": "m",
        "scale_m": 90,
        "reducer": "mean",
        "classification": "satellite_or_reanalysis",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/USGS_SRTMGL1_003",
    },
    "temperature_mean_c": {
        "dataset_id": "ECMWF/ERA5_LAND/DAILY_AGGR",
        "band": "temperature_2m",
        "units": "degC",
        "scale_m": 11132,
        "reducer": "temporal_mean_spatial_mean",
        "classification": "modeled",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_LAND_DAILY_AGGR",
    },
    "precipitation_total_mm": {
        "dataset_id": "UCSB-CHG/CHIRPS/DAILY",
        "band": "precipitation",
        "units": "mm",
        "scale_m": 5566,
        "reducer": "temporal_sum_spatial_mean",
        "classification": "satellite_or_reanalysis",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY",
    },
    "snow_cover_mean_pct": {
        "dataset_id": "MODIS/061/MOD10A1",
        "band": "NDSI_Snow_Cover",
        "units": "percent",
        "scale_m": 500,
        "reducer": "temporal_mean_spatial_mean",
        "classification": "satellite_or_reanalysis",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD10A1",
    },
    "surface_water_occurrence_pct": {
        "dataset_id": "JRC/GSW1_4/GlobalSurfaceWater",
        "band": "occurrence",
        "units": "percent",
        "scale_m": 30,
        "reducer": "spatial_mean",
        "classification": "satellite_or_reanalysis",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_GlobalSurfaceWater",
    },
    "nearby_glacier_outline_count": {
        "dataset_id": "GLIMS/current",
        "band": None,
        "units": "count",
        "scale_m": 1,
        "reducer": "feature_count_within_5km",
        "classification": "satellite_or_reanalysis",
        "source_url": "https://developers.google.com/earth-engine/datasets/catalog/GLIMS_current",
    },
}


def initialize_earth_engine():
    try:
        import ee
    except ImportError as exc:
        raise RuntimeError(
            "earthengine-api is not installed; install requirements.txt"
        ) from exc
    project = current_app.config.get("GEE_PROJECT") or None
    service_account = current_app.config.get("GEE_SERVICE_ACCOUNT")
    key_file = current_app.config.get("GEE_PRIVATE_KEY_FILE")
    try:
        if service_account or key_file:
            if not service_account or not key_file:
                raise RuntimeError(
                    "GEE_SERVICE_ACCOUNT and GEE_PRIVATE_KEY_FILE must be configured together"
                )
            credentials = ee.ServiceAccountCredentials(service_account, key_file)
            ee.Initialize(credentials=credentials, project=project)
        else:
            ee.Initialize(project=project)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "Google Earth Engine is not authenticated or the configured project is not enabled"
        ) from exc
    return ee


def gee_status():
    try:
        ee = initialize_earth_engine()
        ee.Number(1).getInfo()
        return {
            "available": True,
            "authenticated": True,
            "project": current_app.config.get("GEE_PROJECT") or None,
            "datasets": DATASETS,
            "message": "Earth Engine initialized and a bounded server request succeeded.",
        }
    except RuntimeError as exc:
        return {
            "available": False,
            "authenticated": False,
            "project": current_app.config.get("GEE_PROJECT") or None,
            "datasets": DATASETS,
            "message": str(exc),
        }


def _reduce_value(image, band, region, scale, ee):
    return image.select(band).reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=region,
        scale=scale,
        bestEffort=True,
        maxPixels=100000,
    ).get(band).getInfo()


def collect_features(asset, days=14, now=None):
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    ee = initialize_earth_engine()
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    end = now.date().isoformat()
    start = (now - timedelta(days=days)).date().isoformat()
    region = ee.Geometry.Point([asset["longitude"], asset["latitude"]]).buffer(5000)

    elevation = _reduce_value(
        ee.Image(DATASETS["elevation_m"]["dataset_id"]),
        DATASETS["elevation_m"]["band"],
        region,
        DATASETS["elevation_m"]["scale_m"],
        ee,
    )
    era5 = ee.ImageCollection(DATASETS["temperature_mean_c"]["dataset_id"]).filterDate(
        start, end
    )
    temperature = _reduce_value(
        era5.select("temperature_2m").mean(),
        "temperature_2m",
        region,
        DATASETS["temperature_mean_c"]["scale_m"],
        ee,
    )
    precipitation = _reduce_value(
        ee.ImageCollection(DATASETS["precipitation_total_mm"]["dataset_id"])
        .filterDate(start, end)
        .select("precipitation")
        .sum(),
        "precipitation",
        region,
        DATASETS["precipitation_total_mm"]["scale_m"],
        ee,
    )
    snow = _reduce_value(
        ee.ImageCollection(DATASETS["snow_cover_mean_pct"]["dataset_id"])
        .filterDate(start, end)
        .select("NDSI_Snow_Cover")
        .mean(),
        "NDSI_Snow_Cover",
        region,
        DATASETS["snow_cover_mean_pct"]["scale_m"],
        ee,
    )
    surface_water = _reduce_value(
        ee.Image(DATASETS["surface_water_occurrence_pct"]["dataset_id"]),
        "occurrence",
        region,
        DATASETS["surface_water_occurrence_pct"]["scale_m"],
        ee,
    )
    glacier_count = (
        ee.FeatureCollection(DATASETS["nearby_glacier_outline_count"]["dataset_id"])
        .filterBounds(region)
        .size()
        .getInfo()
    )
    values = {
        "elevation_m": elevation,
        "temperature_mean_c": None if temperature is None else temperature - 273.15,
        "precipitation_total_mm": precipitation,
        "snow_cover_mean_pct": snow,
        "surface_water_occurrence_pct": surface_water,
        "nearby_glacier_outline_count": glacier_count,
    }
    return {
        "observed_start": f"{start}T00:00:00+00:00",
        "observed_end": f"{end}T00:00:00+00:00",
        "values": values,
        "buffer_m": 5000,
    }


def store_features(database, asset, result, fetched_at=None):
    fetched_at = fetched_at or datetime.now(timezone.utc).replace(microsecond=0)
    for metric, value in result["values"].items():
        definition = DATASETS[metric]
        database.execute(
            """INSERT INTO gee_features
               (asset_id, dataset_id, metric, observed_start, observed_end, value,
                units, scale_m, reducer, source_url, data_classification,
                fetched_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(asset_id, dataset_id, metric, observed_start, observed_end)
               DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at,
                             metadata_json = excluded.metadata_json""",
            (
                asset["id"], definition["dataset_id"], metric,
                result["observed_start"], result["observed_end"], value,
                definition["units"], definition["scale_m"], definition["reducer"],
                definition["source_url"], definition["classification"],
                fetched_at.isoformat(),
                json.dumps({"buffer_m": result["buffer_m"], "asset_coordinates": [
                    asset["longitude"], asset["latitude"]
                ]}),
            ),
        )
    database.commit()


@bp.get("/api/earth-engine/status")
def earth_engine_status_api():
    return jsonify(gee_status())


@bp.get("/api/assets/<slug>/earth-engine")
def earth_engine_asset_api(slug):
    database = get_db()
    asset = database.execute("SELECT id, slug, name FROM assets WHERE slug = ?", (slug,)).fetchone()
    if not asset:
        return jsonify({"error": {"message": "asset not found", "status": 404}}), 404
    rows = []
    for row in database.execute(
        """SELECT dataset_id, metric, observed_start, observed_end, value, units,
                  scale_m, reducer, source_url, data_classification, fetched_at,
                  metadata_json
           FROM gee_features WHERE asset_id = ? ORDER BY observed_end DESC, metric""",
        (asset["id"],),
    ):
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows), "asset": dict(asset)})


@click.command("refresh-gee-features")
@click.option("--asset", "asset_slug", help="Refresh one asset slug; defaults to all assets.")
@click.option("--days", default=14, show_default=True, type=click.IntRange(1, 90))
@with_appcontext
def refresh_gee_features_command(asset_slug, days):
    """Collect bounded Earth Engine covariates for monitored assets."""
    database = get_db()
    if asset_slug:
        assets = database.execute("SELECT * FROM assets WHERE slug = ?", (asset_slug,)).fetchall()
        if not assets:
            raise click.ClickException(f"Unknown asset: {asset_slug}")
    else:
        assets = database.execute("SELECT * FROM assets ORDER BY slug").fetchall()
    try:
        initialize_earth_engine()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    for asset in assets:
        result = collect_features(asset, days=days)
        store_features(database, asset, result)
        click.echo(f"{asset['slug']}: stored {len(result['values'])} Earth Engine features")


def init_app(app):
    app.cli.add_command(refresh_gee_features_command)
