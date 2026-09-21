import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import Blueprint, current_app, jsonify, render_template, request
from flask.cli import with_appcontext

from .auth import error_response, require_access
from .db import get_db
from .services import parse_timestamp


bp = Blueprint("workbench", __name__)

WORKBENCH_COMMIT = "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124"
REPOSITORY_URL = "https://github.com/harineupane_microsoft/NepalFloodProject"
ELIGIBILITY_STATES = {
    "discovered",
    "downloaded",
    "structurally_valid",
    "rights_approved",
    "scientifically_reviewed",
    "eligible_for_retrospective",
    "eligible_as_of",
    "eligible_for_training",
    "eligible_for_evaluation",
    "rejected",
}
SOURCE_ROLES = {"observation", "forcing", "reference", "context"}
HAZARD_TYPES = {"flood", "lake_outburst", "glacier_movement", "cascade"}
API_CATALOG = [
    {
        "id": "river",
        "provider": "Nepal BIPAD",
        "endpoint": "https://bipadportal.gov.np/api/v1/river/",
        "priority": "P0",
        "role": "observation",
        "access": "bounded_sample",
        "capability": "Bounded river station/time-window discovery",
        "status": "metadata_sample_only",
        "integration": "offline receipt import",
        "limitation": (
            "A returned page does not establish archive completeness, latest-record status, "
            "units, datum, quality, or historical availability."
        ),
    },
    {
        "id": "sentinel2",
        "provider": "Copernicus Data Space STAC",
        "endpoint": "https://stac.dataspace.copernicus.eu/v1/search",
        "priority": "P0",
        "role": "observation",
        "access": "catalogue",
        "capability": "Sentinel-1/Sentinel-2 catalog metadata",
        "status": "implemented",
        "integration": "existing bounded imagery adapter",
        "limitation": "Catalog metadata is not pixel analysis or hazard detection.",
    },
    {
        "id": "sentinel1",
        "provider": "Copernicus Data Space STAC",
        "endpoint": "https://stac.dataspace.copernicus.eu/v1/search",
        "priority": "P0",
        "role": "observation",
        "access": "catalogue",
        "capability": "Sentinel-1 GRD catalog discovery",
        "status": "adapter_ready",
        "integration": "bounded metadata contract",
        "limitation": "GRD backscatter change is not displacement; SLC/InSAR processing is not implemented.",
    },
    {
        "id": "reported_situation",
        "provider": "Rasuwa flood bulletin",
        "endpoint": "https://nirajbhusal.github.io/rasuwa-flood-bulletin/api/dashboard.json",
        "priority": "P1",
        "role": "reference",
        "access": "public_read",
        "capability": "Post-event aggregate situation context",
        "status": "reference_only",
        "integration": "not fetched by default",
        "limitation": "Not live telemetry or model input; narrative/personal content is excluded.",
    },
    {
        "id": "overture_impacted",
        "provider": "Microsoft AI for Good / Overture",
        "endpoint": "https://opendata.aiforgood.ai/damage-assessment/results/nepal_flash_flood_2026_overture_buildings_damage.gpkg",
        "priority": "P0",
        "role": "reference",
        "access": "public_download",
        "capability": "Post-event building-impact reference export",
        "status": "local_receipt_available",
        "integration": "metadata and structural-inspection receipt import",
        "limitation": "Outcome-filtered flags are not a complete exposure inventory or confirmed structural damage.",
    },
    {
        "id": "ihme_extent",
        "provider": "Microsoft AI for Good / IHME",
        "endpoint": "https://opendata.aiforgood.ai/damage-assessment/results/nepal_flash_flood_2026_ihme_population_extent.tif",
        "priority": "P0",
        "role": "reference",
        "access": "public_download",
        "capability": "Post-event population-extent reference raster",
        "status": "local_receipt_available",
        "integration": "metadata and structural-inspection receipt import",
        "limitation": "Buffered extent is not impact-only coverage; units and aggregation semantics remain unverified.",
    },
    {
        "id": "foundry",
        "provider": "Microsoft Foundry",
        "endpoint": None,
        "priority": "P2",
        "role": "context",
        "access": "optional_credentials",
        "capability": "Optional bounded evidence explanation",
        "status": "disabled",
        "integration": "not integrated",
        "limitation": "AI explanations remain outside calculations and warning authorization.",
    },
    {
        "id": "planetscope",
        "provider": "PlanetScope",
        "endpoint": "https://www.planet.com/",
        "priority": "P0",
        "role": "observation",
        "access": "partner_request",
        "capability": "High-cadence multispectral surface and channel change",
        "status": "request_only",
        "integration": "not integrated",
        "limitation": "Requires licensed analytical scenes, masks, registration evidence, and delivery times.",
    },
    {
        "id": "worldview3",
        "provider": "WorldView-3 / Vantor",
        "endpoint": "https://vantor.com/",
        "priority": "P1",
        "role": "observation",
        "access": "partner_request",
        "capability": "Very-high-resolution expert interpretation",
        "status": "request_only",
        "integration": "not integrated",
        "limitation": "No licensed analytical product or redistribution permission is bundled.",
    },
    {
        "id": "unosat",
        "provider": "UNOSAT 4256",
        "endpoint": "https://unosat.org/products/4256",
        "priority": "P0",
        "role": "reference",
        "access": "partner_request",
        "capability": "Post-event source, runout, flood, erosion, deposition, and blockage interpretation",
        "status": "request_only",
        "integration": "reference contract only",
        "limitation": "Evaluator-only reference; not a pre-event feature or independent ground truth.",
    },
    {
        "id": "rain",
        "provider": "NASA IMERG / approved station rainfall",
        "endpoint": "https://gpm.nasa.gov/data/imerg",
        "priority": "P1",
        "role": "observation",
        "access": "partner_request",
        "capability": "Antecedent rainfall and model forcing",
        "status": "adapter_required",
        "integration": "provider interface documented",
        "limitation": "Run type and latency must be retained; IMERG Early is not rapid local telemetry.",
    },
    {
        "id": "weather",
        "provider": "ECMWF Open Data",
        "endpoint": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "priority": "P1",
        "role": "forcing",
        "access": "partner_request",
        "capability": "Issued forecast precipitation, temperature, and ensembles",
        "status": "adapter_required",
        "integration": "provider interface documented",
        "limitation": "Issue, valid, and publication times are mandatory; ERA5 cannot substitute for an issued forecast.",
    },
    {
        "id": "terrain",
        "provider": "Copernicus DEM / HydroBASINS / HydroRIVERS",
        "endpoint": "https://dataspace.copernicus.eu/",
        "priority": "P0",
        "role": "context",
        "access": "partner_request",
        "capability": "Terrain and physically connected catchment/reach context",
        "status": "adapter_required",
        "integration": "metadata contract only",
        "limitation": "Regional terrain is not bathymetry, a validated catchment, or flood-arrival evidence.",
    },
    {
        "id": "exposure",
        "provider": "Overture Maps and licensed population products",
        "endpoint": "https://overturemaps.org/",
        "priority": "P0",
        "role": "context",
        "access": "partner_request",
        "capability": "Complete dated building, road, bridge, facility, and population inventory",
        "status": "request_only",
        "integration": "contract only",
        "limitation": "Exposure is not damage, occupancy, vulnerability, route passability, or casualties.",
    },
    {
        "id": "scenario",
        "provider": "ICIMOD and authorized scientific partners",
        "endpoint": "https://www.icimod.org/",
        "priority": "P0",
        "role": "reference",
        "access": "partner_request",
        "capability": "Reviewed chronology and conditional cascade scenarios",
        "status": "request_only",
        "integration": "scenario interface documented",
        "limitation": "Scenarios remain conditional and cannot establish onset prediction.",
    },
    {
        "id": "movement",
        "provider": "ICIMOD and local instrument partners",
        "endpoint": "https://www.icimod.org/",
        "priority": "P0",
        "role": "observation",
        "access": "partner_request",
        "capability": "GNSS, camera, lake-level, and stable-referenced displacement observations",
        "status": "request_only",
        "integration": "normalized evidence envelope available",
        "limitation": "Glacier segmentation and velocity cannot be relabeled as slope displacement or failure timing.",
    },
    {
        "id": "hkh_glaciers",
        "provider": "Microsoft / ICIMOD HKH Glacier Mapping",
        "endpoint": "https://lila.science/datasets/hkh-glacier-mapping",
        "priority": "P0",
        "role": "reference",
        "access": "partner_request",
        "capability": "Historical clean-ice and debris-covered glacier segmentation benchmark",
        "status": "request_only",
        "integration": "inventory adapter boundary",
        "limitation": "Historical masks are not current truth, movement measurements, or collapse labels.",
    },
    {
        "id": "hkh_lakes",
        "provider": "Microsoft Research / ICIMOD lake mapping",
        "endpoint": "https://www.microsoft.com/en-us/research/publication/mapping-glacial-lakes-using-historically-guided-segmentation-models/",
        "priority": "P0",
        "role": "reference",
        "access": "partner_request",
        "capability": "Historical lake outlines and guided-segmentation benchmark",
        "status": "request_only",
        "integration": "outline-comparison boundary",
        "limitation": "Historical guidance must not become a hard search mask or imply lake depth, breach, or flood probability.",
    },
    {
        "id": "icimod_glof_inventory",
        "provider": "ICIMOD HMA GLOF inventory",
        "endpoint": "https://rds.icimod.org/metadata/8881454b-6f7c-461b-95c2-eaf7618230d9",
        "priority": "P1",
        "role": "reference",
        "access": "catalogue",
        "capability": "Regional glacial-lake-outburst event inventory",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Archive contents, event labels, spatial scope, and current version require validation before use.",
    },
    {
        "id": "rgi7",
        "provider": "NSIDC Randolph Glacier Inventory v7",
        "endpoint": "https://nsidc.org/data/nsidc-0770/versions/7",
        "priority": "P1",
        "role": "reference",
        "access": "public_or_registered",
        "capability": "Versioned glacier outlines and identifiers",
        "status": "adapter_boundary",
        "integration": "existing GeoJSON inventory importer",
        "limitation": "Inventory outlines are not glacier-by-glacier area-change rates or current hazard observations.",
    },
    {
        "id": "hydrosheds",
        "provider": "HydroSHEDS / HydroBASINS / HydroRIVERS",
        "endpoint": "https://www.hydrosheds.org/products/hydrosheds",
        "priority": "P1",
        "role": "context",
        "access": "public_download",
        "capability": "Versioned terrain-derived catchments and river networks",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Topology and resolution require local validation; products are not hydraulic models.",
    },
    {
        "id": "worldpop",
        "provider": "WorldPop",
        "endpoint": "https://www.worldpop.org/",
        "priority": "P1",
        "role": "context",
        "access": "public_catalogue",
        "capability": "Population-distribution research products",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "A dated licensed product and explicit count/density semantics are required; not live occupancy.",
    },
    {
        "id": "openstreetmap",
        "provider": "OpenStreetMap / Geofabrik",
        "endpoint": "https://download.geofabrik.de/asia/nepal.html",
        "priority": "P1",
        "role": "context",
        "access": "public_download",
        "capability": "Road, settlement, and facility context",
        "status": "adapter_required",
        "integration": "Leaflet basemap only by default",
        "limitation": "ODbL attribution and snapshot completeness apply; mapped roads are not proof of passability.",
    },
    {
        "id": "era5",
        "provider": "Copernicus ERA5",
        "endpoint": "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels",
        "priority": "P2",
        "role": "context",
        "access": "registered_download",
        "capability": "Hourly atmospheric reanalysis",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Reanalysis is retrospective context and cannot substitute for an issued forecast in as-of evaluation.",
    },
    {
        "id": "glofas",
        "provider": "Copernicus GloFAS",
        "endpoint": "https://global-flood.emergency.copernicus.eu/",
        "priority": "P2",
        "role": "forcing",
        "access": "registered_or_service",
        "capability": "Global flood forecast and reanalysis context",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Operational and historical versions, issue times, local calibration, and archive access must be verified.",
    },
    {
        "id": "usgs_earthquakes",
        "provider": "USGS Earthquake Catalog API",
        "endpoint": "https://earthquake.usgs.gov/fdsnws/event/1/",
        "priority": "P2",
        "role": "context",
        "access": "public_api",
        "capability": "Regional seismic event chronology",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Catalog events are not continuous waveforms, avalanche detections, or proof of causal triggering.",
    },
    {
        "id": "nepal_seismic",
        "provider": "National Earthquake Monitoring and Research Center Nepal",
        "endpoint": "https://seismonepal.gov.np/",
        "priority": "P2",
        "role": "context",
        "access": "partner_request",
        "capability": "Local seismic event and waveform context",
        "status": "request_only",
        "integration": "source registry only",
        "limitation": "Waveform access, latency, station metadata, and permissions are unverified.",
    },
    {
        "id": "lhasa",
        "provider": "NASA LHASA",
        "endpoint": "https://github.com/nasa/LHASA",
        "priority": "P2",
        "role": "reference",
        "access": "public_code_and_products",
        "capability": "Rainfall-landslide situational-awareness comparator",
        "status": "adapter_required",
        "integration": "source registry only",
        "limitation": "Native-scale probabilities require independent Nepal calibration and are not local ground truth.",
    },
    {
        "id": "hma_landslides",
        "provider": "NSIDC HMA Multitemporal Landslide Inventories",
        "endpoint": "https://nsidc.org/data/hma_mtli/versions/1",
        "priority": "P2",
        "role": "reference",
        "access": "earthdata_login",
        "capability": "Annual initiation-point inventories for selected Himalayan corridors",
        "status": "request_only",
        "integration": "source registry only",
        "limitation": "Annual labels cannot establish hourly failure time and may not cover the pilot AOI.",
    },
    {
        "id": "hma_landslide_indicator",
        "provider": "NSIDC HMA Daily Landslide Hazard Indicator",
        "endpoint": "https://nsidc.org/data/hma2_lhi/versions/1",
        "priority": "P2",
        "role": "reference",
        "access": "earthdata_login",
        "capability": "Historical and climate-scenario landslide indicators",
        "status": "request_only",
        "integration": "source registry only",
        "limitation": "Climate-planning indicators are not issued short-term warnings or observed truth.",
    },
    {
        "id": "ramms",
        "provider": "RAMMS",
        "endpoint": "https://ramms.ch/",
        "priority": "P2",
        "role": "reference",
        "access": "licensed_tool",
        "capability": "Conditional avalanche, rockfall, debris-flow, and RockIce runout scenarios",
        "status": "not_integrated",
        "integration": "scenario contract only",
        "limitation": "Requires licensing, independent validation, local parameters, and scientific review; not initiation prediction.",
    },
    {
        "id": "r_avaflow",
        "provider": "r.avaflow",
        "endpoint": "https://gmd.copernicus.org/articles/10/553/2017/",
        "priority": "P2",
        "role": "reference",
        "access": "research_software",
        "capability": "Two-phase mass-flow process-chain scenarios",
        "status": "not_integrated",
        "integration": "scenario contract only",
        "limitation": "No solver version, parameter set, local validation, or operational runtime is configured.",
    },
    {
        "id": "cap12",
        "provider": "OASIS Common Alerting Protocol 1.2",
        "endpoint": "https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html",
        "priority": "P1",
        "role": "context",
        "access": "open_standard",
        "capability": "Interoperable alert-message profile",
        "status": "planned",
        "integration": "current notifications use internal JSON",
        "limitation": "A locally approved CAP profile, conformance tests, update/cancel behavior, and authority governance are still required.",
    },
]

for source_id, name, endpoint, version in (
    (
        "emsr927_syapru",
        "Copernicus EMSR927 Syapru Besi",
        "https://rapidmapping.emergency.copernicus.eu/backend/EMSR927/AOI01/GRA_PRODUCT/EMSR927_AOI01_GRA_PRODUCT_v1.zip",
        "v1",
    ),
    (
        "emsr927_timure",
        "Copernicus EMSR927 Timure",
        "https://rapidmapping.emergency.copernicus.eu/backend/EMSR927/AOI02/GRA_PRODUCT/EMSR927_AOI02_GRA_PRODUCT_v2.zip",
        "v2",
    ),
    (
        "emsr927_bidur",
        "Copernicus EMSR927 Bidur Monitoring 1",
        "https://rapidmapping.emergency.copernicus.eu/backend/EMSR927/AOI03/GRA_MONIT01/EMSR927_AOI03_GRA_MONIT01_v1.zip",
        "v1",
    ),
):
    API_CATALOG.append({
        "id": source_id,
        "provider": name,
        "endpoint": endpoint,
        "priority": "P0",
        "role": "reference",
        "access": "public_download",
        "capability": f"Pinned post-event grading package {version}",
        "status": "local_receipt_available",
        "integration": "metadata and structural-inspection receipt import",
        "limitation": "Post-event assessment only; AOI, building grades, roads, and observed-event layers are not interchangeable.",
    })

SOURCE_INDEX = {item["id"]: item for item in API_CATALOG}


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso_or_none(value, field):
    if value is None:
        return None
    try:
        return parse_timestamp(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _canonical_hash(document):
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_sha256(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{field} must be a 64-character hexadecimal SHA-256")
    return value.lower()


def validate_receipt(payload):
    if not isinstance(payload, dict):
        raise ValueError("receipt must be a JSON object")
    required = (
        "receipt_id",
        "source_id",
        "source_item_id",
        "source_url",
        "source_role",
        "availability_basis",
        "locally_first_seen_at",
        "processed_at",
        "rights_status",
        "quality_status",
        "content_sha256",
        "eligibility_state",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing required receipt field(s): {', '.join(missing)}")
    for field in ("receipt_id", "source_id", "source_item_id"):
        if not isinstance(payload[field], str) or not 1 <= len(payload[field]) <= 200:
            raise ValueError(f"{field} must be a string between 1 and 200 characters")
    if payload["source_role"] not in SOURCE_ROLES:
        raise ValueError("source_role must be observation, forcing, reference, or context")
    if payload["availability_basis"] not in {
        "provider_published", "first_seen", "estimated", "unknown"
    }:
        raise ValueError(
            "availability_basis must be provider_published, first_seen, estimated, or unknown"
        )
    if payload["rights_status"] not in {"approved", "pending", "restricted", "unknown"}:
        raise ValueError("rights_status must be approved, pending, restricted, or unknown")
    if payload["quality_status"] not in {
        "validated", "provisional", "unverified", "rejected"
    }:
        raise ValueError(
            "quality_status must be validated, provisional, unverified, or rejected"
        )
    if payload["eligibility_state"] not in ELIGIBILITY_STATES:
        raise ValueError("invalid eligibility_state")
    source_url = payload["source_url"]
    if (
        not isinstance(source_url, str)
        or not source_url.startswith("https://")
        or "?" in source_url
        or "@" in source_url.split("://", 1)[1].split("/", 1)[0]
    ):
        raise ValueError(
            "source_url must be stable HTTPS without query parameters or embedded credentials"
        )
    sha256 = _require_sha256(payload["content_sha256"], "content_sha256")
    observed_start = _iso_or_none(payload.get("observed_start"), "observed_start")
    observed_end = _iso_or_none(payload.get("observed_end"), "observed_end")
    source_available_at = _iso_or_none(
        payload.get("source_available_at"), "source_available_at"
    )
    first_seen = _iso_or_none(payload["locally_first_seen_at"], "locally_first_seen_at")
    processed_at = _iso_or_none(payload["processed_at"], "processed_at")
    if observed_start and observed_end and observed_end < observed_start:
        raise ValueError("observed_end cannot be earlier than observed_start")
    if source_available_at and observed_end and source_available_at < observed_end:
        raise ValueError("source_available_at cannot precede observed_end")
    if payload["availability_basis"] == "unknown" and source_available_at is not None:
        raise ValueError("unknown availability_basis requires null source_available_at")
    if (
        payload["eligibility_state"].startswith("eligible_")
        and payload["rights_status"] != "approved"
    ):
        raise ValueError("eligible receipts require approved rights")
    if (
        payload["eligibility_state"] == "eligible_as_of"
        and (
            payload["source_role"] not in {"observation", "forcing"}
            or payload["quality_status"] != "validated"
            or payload["availability_basis"] not in {"provider_published", "first_seen"}
            or source_available_at is None
        )
    ):
        raise ValueError(
            "eligible_as_of requires validated observation/forcing evidence with proven availability"
        )
    limitations = payload.get("limitations", [])
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) or len(item) > 500 for item in limitations
    ):
        raise ValueError("limitations must be an array of strings")
    byte_count = payload.get("byte_count")
    if byte_count is not None and (
        isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0
    ):
        raise ValueError("byte_count must be a non-negative integer or null")
    normalized = dict(payload)
    normalized.update({
        "observed_start": observed_start,
        "observed_end": observed_end,
        "source_available_at": source_available_at,
        "locally_first_seen_at": first_seen,
        "processed_at": processed_at,
        "limitations": limitations,
        "byte_count": byte_count,
        "content_sha256": sha256,
    })
    return normalized


def store_receipt(database, payload, commit=True):
    receipt = validate_receipt(payload)
    try:
        cursor = database.execute(
            """INSERT INTO acquisition_receipts
               (receipt_id, source_id, source_item_id, source_url, source_role,
                observed_start, observed_end, source_available_at, availability_basis,
                locally_first_seen_at, processed_at, rights_status, rights_ref,
                quality_status, quality_review_ref, crs, units, resolution,
                content_sha256, byte_count, algorithm_id, algorithm_version, code_commit,
                limitations_json, eligibility_state, manifest_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                receipt["receipt_id"],
                receipt["source_id"],
                receipt["source_item_id"],
                receipt["source_url"],
                receipt["source_role"],
                receipt["observed_start"],
                receipt["observed_end"],
                receipt["source_available_at"],
                receipt["availability_basis"],
                receipt["locally_first_seen_at"],
                receipt["processed_at"],
                receipt["rights_status"],
                receipt.get("rights_ref"),
                receipt["quality_status"],
                receipt.get("quality_review_ref"),
                receipt.get("crs"),
                receipt.get("units"),
                receipt.get("resolution"),
                receipt["content_sha256"].lower(),
                receipt["byte_count"],
                receipt.get("algorithm_id"),
                receipt.get("algorithm_version"),
                receipt.get("code_commit"),
                json.dumps(receipt["limitations"], sort_keys=True),
                receipt["eligibility_state"],
                json.dumps(receipt, sort_keys=True),
                utcnow().isoformat(),
            ),
        )
        if commit:
            database.commit()
    except sqlite3.IntegrityError:
        raise ValueError("receipt_id already exists") from None
    return cursor.lastrowid


def serialize_receipt(row):
    item = dict(row)
    item["limitations"] = json.loads(item.pop("limitations_json"))
    item["manifest"] = json.loads(item.pop("manifest_json"))
    return item


def acquisition_specification():
    return {
        "schema_version": "himalayawatch.acquisition-specification.v1",
        "case_id": "rasuwa-kyirong-2026",
        "pre_event_window": [
            "2026-07-01T00:00:00+00:00",
            "2026-08-25T23:59:59+00:00",
        ],
        "follow_up_window": [
            "2026-08-26T00:00:00+00:00",
            "2026-09-07T23:59:59+00:00",
        ],
        "reported_rise_nepal": "2026-08-26T09:00:00+05:45",
        "chronology_note": (
            "Approximate reported rapid rise, not a precisely established initiation time."
        ),
        "aoi_status": (
            "A scientist must verify the source area and connected cross-border corridor; "
            "a rectangular search box is not a catchment."
        ),
        "public_delivery_enabled": False,
        "sources": API_CATALOG,
    }


def evaluate_eligibility(payload):
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    required = ("evidence", "cutoff", "purpose", "max_age_hours")
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    evidence = payload["evidence"]
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a JSON object")
    source_id = evidence.get("source_id")
    if source_id not in SOURCE_INDEX:
        raise ValueError("unknown acquisition source")
    if payload["purpose"] not in {"retrospective", "evaluation", "as_of_input"}:
        raise ValueError("purpose must be retrospective, evaluation, or as_of_input")
    max_age = payload["max_age_hours"]
    if (
        isinstance(max_age, bool)
        or not isinstance(max_age, (int, float))
        or not 0 < max_age <= 17520
    ):
        raise ValueError("max_age_hours must be a positive number no greater than 17520")
    cutoff = parse_timestamp(payload["cutoff"])
    observed_end = (
        parse_timestamp(evidence["observed_end"])
        if evidence.get("observed_end")
        else None
    )
    available_at = (
        parse_timestamp(evidence["available_at"])
        if evidence.get("available_at")
        else None
    )
    availability_basis = evidence.get("availability_basis", "unknown")
    quality = evidence.get("quality", "pending")
    reasons = []
    if evidence.get("rights_confirmed") is not True:
        reasons.append("rights_not_confirmed")
    if quality != "accepted":
        reasons.append("quality_not_accepted")
    source = SOURCE_INDEX[source_id]
    if payload["purpose"] == "as_of_input":
        if source["role"] not in {"observation", "forcing"}:
            reasons.append("reference_or_context_only")
        if observed_end is None:
            reasons.append("unknown_observation_time")
        elif observed_end > cutoff:
            reasons.append("future_observation")
        elif (cutoff - observed_end).total_seconds() > float(max_age) * 3600:
            reasons.append("stale_observation")
        if availability_basis in {"unknown", "estimated"} or available_at is None:
            reasons.append("unproven_availability")
        elif available_at > cutoff:
            reasons.append("not_yet_available")
        if source["role"] == "forcing":
            reasons.append("forecast_issue_valid_contract_required")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "purpose": payload["purpose"],
        "source_id": source_id,
        "source_role": source["role"],
        "forecast_skill_established": False,
        "note": (
            "Metadata eligibility does not verify declarations, calibrate a model, "
            "or authorize warnings. First-seen time is conservative."
        ),
    }


def _eligible_observations(series, issue_time, lookback_hours, mode):
    issue = parse_timestamp(issue_time)
    cutoff = issue.timestamp() - lookback_hours * 3600
    eligible = []
    exclusions = []
    for observation in series:
        observed = parse_timestamp(observation["observed_at"])
        quality = observation.get("quality_status", "unverified")
        available_raw = observation.get("available_at")
        basis = observation.get("availability_basis", "unknown")
        reason = None
        if observed > issue:
            reason = "future_measurement"
        elif observed.timestamp() < cutoff:
            reason = "outside_lookback"
        elif quality not in {"validated", "provisional"}:
            reason = "invalid_quality"
        elif mode != "retrospective":
            if not available_raw or basis not in {"provider_published", "first_seen"}:
                reason = "availability_unproven"
            elif parse_timestamp(available_raw) > issue:
                reason = "available_after_issue"
        if reason:
            exclusions.append({"observed_at": observation["observed_at"], "reason": reason})
        else:
            eligible.append({
                "observed_at": observed,
                "value": float(observation["value"]),
            })
    eligible.sort(key=lambda item: item["observed_at"])
    return eligible, exclusions


def calculate_baseline(payload):
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    required = ("station_id", "reference_datum", "units", "issue_time", "horizon_hours",
                "lookback_hours", "max_gap_hours", "max_age_hours", "max_rate_m_per_hour",
                "mode", "observations")
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    if payload["units"] != "m":
        raise ValueError("river baseline units must be metres")
    if payload["mode"] not in {"shadow", "as_of_replay", "retrospective"}:
        raise ValueError("mode must be shadow, as_of_replay, or retrospective")
    observations = payload["observations"]
    if not isinstance(observations, list) or not 1 <= len(observations) <= 2000:
        raise ValueError("observations must contain 1-2000 items")
    for field in (
        "horizon_hours", "lookback_hours", "max_gap_hours", "max_age_hours",
        "max_rate_m_per_hour",
    ):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{field} must be a positive number")
    eligible, exclusions = _eligible_observations(
        observations,
        payload["issue_time"],
        float(payload["lookback_hours"]),
        payload["mode"],
    )
    reasons = []
    if len(eligible) < 4:
        reasons.append("at least four eligible observations are required")
    issue = parse_timestamp(payload["issue_time"])
    if eligible:
        age = (issue - eligible[-1]["observed_at"]).total_seconds() / 3600
        if age > float(payload["max_age_hours"]):
            reasons.append("latest eligible observation is too old")
        gaps = [
            (right["observed_at"] - left["observed_at"]).total_seconds() / 3600
            for left, right in zip(eligible, eligible[1:])
        ]
        if gaps and max(gaps) > float(payload["max_gap_hours"]):
            reasons.append("eligible observation window contains an excessive gap")
    if reasons:
        return {
            "status": "withheld",
            "withholding_reasons": reasons,
            "eligible_count": len(eligible),
            "exclusions": exclusions,
            "input_hash": _canonical_hash(payload),
        }
    latest = eligible[-1]
    xs = [
        (item["observed_at"] - eligible[0]["observed_at"]).total_seconds() / 3600
        for item in eligible
    ]
    ys = [item["value"] for item in eligible]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
        if denominator
        else 0.0
    )
    cap = float(payload["max_rate_m_per_hour"])
    slope = max(-cap, min(cap, slope))
    observation_age = (issue - latest["observed_at"]).total_seconds() / 3600
    horizon = float(payload["horizon_hours"])
    trend_value = latest["value"] + slope * (observation_age + horizon)
    return {
        "status": "candidate",
        "research_only": True,
        "station_id": payload["station_id"],
        "reference_datum": payload["reference_datum"],
        "units": "m",
        "issue_time": issue.isoformat(),
        "horizon_hours": horizon,
        "eligible_count": len(eligible),
        "excluded_count": len(exclusions),
        "exclusions": exclusions,
        "forecasts": {
            "persistence_m": latest["value"],
            "capped_linear_trend_m": round(trend_value, 6),
            "capped_rate_m_per_hour": round(slope, 6),
        },
        "uncertainty": None,
        "limitations": [
            "No calibrated prediction interval is produced.",
            "This is a research baseline, not a warning threshold or operational forecast.",
        ],
        "input_hash": _canonical_hash(payload),
    }


@bp.get("/api/integrations/catalog")
def integration_catalog():
    return jsonify({
        "source_repository": REPOSITORY_URL,
        "reviewed_commit": WORKBENCH_COMMIT,
        "repository_license": "not_declared",
        "code_copied": False,
        "data_reuse_requires_source_terms": True,
        "data": API_CATALOG,
        "count": len(API_CATALOG),
    })


@bp.get("/workbench")
def evidence_workbench_page():
    return render_template("workbench.html")


@bp.get("/api/workbench/summary")
def workbench_summary():
    database = get_db()
    latest_import = database.execute(
        """SELECT * FROM workbench_import_batches
           ORDER BY imported_at DESC LIMIT 1"""
    ).fetchone()
    archive_counts = {
        row["record_type"]: row["count"]
        for row in database.execute(
            """SELECT record_type, COUNT(*) AS count
               FROM research_archive_records GROUP BY record_type"""
        )
    }
    source_counts = {
        row["source_database"]: row["count"]
        for row in database.execute(
            """SELECT source_database, COUNT(*) AS count
               FROM research_archive_records GROUP BY source_database"""
        )
    }
    latest_runs = [
        dict(row)
        for row in database.execute(
            """SELECT id, archive_record_id, source_database, record_type, title,
                      issue_time, input_sha256, content_sha256, origin, research_only,
                      created_at
               FROM research_archive_records
               ORDER BY created_at DESC LIMIT 20"""
        )
    ]
    for item in latest_runs:
        item["research_only"] = bool(item["research_only"])
    external_dataset_count = database.execute(
        "SELECT COUNT(*) FROM external_datasets"
    ).fetchone()[0]
    external_record_count = database.execute(
        "SELECT COUNT(*) FROM external_dataset_records"
    ).fetchone()[0]
    gee_feature_count = database.execute(
        "SELECT COUNT(*) FROM gee_features"
    ).fetchone()[0]
    import_summary = None
    if latest_import:
        import_summary = dict(latest_import)
        import_summary["summary"] = json.loads(import_summary.pop("summary_json"))
    return jsonify({
        "version": "integrated-0.4",
        "operational": False,
        "public_delivery_enabled": False,
        "tabs": [
            "overview",
            "evidence",
            "sources",
            "acquisition",
            "observations",
            "prediction",
            "synthetic",
        ],
        "latest_import": import_summary,
        "archive_counts": archive_counts,
        "source_counts": source_counts,
        "latest_runs": latest_runs,
        "external_dataset_count": external_dataset_count,
        "external_record_count": external_record_count,
        "gee_feature_count": gee_feature_count,
        "gee_configured": bool(current_app.config.get("GEE_PROJECT")),
        "synthetic_scenarios": [
            {"id": "normal", "label": "Seasonal non-event control"},
            {"id": "rainfall", "label": "River rise without movement"},
            {"id": "movement", "label": "Movement without downstream flood"},
            {"id": "lake_breach", "label": "Prescribed lake release"},
            {"id": "compound", "label": "Movement, blockage, and prescribed breach"},
            {"id": "missing", "label": "Missing river measurements"},
            {"id": "delayed", "label": "Delayed river measurements"},
            {"id": "contradictory", "label": "Contradictory cascade assumptions"},
            {"id": "out_of_domain", "label": "River rise outside exercise domain"},
            {"id": "false_positive", "label": "Suspect sensor spike"},
        ],
        "limitations": [
            "Imported runs remain research-only and do not establish predictive skill.",
            "The synthetic lab represents fictional objects and prescribed assumptions.",
            "No public warnings are authorized from this workbench.",
            "Raw licensed acquisition files remain outside the Flask application.",
        ],
    })


@bp.get("/api/acquisition/specification")
def get_acquisition_specification():
    return jsonify(acquisition_specification())


@bp.post("/api/acquisition/eligibility")
def acquisition_eligibility():
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    try:
        result = evaluate_eligibility(request.get_json(silent=True))
    except (KeyError, TypeError, ValueError) as exc:
        return error_response(str(exc))
    return jsonify({"data": result})


@bp.get("/api/acquisition-receipts")
def list_receipts():
    rows = [
        serialize_receipt(row)
        for row in get_db().execute(
            "SELECT * FROM acquisition_receipts ORDER BY processed_at DESC LIMIT 200"
        )
    ]
    return jsonify({"data": rows, "count": len(rows)})


@bp.post("/api/acquisition-receipts")
def create_receipt():
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True)
    try:
        receipt_id = store_receipt(get_db(), payload)
    except ValueError as exc:
        return error_response(str(exc), 409 if "already exists" in str(exc) else 400)
    row = get_db().execute(
        "SELECT * FROM acquisition_receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    return jsonify({"data": serialize_receipt(row)}), 201


@bp.post("/api/river-baselines")
def river_baseline():
    auth_error = require_access("scientist")
    if auth_error:
        return auth_error
    try:
        result = calculate_baseline(request.get_json(silent=True))
    except (KeyError, TypeError, ValueError) as exc:
        return error_response(str(exc))
    return jsonify({"data": result}), 200


@bp.get("/api/observation-comparisons")
def list_observation_comparisons():
    rows = []
    for row in get_db().execute(
        "SELECT * FROM observation_comparisons ORDER BY created_at DESC LIMIT 100"
    ):
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item["limitations"] = json.loads(item.pop("limitations_json"))
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.get("/api/integrations/imports")
def list_import_batches():
    rows = []
    for row in get_db().execute(
        "SELECT * FROM workbench_import_batches ORDER BY imported_at DESC LIMIT 50"
    ):
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json"))
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.get("/api/integrations/research-archive")
def list_research_archive():
    limit_raw = request.args.get("limit", "100")
    try:
        limit = int(limit_raw)
    except ValueError:
        return error_response("limit must be an integer")
    if not 1 <= limit <= 200:
        return error_response("limit must be between 1 and 200")
    rows = []
    for row in get_db().execute(
        """SELECT * FROM research_archive_records
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ):
        item = dict(row)
        item["research_only"] = bool(item["research_only"])
        item["result_summary"] = json.loads(item.pop("result_summary_json"))
        rows.append(item)
    return jsonify({"data": rows, "count": len(rows)})


@bp.get("/api/integrations/research-archive/<int:record_id>")
def get_research_archive_record(record_id):
    row = get_db().execute(
        "SELECT * FROM research_archive_records WHERE id = ?", (record_id,)
    ).fetchone()
    if not row:
        return error_response("research archive record not found", 404)
    item = dict(row)
    item["research_only"] = bool(item["research_only"])
    item["result"] = json.loads(item.pop("result_summary_json"))
    return jsonify({"data": item})


def _read_bounded_json(path, max_bytes=2 * 1024 * 1024):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"required backup artifact is missing: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"backup artifact exceeds the {max_bytes}-byte limit: {path.name}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"backup artifact is not valid JSON: {path.name}") from exc
    return document


def _verify_backup_inventory(root, backup_manifest):
    files = backup_manifest.get("Files", [])
    if not isinstance(files, list) or len(files) > 5000:
        raise ValueError("backup file inventory must be an array of at most 5000 items")
    total_bytes = 0
    extensions = {}
    missing = []
    verified = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("backup file inventory contains a malformed entry")
        relative = item.get("Path")
        expected_bytes = item.get("Bytes")
        expected_hash = _require_sha256(item.get("SHA256"), "backup file SHA256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise ValueError("backup file inventory contains an unsafe entry")
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        if path.stat().st_size != expected_bytes:
            raise ValueError(f"backup file has a size mismatch: {relative}")
        with path.open("rb") as stream:
            actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"backup file hash mismatch: {relative}")
        verified += 1
        total_bytes += expected_bytes
        extension = path.suffix.lower() or "[no extension]"
        extensions[extension] = extensions.get(extension, 0) + 1
    return {
        "manifest_file_count": len(files),
        "manifest_total_bytes": total_bytes,
        "verified_file_count": verified,
        "missing_file_count": len(missing),
        "missing_files": missing,
        "file_types": dict(sorted(extensions.items())),
    }


def _store_receipt_idempotently(database, receipt):
    existing = database.execute(
        "SELECT content_sha256 FROM acquisition_receipts WHERE receipt_id = ?",
        (receipt["receipt_id"],),
    ).fetchone()
    if existing:
        expected = _require_sha256(receipt["content_sha256"], "content_sha256")
        if existing["content_sha256"] != expected:
            raise ValueError(f"receipt_id collision with different content: {receipt['receipt_id']}")
        return False
    store_receipt(database, receipt, commit=False)
    return True


def _native_receipt(record, source_commit):
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        raise ValueError("native acquisition record requires an id")
    recorded_at = _iso_or_none(record.get("recorded_at"), "recorded_at")
    if record.get("kind") == "catalogue":
        result = record.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("query"), dict):
            raise ValueError("native catalogue record is malformed")
        collection = result["query"].get("collection")
        source_id = "sentinel1" if collection == "sentinel-1-grd" else "sentinel2"
        return {
            "receipt_id": f"nfp:{record['id']}",
            "source_id": source_id,
            "source_item_id": f"catalogue:{record['id']}",
            "source_url": SOURCE_INDEX[source_id]["endpoint"],
            "source_role": SOURCE_INDEX[source_id]["role"],
            "observed_start": None,
            "observed_end": None,
            "source_available_at": None,
            "availability_basis": "unknown",
            "locally_first_seen_at": recorded_at,
            "processed_at": recorded_at,
            "rights_status": "unknown",
            "quality_status": "unverified",
            "content_sha256": _canonical_hash(record),
            "byte_count": None,
            "algorithm_id": "bounded-stac-catalogue",
            "algorithm_version": "0.2",
            "code_commit": source_commit,
            "limitations": [
                "Catalogue metadata only; pixels were not acquired.",
                "Historical availability and local pixel usability remain unverified.",
            ],
            "eligibility_state": "discovered",
            "native_record": record,
        }
    if record.get("kind") != "download":
        raise ValueError("native acquisition record kind must be catalogue or download")
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("native download record requires evidence")
    source_id = evidence.get("source_id")
    if source_id not in SOURCE_INDEX:
        raise ValueError(f"native acquisition source is not registered: {source_id}")
    basis = evidence.get("availability_basis", "unknown")
    basis = {
        "provider_recorded": "provider_published",
        "archive_documented": "provider_published",
    }.get(basis, basis)
    quality = {
        "accepted": "validated",
        "pending": "unverified",
        "rejected": "rejected",
    }.get(evidence.get("quality"), "unverified")
    rights = "approved" if evidence.get("rights_confirmed") is True else "pending"
    limitations = evidence.get("limitations")
    return {
        "receipt_id": f"nfp:{record['id']}",
        "source_id": source_id,
        "source_item_id": evidence.get("item_id") or record.get("filename") or record["id"],
        "source_url": evidence.get("source_url") or SOURCE_INDEX[source_id]["endpoint"],
        "source_role": SOURCE_INDEX[source_id]["role"],
        "observed_start": evidence.get("observed_start"),
        "observed_end": evidence.get("observed_end"),
        "source_available_at": evidence.get("available_at"),
        "availability_basis": basis,
        "locally_first_seen_at": recorded_at,
        "processed_at": recorded_at,
        "rights_status": rights,
        "rights_ref": evidence.get("rights_ref"),
        "quality_status": quality,
        "quality_review_ref": evidence.get("quality_ref"),
        "crs": evidence.get("crs"),
        "units": evidence.get("units"),
        "resolution": evidence.get("resolution"),
        "content_sha256": record.get("sha256"),
        "byte_count": record.get("bytes"),
        "algorithm_id": "native-acquisition-inspection",
        "algorithm_version": "0.2",
        "code_commit": source_commit,
        "limitations": [limitations] if limitations else [],
        "eligibility_state": (
            "scientifically_reviewed"
            if quality == "validated" and rights == "approved"
            else "downloaded"
        ),
        "native_record": record,
    }


def _import_native_acquisition(database, manifest, source_commit, acquisition_root):
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "0.2"
        or manifest.get("case_id") != "rasuwa-kyirong-2026"
        or not isinstance(manifest.get("records"), list)
        or len(manifest["records"]) > 50
    ):
        raise ValueError("unsupported or oversized native acquisition manifest")
    expected_hash = _require_sha256(
        manifest.get("records_sha256"), "native records_sha256"
    )
    if _canonical_hash(manifest["records"]) != expected_hash:
        raise ValueError("native acquisition manifest hash does not match its records")
    imported = skipped = verified_files = 0
    for record in manifest["records"]:
        if record.get("kind") == "download":
            filename = record.get("filename")
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename)
            ):
                raise ValueError("native download filename is invalid")
            asset = acquisition_root / filename
            if not asset.is_file():
                raise ValueError(f"native acquired asset is missing: {filename}")
            expected_size = record.get("bytes")
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or asset.stat().st_size != expected_size
            ):
                raise ValueError(f"native acquired asset size mismatch: {filename}")
            with asset.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != _require_sha256(record.get("sha256"), "native asset sha256"):
                raise ValueError(f"native acquired asset hash mismatch: {filename}")
            verified_files += 1
        if _store_receipt_idempotently(
            database, _native_receipt(record, source_commit)
        ):
            imported += 1
        else:
            skipped += 1
    return imported, skipped, expected_hash, verified_files


def _import_native_comparison(database, report):
    if not isinstance(report, dict) or report.get("kind") != "outline_comparison":
        raise ValueError("native observation report is not an outline comparison")
    manifest = report.get("input_manifest")
    if not isinstance(manifest, dict):
        raise ValueError("native observation report lacks its input manifest")
    input_hash = _require_sha256(report.get("input_sha256"), "observation input_sha256")
    if _canonical_hash(manifest) != input_hash:
        raise ValueError("native observation report input hash does not match its manifest")
    existing = database.execute(
        "SELECT input_hash FROM observation_comparisons WHERE comparison_id = ?",
        ("nfp:observation-report-2026-09-08",),
    ).fetchone()
    if existing:
        if existing["input_hash"] != input_hash:
            raise ValueError("native observation comparison ID has different content")
        return 0, 1
    before = manifest.get("before", {}).get("evidence", {})
    after = manifest.get("after", {}).get("evidence", {})
    objects = report.get("objects")
    limitations = report.get("limitations")
    coverage = report.get("coverage_fraction")
    if (
        not isinstance(objects, list)
        or not 1 <= len(objects) <= 30
        or not isinstance(limitations, list)
        or not isinstance(coverage, (int, float))
        or not 0 <= coverage <= 1
    ):
        raise ValueError("native observation report has invalid metrics or coverage")
    database.execute(
        """INSERT INTO observation_comparisons
           (comparison_id, source_system, source_run_id, input_hash,
            before_observed_start, before_observed_end, after_observed_start,
            after_observed_end, common_coverage_fraction, object_count,
            review_status, metrics_json, limitations_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "nfp:observation-report-2026-09-08",
            "NepalFloodProject local backup",
            None,
            input_hash,
            _iso_or_none(before.get("observed_start"), "before observed_start"),
            _iso_or_none(before.get("observed_end"), "before observed_end"),
            _iso_or_none(after.get("observed_start"), "after observed_start"),
            _iso_or_none(after.get("observed_end"), "after observed_end"),
            coverage,
            len(objects),
            "pending",
            json.dumps(objects, sort_keys=True),
            json.dumps(limitations, sort_keys=True),
            _iso_or_none(report.get("generated_at"), "comparison generated_at"),
        ),
    )
    return 1, 0


def _read_native_runs(path, max_records=100):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError(f"native research database is missing or oversized: {path.name}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
        if not table:
            raise ValueError(f"native research database lacks the runs table: {path.name}")
        rows = connection.execute(
            """SELECT id, created_at, input_hash, input_json, result_json
               FROM runs ORDER BY created_at"""
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > max_records:
        raise ValueError(f"native research database exceeds {max_records} records")
    return rows


def _archive_native_runs(database, import_id, source_database, rows):
    imported = skipped = synthetic = 0
    for row in rows:
        input_hash = _require_sha256(row["input_hash"], "native run input_hash")
        try:
            input_document = json.loads(row["input_json"])
            result = json.loads(row["result_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"native run contains invalid JSON: {row['id']}") from exc
        if not isinstance(input_document, dict) or not isinstance(result, dict):
            raise ValueError(f"native run documents must be objects: {row['id']}")
        if _canonical_hash(input_document) != input_hash:
            raise ValueError(f"native run input hash mismatch: {row['id']}")
        serialized_result = json.dumps(
            result, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(serialized_result.encode("utf-8")) > 1024 * 1024:
            raise ValueError(f"native run result exceeds 1 MiB: {row['id']}")
        record_id = f"nfp:{source_database}:{row['id']}"
        content_hash = hashlib.sha256(serialized_result.encode("utf-8")).hexdigest()
        existing = database.execute(
            """SELECT input_sha256, content_sha256 FROM research_archive_records
               WHERE archive_record_id = ?""",
            (record_id,),
        ).fetchone()
        if existing:
            if (
                existing["input_sha256"] != input_hash
                or existing["content_sha256"] != content_hash
            ):
                raise ValueError(f"native research record ID has different content: {record_id}")
            skipped += 1
            continue
        origin = "unknown"
        serialized_input = json.dumps(input_document, sort_keys=True)
        if '"synthetic"' in serialized_input or "synthetic_exercise" in serialized_input:
            origin = "synthetic"
            synthetic += 1
        database.execute(
            """INSERT INTO research_archive_records
               (archive_record_id, import_id, source_database, record_type, title,
                issue_time, input_sha256, content_sha256, origin, research_only,
                result_summary_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                record_id,
                import_id,
                source_database,
                result.get("mode", "unknown"),
                str(result.get("title", ""))[:200] or None,
                result.get("issue_time"),
                input_hash,
                content_hash,
                origin,
                serialized_result,
                _iso_or_none(row["created_at"], "native run created_at"),
            ),
        )
        imported += 1
    return imported, skipped, synthetic


def import_native_backup(database, root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError("backup path must be an existing directory")
    backup_manifest_path = root / "BACKUP-MANIFEST.json"
    backup_manifest = _read_bounded_json(backup_manifest_path)
    source_commit = backup_manifest.get("GitHead")
    if not isinstance(source_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", source_commit
    ):
        raise ValueError("backup manifest GitHead is invalid")
    backup_created_at = _iso_or_none(
        backup_manifest.get("CreatedUtc"), "backup CreatedUtc"
    )
    backup_hash = hashlib.sha256(backup_manifest_path.read_bytes()).hexdigest()
    inventory = _verify_backup_inventory(root, backup_manifest)
    import_id = f"nfp-backup:{backup_hash[:24]}"
    existing = database.execute(
        "SELECT summary_json FROM workbench_import_batches WHERE import_id = ?",
        (import_id,),
    ).fetchone()
    if existing:
        result = json.loads(existing["summary_json"])
        result["already_imported"] = True
        return result
    local = root / "prototype" / ".local"
    acquisition = _read_bounded_json(
        local / "acquisition-checks" / "rasuwa-acquisition-manifest.json"
    )
    observation = _read_bounded_json(
        local / "observation-checks" / "observation-report.json"
    )
    inspection_path = local / "acquisition-review-20260909" / "inspection.json"
    hdx_metadata_path = local / "acquisition-review-20260909" / "hdx-metadata.json"
    acquisition_review = (
        _read_bounded_json(inspection_path) if inspection_path.is_file() else None
    )
    hdx_metadata = (
        _read_bounded_json(hdx_metadata_path) if hdx_metadata_path.is_file() else None
    )
    workbench_rows = _read_native_runs(local / "workbench.sqlite3")
    prediction_rows = _read_native_runs(local / "predictions.sqlite3")
    synthetic_rows = _read_native_runs(local / "synthetic.sqlite3")
    database.execute("SAVEPOINT native_backup_import")
    try:
        (
            receipt_count,
            receipt_skipped,
            acquisition_hash,
            verified_files,
        ) = _import_native_acquisition(
            database, acquisition, source_commit, local / "acquisition"
        )
        comparison_count, comparison_skipped = _import_native_comparison(
            database, observation
        )
        summary = {
            "source_commit": source_commit,
            "backup_created_at": backup_created_at,
            "backup_inventory": inventory,
            "database_snapshot": backup_manifest.get("DatabaseSnapshot"),
            "restore_note": backup_manifest.get("Restore"),
            "acquisition_review": acquisition_review,
            "hdx_metadata": hdx_metadata,
            "acquisition_receipts": receipt_count,
            "acquisition_receipts_skipped": receipt_skipped,
            "binary_assets_hash_verified": verified_files,
            "observation_comparisons": comparison_count,
            "observation_comparisons_skipped": comparison_skipped,
            "research_records": 0,
            "research_records_skipped": 0,
            "synthetic_records": 0,
            "raw_binary_files_copied": False,
            "operational_claim": False,
        }
        database.execute(
            """INSERT INTO workbench_import_batches
               (import_id, source_repository, source_commit, backup_created_at,
                backup_manifest_sha256, acquisition_manifest_sha256,
                acquisition_receipt_count, observation_comparison_count,
                research_record_count, synthetic_record_count, summary_json, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '{}', ?)""",
            (
                import_id,
                REPOSITORY_URL,
                source_commit,
                backup_created_at,
                backup_hash,
                acquisition_hash,
                receipt_count,
                comparison_count,
                utcnow().isoformat(),
            ),
        )
        for source_database, rows in (
            ("workbench.sqlite3", workbench_rows),
            ("predictions.sqlite3", prediction_rows),
            ("synthetic.sqlite3", synthetic_rows),
        ):
            imported, skipped, synthetic = _archive_native_runs(
                database, import_id, source_database, rows
            )
            summary["research_records"] += imported
            summary["research_records_skipped"] += skipped
            summary["synthetic_records"] += synthetic
        database.execute(
            """UPDATE workbench_import_batches
               SET research_record_count = ?, synthetic_record_count = ?,
                   summary_json = ?
               WHERE import_id = ?""",
            (
                summary["research_records"],
                summary["synthetic_records"],
                json.dumps(summary, sort_keys=True),
                import_id,
            ),
        )
        database.execute("RELEASE SAVEPOINT native_backup_import")
        database.commit()
    except Exception:
        database.execute("ROLLBACK TO SAVEPOINT native_backup_import")
        database.execute("RELEASE SAVEPOINT native_backup_import")
        raise
    return summary


def import_bundle(database, document):
    if not isinstance(document, dict) or document.get("schema") != "himalayawatch.workbench-import.v1":
        raise ValueError("bundle schema must be himalayawatch.workbench-import.v1")
    if document.get("source_repository") != REPOSITORY_URL:
        raise ValueError("source_repository does not match the reviewed workbench")
    source_commit = document.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", source_commit
    ):
        raise ValueError("source_commit must be a 40-character hexadecimal Git commit")
    receipts = document.get("receipts", [])
    comparisons = document.get("observation_comparisons", [])
    model_runs = document.get("model_runs", [])
    if not all(isinstance(items, list) for items in (receipts, comparisons, model_runs)):
        raise ValueError("receipts, observation_comparisons, and model_runs must be arrays")
    if len(receipts) > 50 or len(comparisons) > 30 or len(model_runs) > 100:
        raise ValueError("bundle exceeds bounded import limits")
    summary = {"receipts": 0, "observation_comparisons": 0, "model_runs": 0}
    database.execute("SAVEPOINT workbench_import")
    try:
        for receipt in receipts:
            store_receipt(database, receipt, commit=False)
            summary["receipts"] += 1
        for comparison in comparisons:
            required = (
                "comparison_id", "input_hash", "before_observed_start",
                "before_observed_end", "after_observed_start", "after_observed_end",
                "object_count", "review_status", "metrics", "limitations",
            )
            missing = [field for field in required if field not in comparison]
            if missing:
                raise ValueError(
                    f"missing observation comparison field(s): {', '.join(missing)}"
                )
            if comparison["review_status"] not in {"pending", "approved", "rejected"}:
                raise ValueError(
                    "comparison review_status must be pending, approved, or rejected"
                )
            input_hash = _require_sha256(comparison["input_hash"], "comparison input_hash")
            object_count = comparison["object_count"]
            if (
                isinstance(object_count, bool)
                or not isinstance(object_count, int)
                or object_count < 1
                or object_count > 30
            ):
                raise ValueError("comparison object_count must be an integer from 1 to 30")
            coverage = comparison.get("common_coverage_fraction")
            if coverage is not None and (
                isinstance(coverage, bool)
                or not isinstance(coverage, (int, float))
                or not 0 <= coverage <= 1
            ):
                raise ValueError(
                    "common_coverage_fraction must be a number from 0 to 1 or null"
                )
            if not isinstance(comparison["metrics"], list) or len(
                comparison["metrics"]
            ) > 10000:
                raise ValueError("comparison metrics must be an array of at most 10000 items")
            if not isinstance(comparison["limitations"], list) or any(
                not isinstance(item, str) or len(item) > 500
                for item in comparison["limitations"]
            ):
                raise ValueError("comparison limitations must be an array of strings")
            before_start = _iso_or_none(
                comparison["before_observed_start"], "before_observed_start"
            )
            before_end = _iso_or_none(
                comparison["before_observed_end"], "before_observed_end"
            )
            after_start = _iso_or_none(
                comparison["after_observed_start"], "after_observed_start"
            )
            after_end = _iso_or_none(
                comparison["after_observed_end"], "after_observed_end"
            )
            if before_end < before_start or after_end < after_start:
                raise ValueError("comparison observation intervals must be ordered")
            if after_start < before_end:
                raise ValueError("after observation interval cannot precede the before interval")
            database.execute(
            """INSERT INTO observation_comparisons
               (comparison_id, source_system, source_run_id, input_hash,
                before_observed_start, before_observed_end, after_observed_start,
                after_observed_end, common_coverage_fraction, object_count,
                review_status, metrics_json, limitations_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                comparison["comparison_id"],
                "NepalFloodProject",
                comparison.get("source_run_id"),
                input_hash,
                before_start,
                before_end,
                after_start,
                after_end,
                coverage,
                object_count,
                comparison["review_status"],
                json.dumps(comparison["metrics"], sort_keys=True),
                json.dumps(comparison["limitations"], sort_keys=True),
                utcnow().isoformat(),
            ),
            )
            summary["observation_comparisons"] += 1
        for model in model_runs:
            required = (
                "run_id", "model_name", "model_version", "hazard_type", "catchment",
                "input_hash", "input_evidence_ids", "output", "uncertainty", "status",
                "created_at", "created_by",
            )
            missing = [field for field in required if field not in model]
            if missing:
                raise ValueError(f"missing model run field(s): {', '.join(missing)}")
            if model["hazard_type"] not in HAZARD_TYPES:
                raise ValueError(
                    "hazard_type must be flood, lake_outburst, glacier_movement, or cascade"
                )
            if model["status"] not in {"withheld", "candidate", "accepted", "rejected"}:
                raise ValueError("invalid model run status")
            input_hash = _require_sha256(model["input_hash"], "model input_hash")
            dataset_hash = model.get("dataset_sha256")
            protocol_hash = model.get("protocol_sha256")
            if dataset_hash is not None:
                dataset_hash = _require_sha256(dataset_hash, "dataset_sha256")
            if protocol_hash is not None:
                protocol_hash = _require_sha256(protocol_hash, "protocol_sha256")
            if not isinstance(model["input_evidence_ids"], list) or any(
                not isinstance(item, str) for item in model["input_evidence_ids"]
            ):
                raise ValueError("input_evidence_ids must be an array of strings")
            if not isinstance(model["output"], dict) or not isinstance(
                model["uncertainty"], dict
            ):
                raise ValueError("model output and uncertainty must be objects")
            created_at = _iso_or_none(model["created_at"], "model created_at")
            fit_available_at = _iso_or_none(
                model.get("fit_available_at"), "fit_available_at"
            )
            if fit_available_at and fit_available_at > created_at:
                raise ValueError("fit_available_at cannot be later than model created_at")
            feature_schema = model.get("feature_schema", [])
            training_domain = model.get("training_domain", {})
            if not isinstance(feature_schema, list) or any(
                not isinstance(item, str) for item in feature_schema
            ):
                raise ValueError("feature_schema must be an array of strings")
            if not isinstance(training_domain, dict):
                raise ValueError("training_domain must be an object")
            database.execute(
            """INSERT INTO model_runs
               (run_id, model_name, model_version, hazard_type, catchment, input_hash,
                input_evidence_json, output_json, uncertainty_json, status,
                withholding_reason, created_at, created_by, reviewed_at, reviewed_by,
                review_note, artifact_schema, dataset_sha256, protocol_sha256,
                feature_schema_json, station_id, reference_datum, fit_available_at,
                training_domain_json, research_only)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?)""",
            (
                model["run_id"], model["model_name"], model["model_version"],
                model["hazard_type"], model["catchment"], input_hash,
                json.dumps(model["input_evidence_ids"]), json.dumps(model["output"]),
                json.dumps(model["uncertainty"]), model["status"],
                model.get("withholding_reason"), created_at, model["created_by"],
                model.get("reviewed_at"), model.get("reviewed_by"), model.get("review_note"),
                model.get("artifact_schema"), dataset_hash, protocol_hash,
                json.dumps(feature_schema),
                model.get("station_id"), model.get("reference_datum"),
                fit_available_at,
                json.dumps(training_domain, sort_keys=True),
                1,
            ),
            )
            summary["model_runs"] += 1
        database.execute("RELEASE SAVEPOINT workbench_import")
        database.commit()
    except Exception:
        database.execute("ROLLBACK TO SAVEPOINT workbench_import")
        database.execute("RELEASE SAVEPOINT workbench_import")
        raise
    return summary


@click.command("import-workbench-export")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@with_appcontext
def import_workbench_export(path):
    """Import a bounded, reviewed NepalFloodProject export bundle."""
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    try:
        summary = import_bundle(get_db(), document)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise click.ClickException(f"workbench import failed: {exc}") from exc
    click.echo(
        "Imported workbench bundle: "
        f"{summary['receipts']} receipt(s), "
        f"{summary['observation_comparisons']} comparison(s), "
        f"{summary['model_runs']} model run(s)."
    )


@click.command("import-nepalflood-backup")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@with_appcontext
def import_nepalflood_backup(path):
    """Import metadata and research records from a local NepalFloodProject backup."""
    try:
        summary = import_native_backup(get_db(), path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(f"backup import failed: {exc}") from exc
    if summary.get("already_imported"):
        click.echo(
            "NepalFloodProject backup was already imported; verified batch contains "
            f"{summary['acquisition_receipts']} receipt(s), "
            f"{summary['observation_comparisons']} comparison(s), and "
            f"{summary['research_records']} research record(s)."
        )
        return
    click.echo(
        "Imported NepalFloodProject backup: "
        f"{summary['acquisition_receipts']} receipt(s), "
        f"{summary['observation_comparisons']} comparison(s), "
        f"{summary['research_records']} research record(s), "
        f"{summary['synthetic_records']} synthetic record(s)."
    )


def init_app(app):
    app.cli.add_command(import_workbench_export)
    app.cli.add_command(import_nepalflood_backup)
