import json
import sqlite3
from datetime import datetime, timedelta, timezone

import click
from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    country TEXT NOT NULL CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
    asset_type TEXT NOT NULL CHECK(asset_type IN ('glacier', 'lake', 'station')),
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    area_km2 REAL,
    elevation_m REAL,
    observation_time TEXT,
    trend_value REAL,
    trend_unit TEXT,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'moderate', 'high', 'critical', 'unknown')),
    operational_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(operational_status IN ('online', 'offline', 'unknown', 'not_applicable')),
    device_type TEXT,
    operator TEXT,
    measurement_kind TEXT,
    units TEXT,
    source_url TEXT NOT NULL,
    data_classification TEXT NOT NULL,
    notes TEXT,
    warning_threshold REAL,
    critical_threshold REAL,
    rapid_rise_threshold REAL,
    abnormal_movement_threshold REAL,
    stale_after_hours INTEGER NOT NULL DEFAULT 72
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    units TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    quality_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK(quality_status IN ('validated', 'provisional', 'unverified', 'rejected')),
    source_url TEXT,
    is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK(is_synthetic IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    temperature_c REAL,
    relative_humidity_pct REAL,
    precipitation_mm REAL,
    rain_mm REAL,
    snowfall_cm REAL,
    wind_speed_kmh REAL,
    grid_latitude REAL NOT NULL,
    grid_longitude REAL NOT NULL,
    grid_elevation_m REAL,
    provider TEXT NOT NULL,
    source_url TEXT NOT NULL,
    data_classification TEXT NOT NULL
        CHECK(data_classification IN ('modeled', 'measured', 'synthetic')),
    fetched_at TEXT NOT NULL,
    UNIQUE(asset_id, observed_at, provider)
);

CREATE TABLE IF NOT EXISTS external_datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT UNIQUE NOT NULL,
    provider TEXT NOT NULL,
    title TEXT NOT NULL,
    dataset_uuid TEXT,
    doi TEXT,
    metadata_url TEXT NOT NULL,
    download_url TEXT,
    license TEXT NOT NULL,
    file_path TEXT,
    file_sha256 TEXT,
    byte_count INTEGER CHECK(byte_count IS NULL OR byte_count >= 0),
    media_type TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('registered', 'downloaded', 'imported', 'failed', 'metadata_only')),
    record_count INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    imported_at TEXT,
    error_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_dataset_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES external_datasets(id) ON DELETE CASCADE,
    record_key TEXT NOT NULL,
    record_type TEXT NOT NULL,
    observed_at TEXT,
    latitude REAL CHECK(latitude IS NULL OR latitude BETWEEN -90 AND 90),
    longitude REAL CHECK(longitude IS NULL OR longitude BETWEEN -180 AND 180),
    properties_json TEXT NOT NULL,
    geometry_json TEXT,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, record_key)
);

CREATE TABLE IF NOT EXISTS gee_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    dataset_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    observed_start TEXT NOT NULL,
    observed_end TEXT NOT NULL,
    value REAL,
    units TEXT NOT NULL,
    scale_m REAL NOT NULL CHECK(scale_m > 0),
    reducer TEXT NOT NULL,
    source_url TEXT NOT NULL,
    data_classification TEXT NOT NULL DEFAULT 'satellite_or_reanalysis'
        CHECK(data_classification IN ('satellite_or_reanalysis', 'modeled', 'measured')),
    fetched_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(asset_id, dataset_id, metric, observed_start, observed_end)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('warning', 'critical')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    evidence_reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
    UNIQUE(asset_id, alert_type, reason, created_at)
);

CREATE TABLE IF NOT EXISTS source_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    provider TEXT NOT NULL,
    asset_slug TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('received', 'processed', 'rejected')),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS evidence_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT UNIQUE NOT NULL,
    source_event_id INTEGER NOT NULL REFERENCES source_events(id) ON DELETE CASCADE,
    reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    contract_version TEXT NOT NULL,
    metric TEXT NOT NULL,
    measurement_reference TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL DEFAULT '{}',
    provenance_url TEXT NOT NULL,
    is_synthetic INTEGER NOT NULL CHECK(is_synthetic IN (0, 1)),
    payload_sha256 TEXT NOT NULL,
    quality_checks_json TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    hazard_type TEXT NOT NULL
        CHECK(hazard_type IN ('flood', 'lake_outburst', 'glacier_movement', 'cascade')),
    catchment TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_evidence_json TEXT NOT NULL,
    output_json TEXT NOT NULL DEFAULT '{}',
    uncertainty_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('withheld', 'candidate', 'accepted', 'rejected')),
    withholding_reason TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT,
    artifact_schema TEXT,
    dataset_sha256 TEXT,
    protocol_sha256 TEXT,
    feature_schema_json TEXT NOT NULL DEFAULT '[]',
    station_id TEXT,
    reference_datum TEXT,
    fit_available_at TEXT,
    training_domain_json TEXT NOT NULL DEFAULT '{}',
    research_only INTEGER NOT NULL DEFAULT 1 CHECK(research_only IN (0, 1))
);

CREATE TABLE IF NOT EXISTS acquisition_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    source_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_role TEXT NOT NULL
        CHECK(source_role IN ('observation', 'forcing', 'reference', 'context')),
    observed_start TEXT,
    observed_end TEXT,
    source_available_at TEXT,
    availability_basis TEXT NOT NULL
        CHECK(availability_basis IN ('provider_published', 'first_seen', 'estimated', 'unknown')),
    locally_first_seen_at TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    rights_status TEXT NOT NULL
        CHECK(rights_status IN ('approved', 'pending', 'restricted', 'unknown')),
    rights_ref TEXT,
    quality_status TEXT NOT NULL
        CHECK(quality_status IN ('validated', 'provisional', 'unverified', 'rejected')),
    quality_review_ref TEXT,
    crs TEXT,
    units TEXT,
    resolution TEXT,
    content_sha256 TEXT NOT NULL,
    byte_count INTEGER CHECK(byte_count IS NULL OR byte_count >= 0),
    algorithm_id TEXT,
    algorithm_version TEXT,
    code_commit TEXT,
    limitations_json TEXT NOT NULL DEFAULT '[]',
    eligibility_state TEXT NOT NULL
        CHECK(eligibility_state IN (
            'discovered', 'downloaded', 'structurally_valid', 'rights_approved',
            'scientifically_reviewed', 'eligible_for_retrospective', 'eligible_as_of',
            'eligible_for_training', 'eligible_for_evaluation', 'rejected'
        )),
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id TEXT UNIQUE NOT NULL,
    source_system TEXT NOT NULL,
    source_run_id TEXT,
    input_hash TEXT NOT NULL,
    before_observed_start TEXT NOT NULL,
    before_observed_end TEXT NOT NULL,
    after_observed_start TEXT NOT NULL,
    after_observed_end TEXT NOT NULL,
    common_coverage_fraction REAL CHECK(common_coverage_fraction BETWEEN 0 AND 1),
    object_count INTEGER NOT NULL CHECK(object_count BETWEEN 1 AND 30),
    review_status TEXT NOT NULL
        CHECK(review_status IN ('pending', 'approved', 'rejected')),
    metrics_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workbench_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT UNIQUE NOT NULL,
    source_repository TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    backup_created_at TEXT,
    backup_manifest_sha256 TEXT NOT NULL,
    acquisition_manifest_sha256 TEXT,
    acquisition_receipt_count INTEGER NOT NULL DEFAULT 0,
    observation_comparison_count INTEGER NOT NULL DEFAULT 0,
    research_record_count INTEGER NOT NULL DEFAULT 0,
    synthetic_record_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_archive_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_record_id TEXT UNIQUE NOT NULL,
    import_id TEXT NOT NULL
        REFERENCES workbench_import_batches(import_id) ON DELETE CASCADE,
    source_database TEXT NOT NULL,
    record_type TEXT NOT NULL,
    title TEXT,
    issue_time TEXT,
    input_sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('synthetic', 'observed', 'mixed', 'unknown')),
    research_only INTEGER NOT NULL DEFAULT 1 CHECK(research_only IN (0, 1)),
    result_summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_archive_import
    ON research_archive_records(import_id, source_database, record_type);

CREATE TABLE IF NOT EXISTS notification_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('sms', 'email', 'webhook')),
    destination TEXT NOT NULL,
    country TEXT CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
    minimum_severity TEXT NOT NULL DEFAULT 'critical'
        CHECK(minimum_severity IN ('warning', 'critical')),
    environment TEXT NOT NULL DEFAULT 'test'
        CHECK(environment IN ('test', 'operational')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(channel, destination, country)
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL
        REFERENCES notification_subscriptions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    queued_at TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at TEXT,
    provider_message_id TEXT,
    error_message TEXT,
    recipient_acknowledged_at TEXT,
    recipient_acknowledgement_note TEXT,
    action_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(action_status IN ('unknown', 'received', 'acted', 'unreachable')),
    UNIQUE(alert_id, subscription_id)
);

CREATE TABLE IF NOT EXISTS hazard_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    corridor TEXT NOT NULL,
    trigger_summary TEXT NOT NULL,
    assessment_status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    peak_rise_m REAL,
    rise_minutes INTEGER,
    decision_state TEXT NOT NULL,
    source_url TEXT NOT NULL,
    limitations TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downstream_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES hazard_cases(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    district TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    exposure TEXT NOT NULL,
    sequence_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT UNIQUE NOT NULL,
    evidence_type TEXT NOT NULL,
    access_scope TEXT NOT NULL,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    limitation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glacier_catalogs (
    catalog_key TEXT PRIMARY KEY,
    catalog_name TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    spatial_boundary TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    coverage_statement TEXT NOT NULL,
    is_authoritative INTEGER NOT NULL CHECK(is_authoritative IN (0, 1))
);

CREATE TABLE IF NOT EXISTS glacier_inventory (
    inventory_id TEXT PRIMARY KEY,
    name TEXT,
    country TEXT NOT NULL CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
    region TEXT NOT NULL,
    latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
    min_longitude REAL,
    min_latitude REAL,
    max_longitude REAL,
    max_latitude REAL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL CHECK(is_demo IN (0, 1))
);

CREATE TABLE IF NOT EXISTS glacier_imagery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    glacier_id TEXT NOT NULL REFERENCES glacier_inventory(inventory_id) ON DELETE CASCADE,
    provider_item_id TEXT UNIQUE NOT NULL,
    acquired_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source TEXT NOT NULL,
    processing_level TEXT NOT NULL,
    cloud_cover REAL CHECK(cloud_cover BETWEEN 0 AND 100),
    thumbnail_url TEXT,
    thumbnail_kind TEXT NOT NULL
        CHECK(thumbnail_kind IN ('none', 'provider_quicklook', 'generated_preview')),
    external_url TEXT NOT NULL,
    quality_status TEXT NOT NULL
        CHECK(quality_status IN ('validated_metadata', 'provisional', 'unverified')),
    is_synthetic INTEGER NOT NULL CHECK(is_synthetic IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_asset_time ON readings(asset_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_asset_time
    ON weather_observations(asset_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_records_dataset
    ON external_dataset_records(dataset_id, record_type, observed_at);
CREATE INDEX IF NOT EXISTS idx_gee_features_asset_time
    ON gee_features(asset_id, observed_end DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_events_received ON source_events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_review
    ON evidence_records(review_status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_runs_status
    ON model_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_eligibility
    ON acquisition_receipts(eligibility_state, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_comparisons_review
    ON observation_comparisons(review_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
    ON notification_deliveries(status, queued_at);
CREATE INDEX IF NOT EXISTS idx_glacier_country_region
    ON glacier_inventory(country, region, inventory_id);
CREATE INDEX IF NOT EXISTS idx_imagery_acquired
    ON glacier_imagery(acquired_at DESC, glacier_id);
"""


HAZARD_CASE = {
    "slug": "bhote-koshi-trishuli-2026",
    "title": "Bhote Koshi–Trishuli cascading flash flood",
    "occurred_at": "2026-08-26T09:00:00+05:45",
    "corridor": "Nepal–Tibet border → Lende Khola → Bhote Koshi → Trishuli",
    "trigger_summary": "Suspected high-altitude ice-rock avalanche; preliminary assessment only.",
    "assessment_status": "Post-event research case",
    "confidence": "Preliminary / trigger not independently validated",
    "peak_rise_m": 9.0,
    "rise_minutes": 30,
    "decision_state": "No operational forecast — retrospective scenario",
    "source_url": "https://www.icimod.org/category/media-advisory/feed/",
    "limitations": (
        "Event facts are based on the supplied preliminary ICIMOD account. "
        "Causation, timing, and climate attribution require formal investigation."
    ),
}

DOWNSTREAM_LOCATIONS = [
    ("Rasuwagadhi / Lende Khola", "Rasuwa", 28.276, 85.378, "Border communities and transport infrastructure", 1),
    ("Syabrubesi", "Rasuwa", 28.164, 85.348, "Settlements, roads, bridges, and riverbank assets", 2),
    ("Betrawati", "Rasuwa–Nuwakot", 27.963, 85.176, "Downstream settlements and critical crossings", 3),
    ("Galchhi", "Dhading", 27.817, 84.787, "Reported rapid river rise; communities and highway assets", 4),
    ("Malekhu", "Dhading", 27.810, 84.837, "Downstream river corridor and transport links", 5),
]

DISCOVERY_CHECKS = [
    (
        "Nepal BIPAD", "Disaster incidents and alert records", "Metadata/record discovery",
        "reachable", "https://bipadportal.gov.np/api/",
        "Access confirms discoverability only; records are not automatically validated or fused."
    ),
    (
        "Copernicus Sentinel", "Satellite product catalog", "Catalog metadata discovery",
        "reachable", "https://documentation.dataspace.copernicus.eu/APIs.html",
        "Catalog matches do not constitute image interpretation or hazard detection."
    ),
    (
        "River and rainfall feeds", "Hydrometeorological observations", "Adapter interface only",
        "not_configured", "https://www.dhm.gov.np/",
        "No live operational Nepal DHM telemetry is connected in the default demo."
    ),
]

DEMO_GLACIERS = [
    ("HW-DEMO-NP-0001", "Khumbu Glacier", "Nepal", "Koshi / Everest", 27.983, 86.864),
    ("HW-DEMO-NP-0002", "Ngozumpa Glacier", "Nepal", "Koshi / Everest", 28.000, 86.710),
    ("HW-DEMO-NP-0003", "Imja Glacier", "Nepal", "Koshi / Everest", 27.892, 86.936),
    ("HW-DEMO-NP-0004", "Barun Glacier", "Nepal", "Koshi / Makalu", 27.790, 87.080),
    ("HW-DEMO-NP-0005", "Yala Glacier", "Nepal", "Bagmati / Langtang", 28.236, 85.613),
    ("HW-DEMO-NP-0006", "Langtang Glacier", "Nepal", "Bagmati / Langtang", 28.286, 85.720),
    ("HW-DEMO-NP-0007", None, "Nepal", "Bagmati / Langtang", 28.190, 85.690),
    ("HW-DEMO-NP-0008", None, "Nepal", "Gandaki / Manaslu", 28.530, 84.560),
    ("HW-DEMO-NP-0009", None, "Nepal", "Gandaki / Annapurna", 28.640, 84.020),
    ("HW-DEMO-NP-0010", "South Annapurna Glacier", "Nepal", "Gandaki / Annapurna", 28.530, 83.840),
    ("HW-DEMO-NP-0011", None, "Nepal", "Karnali / Dhaulagiri", 28.700, 83.440),
    ("HW-DEMO-NP-0012", None, "Nepal", "Karnali / Dolpo", 29.170, 83.080),
    ("HW-DEMO-NP-0013", None, "Nepal", "Karnali / Humla", 30.050, 81.850),
    ("HW-DEMO-NP-0014", None, "Nepal", "Sudurpashchim / Api", 30.000, 80.920),
    ("HW-DEMO-NP-0015", None, "Nepal", "Koshi / Kanchenjunga", 27.720, 88.050),
    ("HW-DEMO-NP-0016", None, "Nepal", "Koshi / Kanchenjunga", 27.810, 88.000),
    ("HW-DEMO-TB-0001", "Rongbuk Glacier", "Tibet", "Shigatse / Everest", 28.080, 86.840),
    ("HW-DEMO-TB-0002", "East Rongbuk Glacier", "Tibet", "Shigatse / Everest", 28.030, 86.910),
    ("HW-DEMO-TB-0003", None, "Tibet", "Shigatse / Himalaya", 28.350, 87.450),
    ("HW-DEMO-TB-0004", None, "Tibet", "Shigatse / Himalaya", 28.620, 85.800),
    ("HW-DEMO-TB-0005", None, "Tibet", "Shigatse / Himalaya", 29.100, 89.200),
    ("HW-DEMO-TB-0006", None, "Tibet", "Shannan / Himalaya", 28.500, 91.000),
    ("HW-DEMO-TB-0007", None, "Tibet", "Shannan / Himalaya", 28.750, 92.100),
    ("HW-DEMO-TB-0008", "Midui Glacier", "Tibet", "Nyingchi / Nyainqentanglha", 29.460, 96.500),
    ("HW-DEMO-TB-0009", None, "Tibet", "Nyingchi / Nyainqentanglha", 29.850, 95.100),
    ("HW-DEMO-TB-0010", None, "Tibet", "Nyingchi / Nyainqentanglha", 30.150, 94.400),
    ("HW-DEMO-TB-0011", None, "Tibet", "Nagqu / Nyainqentanglha", 30.800, 91.300),
    ("HW-DEMO-TB-0012", None, "Tibet", "Nagqu / Tanggula", 32.200, 92.900),
    ("HW-DEMO-TB-0013", None, "Tibet", "Nagqu / Tanggula", 32.800, 91.900),
    ("HW-DEMO-TB-0014", None, "Tibet", "Ngari / Gangdise", 30.500, 81.200),
    ("HW-DEMO-TB-0015", None, "Tibet", "Ngari / Karakoram", 34.100, 79.100),
    ("HW-DEMO-TB-0016", None, "Tibet", "Ngari / Himalaya", 30.200, 82.400),
    ("HW-DEMO-IN-0001", "Gangotri Glacier", "India", "Uttarakhand / Garhwal", 30.930, 79.070),
    ("HW-DEMO-IN-0002", "Siachen Glacier", "India", "Ladakh / Karakoram", 35.420, 77.110),
    ("HW-DEMO-IN-0003", "Bara Shigri Glacier", "India", "Himachal / Chandra", 32.230, 77.600),
    ("HW-DEMO-IN-0004", "Chhota Shigri Glacier", "India", "Himachal / Chandra", 32.280, 77.580),
    ("HW-DEMO-IN-0005", "Samudra Tapu Glacier", "India", "Himachal / Chandra", 32.500, 77.500),
    ("HW-DEMO-IN-0006", None, "India", "Uttarakhand / Kumaon", 30.180, 80.010),
    ("HW-DEMO-IN-0007", None, "India", "Sikkim / Teesta", 27.860, 88.550),
    ("HW-DEMO-IN-0008", None, "India", "Arunachal / Eastern Himalaya", 28.620, 95.520),
    ("HW-DEMO-PK-0001", "Baltoro Glacier", "Pakistan", "Gilgit-Baltistan / Karakoram", 35.7416, 76.4047),
    ("HW-DEMO-PK-0002", "Biafo Glacier", "Pakistan", "Gilgit-Baltistan / Karakoram", 36.0064, 75.5932),
    ("HW-DEMO-PK-0003", "Hispar Glacier", "Pakistan", "Gilgit-Baltistan / Karakoram", 36.0550, 75.2810),
    ("HW-DEMO-PK-0004", "Batura Glacier", "Pakistan", "Gilgit-Baltistan / Karakoram", 36.5007, 74.5813),
    ("HW-DEMO-PK-0005", "Barpu Glacier", "Pakistan", "Gilgit-Baltistan / Karakoram", 36.0673, 74.8171),
    ("HW-DEMO-PK-0006", None, "Pakistan", "Gilgit-Baltistan / Karakoram", 35.850, 76.150),
    ("HW-DEMO-PK-0007", None, "Pakistan", "Gilgit-Baltistan / Karakoram", 36.220, 74.900),
    ("HW-DEMO-PK-0008", None, "Pakistan", "Khyber Pakhtunkhwa / Hindu Kush", 36.430, 72.500),
]


ASSETS = [
    {
        "slug": "khumbu-glacier", "name": "Khumbu Glacier", "country": "Nepal",
        "asset_type": "glacier", "latitude": 27.983, "longitude": 86.864,
        "area_km2": 15.0, "elevation_m": 4900, "trend_value": -20.0,
        "trend_unit": "m/year terminus retreat (representative literature value)",
        "risk_level": "high", "operational_status": "not_applicable",
        "measurement_kind": "satellite-derived glacier change", "units": "m/year",
        "source_url": "https://www.glims.org/glacierdata/",
        "data_classification": "Curated location; synthetic demo observation",
        "notes": "Catalog geometry is representative. Connect GLIMS/ITS_LIVE adapters for analysis.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "ngo-zumpa-glacier", "name": "Ngozumpa Glacier", "country": "Nepal",
        "asset_type": "glacier", "latitude": 28.00, "longitude": 86.71,
        "area_km2": 61.0, "elevation_m": 4700, "trend_value": -18.0,
        "trend_unit": "m/year terminus retreat (representative literature value)",
        "risk_level": "moderate", "operational_status": "not_applicable",
        "measurement_kind": "satellite-derived glacier change", "units": "m/year",
        "source_url": "https://nsidc.org/data/glims",
        "data_classification": "Curated location; synthetic demo observation",
        "notes": "Area is approximate and should be refreshed from an authoritative inventory.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "rongbuk-glacier", "name": "Rongbuk Glacier", "country": "Tibet",
        "asset_type": "glacier", "latitude": 28.08, "longitude": 86.84,
        "area_km2": 15.0, "elevation_m": 5200, "trend_value": -12.0,
        "trend_unit": "m/year retreat (synthetic demo)",
        "risk_level": "moderate", "operational_status": "not_applicable",
        "measurement_kind": "satellite-derived glacier change", "units": "m/year",
        "source_url": "https://its-live.jpl.nasa.gov/",
        "data_classification": "Curated regional example; synthetic demo observation",
        "notes": "Not a live feed. ITS_LIVE is the intended velocity provider.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "imja-tsho", "name": "Imja Tsho", "country": "Nepal",
        "asset_type": "lake", "latitude": 27.899, "longitude": 86.923,
        "area_km2": 1.35, "elevation_m": 5010, "risk_level": "high",
        "operational_status": "unknown", "measurement_kind": "water level",
        "units": "m", "source_url": "https://rds.icimod.org/",
        "data_classification": "Curated asset; synthetic demo readings",
        "notes": "Readings are explicitly synthetic and are not an emergency warning feed.",
        "warning_threshold": 5.0, "critical_threshold": 5.8, "rapid_rise_threshold": 0.20,
    },
    {
        "slug": "tsho-rolpa", "name": "Tsho Rolpa", "country": "Nepal",
        "asset_type": "lake", "latitude": 27.858, "longitude": 86.477,
        "area_km2": 1.53, "elevation_m": 4580, "risk_level": "high",
        "operational_status": "unknown", "measurement_kind": "water level",
        "units": "m", "source_url": "https://www.dhm.gov.np/",
        "data_classification": "Curated asset; synthetic demo readings",
        "notes": "Representative monitoring scenario; not connected to Nepal DHM telemetry.",
        "warning_threshold": 4.5, "critical_threshold": 5.2, "rapid_rise_threshold": 0.18,
    },
    {
        "slug": "longbasaba-lake", "name": "Longbasaba Lake", "country": "Tibet",
        "asset_type": "lake", "latitude": 28.09, "longitude": 88.08,
        "area_km2": 1.0, "elevation_m": 5490, "risk_level": "high",
        "operational_status": "unknown", "measurement_kind": "water level",
        "units": "m", "source_url": "https://www.icimod.org/mountain/glaciers/",
        "data_classification": "Curated regional example; synthetic demo readings",
        "notes": "Location context is regional; readings are synthetic.",
        "warning_threshold": 4.2, "critical_threshold": 5.0, "rapid_rise_threshold": 0.15,
    },
    {
        "slug": "evk2cnh-pyramid", "name": "Pyramid Observatory", "country": "Nepal",
        "asset_type": "station", "latitude": 27.959, "longitude": 86.813,
        "elevation_m": 5079, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "high-altitude atmospheric observatory", "operator": "Ev-K2-CNR / partners",
        "measurement_kind": "measured CO2", "units": "ppm",
        "source_url": "https://www.isac.cnr.it/en/infrastructures/abc-pyramid",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "CO2 values shown in this demo are synthetic, not current observatory data.",
        "stale_after_hours": 48,
    },
    {
        "slug": "nam-co-station", "name": "Nam Co Monitoring and Research Station", "country": "Tibet",
        "asset_type": "station", "latitude": 30.773, "longitude": 90.962,
        "elevation_m": 4730, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "plateau environmental research station",
        "operator": "Institute of Tibetan Plateau Research, CAS",
        "measurement_kind": "weather / modeled CO2 proxy", "units": "ppm-equivalent",
        "source_url": "http://english.itpcas.cas.cn/",
        "data_classification": "Real station metadata; synthetic modeled proxy",
        "notes": "Modeled proxy is not a direct CO2 measurement.",
        "stale_after_hours": 24,
    },
    {
        "slug": "lukla-weather", "name": "Lukla Weather Reference", "country": "Nepal",
        "asset_type": "station", "latitude": 27.687, "longitude": 86.731,
        "elevation_m": 2860, "risk_level": "unknown", "operational_status": "offline",
        "device_type": "automatic weather station", "operator": "Demo adapter / Nepal DHM target",
        "measurement_kind": "weather proxy", "units": "degC",
        "source_url": "https://www.dhm.gov.np/",
        "data_classification": "Synthetic demo station and reading",
        "notes": "Used to demonstrate stale/offline alert handling.",
        "stale_after_hours": 12,
    },
]

CORRIDOR_ASSETS = [
    {
        "slug": "yala-glacier", "name": "Yala Glacier", "country": "Nepal",
        "asset_type": "glacier", "latitude": 28.23526, "longitude": 85.61263,
        "area_km2": 1.6, "elevation_m": 5168, "trend_value": None,
        "trend_unit": "See source mass-balance series",
        "risk_level": "unknown", "operational_status": "not_applicable",
        "measurement_kind": "glacier mass balance and weather observations",
        "units": "m/day",
        "source_url": "https://rds.icimod.org/Home/DataDetail?metadataId=1972507",
        "data_classification": "Real monitored glacier metadata; synthetic demo movement reading",
        "notes": "ICIMOD and partners monitor Yala Glacier. Demo velocity is not a collapse forecast.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "langtang-glacier", "name": "Langtang Glacier", "country": "Nepal",
        "asset_type": "glacier", "latitude": 28.2411, "longitude": 85.7069,
        "area_km2": 46.5, "elevation_m": 4800, "trend_value": None,
        "trend_unit": "Authoritative time series not connected",
        "risk_level": "unknown", "operational_status": "not_applicable",
        "measurement_kind": "satellite-derived glacier change",
        "units": "m/day",
        "source_url": "https://rds.icimod.org/",
        "data_classification": "Curated glacier metadata; synthetic demo movement reading",
        "notes": "Representative centroid; import GLIMS/RGI geometry for authoritative boundaries.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "yala-aws", "name": "Yala Glacier AWS", "country": "Nepal",
        "asset_type": "station", "latitude": 28.232, "longitude": 85.610,
        "elevation_m": 5350, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "automatic weather station", "operator": "ICIMOD / research partners",
        "measurement_kind": "weather and glacier energy-balance observations",
        "units": "degC",
        "source_url": "https://rds.icimod.org/Home/DataDetail?metadataId=1972507",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "The dashboard does not claim a live feed or current operational status.",
        "stale_after_hours": 24,
    },
    {
        "slug": "syabrubesi-hydromet-reference",
        "name": "Syabrubesi Hydro-Met Reference", "country": "Nepal",
        "asset_type": "station", "latitude": 28.162, "longitude": 85.334,
        "elevation_m": 1470, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "hydrometeorological reference point",
        "operator": "Nepal DHM adapter target",
        "measurement_kind": "river-level and weather context",
        "units": "m",
        "source_url": "https://www.dhm.gov.np/",
        "data_classification": "Synthetic scenario location; no verified live station feed",
        "notes": (
            "Shown to make the monitoring gap explicit, not to imply a functioning sensor. "
            "Thresholds are synthetic exercise values, not official flood-warning criteria."
        ),
        "warning_threshold": 3.0,
        "critical_threshold": 5.0,
        "rapid_rise_threshold": 0.5,
        "stale_after_hours": 12,
    },
]

REGIONAL_ASSETS = [
    {
        "slug": "gangotri-glacier", "name": "Gangotri Glacier", "country": "India",
        "asset_type": "glacier", "latitude": 30.930, "longitude": 79.070,
        "elevation_m": 4000, "risk_level": "unknown", "operational_status": "not_applicable",
        "measurement_kind": "glacier inventory / satellite change", "units": "m/day",
        "source_url": "https://v1.wii.gov.in/nmcg/protected-areas-along-ganga/gangotri-national-park",
        "data_classification": "Curated source metadata; synthetic demo movement reading",
        "notes": "Representative point near Gaumukh; not a live glacier-movement feed.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "siachen-glacier", "name": "Siachen Glacier", "country": "India",
        "asset_type": "glacier", "latitude": 35.420, "longitude": 77.110,
        "elevation_m": 5400, "risk_level": "unknown", "operational_status": "not_applicable",
        "measurement_kind": "glacier inventory / satellite change", "units": "m/day",
        "source_url": "https://vedas.sac.gov.in/en/Himalayan_Glacier_Inventory_Atlas.html",
        "data_classification": "Curated source metadata; synthetic demo movement reading",
        "notes": "Representative point; boundaries should come from ISRO/GLIMS/RGI.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "bara-shigri-glacier", "name": "Bara Shigri Glacier", "country": "India",
        "asset_type": "glacier", "latitude": 32.230, "longitude": 77.600,
        "area_km2": 137.0, "elevation_m": 5000, "risk_level": "unknown",
        "operational_status": "not_applicable",
        "measurement_kind": "benchmark glacier monitoring", "units": "m/day",
        "source_url": "https://www.ncpor.res.in/pages/view/115/426-himansh",
        "data_classification": "Curated NCPOR metadata; synthetic demo movement reading",
        "notes": "Area is source/date dependent; no live movement feed is connected.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "chhota-shigri-glacier", "name": "Chhota Shigri Glacier", "country": "India",
        "asset_type": "glacier", "latitude": 32.280, "longitude": 77.580,
        "area_km2": 15.7, "elevation_m": 5000, "risk_level": "unknown",
        "operational_status": "not_applicable",
        "measurement_kind": "benchmark glacier monitoring", "units": "m/day",
        "source_url": "https://pdc.ncpor.res.in/npdc/himalaya_home.action",
        "data_classification": "Curated NCPOR/GLIMS metadata; synthetic demo movement reading",
        "notes": "Approximate centroid and area; use source inventory geometry for analysis.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "south-lhonak-lake", "name": "South Lhonak Lake", "country": "India",
        "asset_type": "lake", "latitude": 27.858, "longitude": 88.512,
        "area_km2": 1.05, "elevation_m": 5200, "risk_level": "high",
        "operational_status": "unknown", "measurement_kind": "satellite-derived lake extent",
        "units": "m", "source_url": "https://www.isro.gov.in/ISRO_EN/Satellite_studies_South_Lhonak_Lake.html",
        "data_classification": "Curated historical metadata; synthetic demo water-level reading",
        "notes": "Area reflects pre-October 2023 imagery, not current lake extent.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "tso-moriri", "name": "Tso Moriri", "country": "India",
        "asset_type": "lake", "latitude": 32.900, "longitude": 78.300,
        "area_km2": 120.0, "elevation_m": 4595, "risk_level": "unknown",
        "operational_status": "unknown", "measurement_kind": "high-altitude lake context",
        "units": "m", "source_url": "https://rsis.ramsar.org/ris/1213",
        "data_classification": "Ramsar metadata; synthetic demo water-level reading",
        "notes": "High-altitude lake context; not classified here as an unstable glacial lake.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "chandertal", "name": "Chandertal", "country": "India",
        "asset_type": "lake", "latitude": 32.483, "longitude": 77.600,
        "area_km2": 0.49, "elevation_m": 4337, "risk_level": "unknown",
        "operational_status": "unknown", "measurement_kind": "high-altitude lake context",
        "units": "m", "source_url": "https://rsis.ramsar.org/ris/1569",
        "data_classification": "Ramsar metadata; synthetic demo water-level reading",
        "notes": "No live water-level feed is connected.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "himansh-station", "name": "HIMANSH Research Station", "country": "India",
        "asset_type": "station", "latitude": 32.4094, "longitude": 77.6089,
        "elevation_m": 4080, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "high-altitude glaciology research station", "operator": "NCPOR / MoES",
        "measurement_kind": "weather and glacier observations", "units": "degC",
        "source_url": "https://www.ncpor.res.in/pages/view/115/426-himansh",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "No live station telemetry is connected.",
        "stale_after_hours": 24,
    },
    {
        "slug": "hanle-observatory", "name": "Indian Astronomical Observatory, Hanle",
        "country": "India", "asset_type": "station", "latitude": 32.7794,
        "longitude": 78.9642, "elevation_m": 4500, "risk_level": "unknown",
        "operational_status": "unknown", "device_type": "astronomical and atmospheric observatory",
        "operator": "Indian Institute of Astrophysics", "measurement_kind": "atmospheric/weather context",
        "units": "degC", "source_url": "https://www.iiap.res.in/centers/iao/",
        "data_classification": "Real observatory metadata; synthetic demo reading",
        "notes": "Not presented as an operational hazard-warning station.",
        "stale_after_hours": 24,
    },
    {
        "slug": "baltoro-glacier", "name": "Baltoro Glacier", "country": "Pakistan",
        "asset_type": "glacier", "latitude": 35.7416, "longitude": 76.4047,
        "area_km2": 809.1, "elevation_m": 5393, "risk_level": "unknown",
        "operational_status": "not_applicable", "measurement_kind": "RGI glacier inventory",
        "units": "m/day", "source_url": "https://www.glims.org/glacierdata/",
        "data_classification": "RGI 6-derived metadata; synthetic demo movement reading",
        "notes": "Inventory values are versioned, not current measurements.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "biafo-glacier", "name": "Biafo Glacier", "country": "Pakistan",
        "asset_type": "glacier", "latitude": 36.0064, "longitude": 75.5932,
        "area_km2": 559.8, "elevation_m": 5043, "risk_level": "unknown",
        "operational_status": "not_applicable", "measurement_kind": "RGI glacier inventory",
        "units": "m/day", "source_url": "https://www.glims.org/glacierdata/",
        "data_classification": "RGI 6-derived metadata; synthetic demo movement reading",
        "notes": "Inventory values are versioned, not current measurements.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "hispar-glacier", "name": "Hispar Glacier", "country": "Pakistan",
        "asset_type": "glacier", "latitude": 36.0550, "longitude": 75.2810,
        "area_km2": 495.6, "elevation_m": 4854, "risk_level": "unknown",
        "operational_status": "not_applicable", "measurement_kind": "RGI glacier inventory",
        "units": "m/day", "source_url": "https://www.glims.org/glacierdata/",
        "data_classification": "RGI 6-derived metadata; synthetic demo movement reading",
        "notes": "Inventory values are versioned, not current measurements.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "batura-glacier", "name": "Batura Glacier", "country": "Pakistan",
        "asset_type": "glacier", "latitude": 36.5007, "longitude": 74.5813,
        "area_km2": 311.7, "elevation_m": 4932, "risk_level": "unknown",
        "operational_status": "not_applicable", "measurement_kind": "RGI glacier inventory",
        "units": "m/day", "source_url": "https://www.glims.org/glacierdata/",
        "data_classification": "RGI 6-derived metadata; synthetic demo movement reading",
        "notes": "PMD documents glacier-region monitoring; no live feed is connected.",
        "abnormal_movement_threshold": 1.0,
    },
    {
        "slug": "sheosar-lake", "name": "Sheosar Lake", "country": "Pakistan",
        "asset_type": "lake", "latitude": 35.030, "longitude": 75.540,
        "elevation_m": 4114, "risk_level": "unknown", "operational_status": "unknown",
        "measurement_kind": "high-altitude lake context", "units": "m",
        "source_url": "https://visitgilgitbaltistan.gov.pk/tour/id/3",
        "data_classification": "Curated government tourism metadata; synthetic demo reading",
        "notes": "Published elevation sources vary; not classified here as a proglacial lake.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "rush-lake", "name": "Rush Lake", "country": "Pakistan",
        "asset_type": "lake", "latitude": 36.120, "longitude": 74.920,
        "elevation_m": 4694, "risk_level": "unknown", "operational_status": "unknown",
        "measurement_kind": "high-altitude lake context", "units": "m",
        "source_url": "https://visitgilgitbaltistan.gov.pk/tour/id/38",
        "data_classification": "Curated government tourism metadata; synthetic demo reading",
        "notes": "Approximate map point; no live water-level feed is connected.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "saiful-muluk-lake", "name": "Lake Saiful Muluk", "country": "Pakistan",
        "asset_type": "lake", "latitude": 34.876, "longitude": 73.694,
        "elevation_m": 3224, "risk_level": "unknown", "operational_status": "unknown",
        "measurement_kind": "high-altitude lake and climate context", "units": "m",
        "source_url": "https://wit.lums.edu.pk/news/wit-and-gmrc-collaborate-strengthen-climate-monitoring-lake-saiful-muluk",
        "data_classification": "Curated research metadata; synthetic demo water-level reading",
        "notes": "High-altitude lake context; no live lake-level feed is connected.",
        "warning_threshold": 4.0, "critical_threshold": 5.0, "rapid_rise_threshold": 0.2,
    },
    {
        "slug": "astore-observatory", "name": "Astore Observatory", "country": "Pakistan",
        "asset_type": "station", "latitude": 35.333, "longitude": 74.900,
        "elevation_m": 2168, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "meteorological observatory", "operator": "Pakistan Meteorological Department",
        "measurement_kind": "weather observations", "units": "degC",
        "source_url": "https://www.pmd.gov.pk/Observatories/",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "No live PMD telemetry is connected.",
        "stale_after_hours": 24,
    },
    {
        "slug": "skardu-observatory", "name": "Skardu Observatory", "country": "Pakistan",
        "asset_type": "station", "latitude": 35.300, "longitude": 75.683,
        "elevation_m": 2317, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "meteorological observatory", "operator": "Pakistan Meteorological Department",
        "measurement_kind": "weather observations", "units": "degC",
        "source_url": "https://www.pmd.gov.pk/Observatories/",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "No live PMD telemetry is connected.",
        "stale_after_hours": 24,
    },
    {
        "slug": "hunza-observatory", "name": "Hunza Observatory", "country": "Pakistan",
        "asset_type": "station", "latitude": 36.317, "longitude": 74.650,
        "elevation_m": 2156, "risk_level": "unknown", "operational_status": "unknown",
        "device_type": "meteorological observatory", "operator": "Pakistan Meteorological Department",
        "measurement_kind": "weather observations", "units": "degC",
        "source_url": "https://www.pmd.gov.pk/Observatories/",
        "data_classification": "Real station metadata; synthetic demo reading",
        "notes": "No live PMD telemetry is connected.",
        "stale_after_hours": 24,
    },
]


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def init_db(seed=True):
    database = get_db()
    database.executescript(SCHEMA)
    migrate_country_constraints(database)
    migrate_notification_channels(database)
    ensure_schema_columns(database)
    if seed and database.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
        seed_db(database)
    if seed and database.execute("SELECT COUNT(*) FROM hazard_cases").fetchone()[0] == 0:
        seed_research_context(database)
    if seed and database.execute("SELECT COUNT(*) FROM glacier_inventory").fetchone()[0] == 0:
        seed_imagery_catalog(database)
    if seed:
        expand_demo_glacier_catalog(database)
        seed_corridor_assets(database)
        seed_regional_assets(database)
    database.commit()


def migrate_country_constraints(database):
    assets_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'assets'"
    ).fetchone()["sql"]
    inventory_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'glacier_inventory'"
    ).fetchone()["sql"]
    if "'India'" in assets_sql and "'India'" in inventory_sql:
        return
    database.commit()
    database.execute("PRAGMA foreign_keys = OFF")
    if "'India'" not in assets_sql:
        database.executescript("""
            CREATE TABLE assets_region (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                country TEXT NOT NULL CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
                asset_type TEXT NOT NULL CHECK(asset_type IN ('glacier', 'lake', 'station')),
                latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                area_km2 REAL, elevation_m REAL, observation_time TEXT,
                trend_value REAL, trend_unit TEXT,
                risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'moderate', 'high', 'critical', 'unknown')),
                operational_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK(operational_status IN ('online', 'offline', 'unknown', 'not_applicable')),
                device_type TEXT, operator TEXT, measurement_kind TEXT, units TEXT,
                source_url TEXT NOT NULL, data_classification TEXT NOT NULL, notes TEXT,
                warning_threshold REAL, critical_threshold REAL, rapid_rise_threshold REAL,
                abnormal_movement_threshold REAL, stale_after_hours INTEGER NOT NULL DEFAULT 72
            );
            INSERT INTO assets_region SELECT * FROM assets;
            DROP TABLE assets;
            ALTER TABLE assets_region RENAME TO assets;
            CREATE INDEX IF NOT EXISTS idx_assets_country_type ON assets(country, asset_type);
        """)
    if "'India'" not in inventory_sql:
        database.executescript("""
            CREATE TABLE glacier_inventory_region (
                inventory_id TEXT PRIMARY KEY,
                name TEXT,
                country TEXT NOT NULL CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
                region TEXT NOT NULL,
                latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                min_longitude REAL, min_latitude REAL, max_longitude REAL, max_latitude REAL,
                source_name TEXT NOT NULL, source_url TEXT NOT NULL,
                catalog_version TEXT NOT NULL, ingested_at TEXT NOT NULL,
                is_demo INTEGER NOT NULL CHECK(is_demo IN (0, 1))
            );
            INSERT INTO glacier_inventory_region SELECT * FROM glacier_inventory;
            DROP TABLE glacier_inventory;
            ALTER TABLE glacier_inventory_region RENAME TO glacier_inventory;
            CREATE INDEX IF NOT EXISTS idx_glacier_country_region
                ON glacier_inventory(country, region, inventory_id);
        """)
    database.execute("PRAGMA foreign_keys = ON")


def migrate_notification_channels(database):
    table = database.execute(
        """SELECT sql FROM sqlite_master
           WHERE type = 'table' AND name = 'notification_subscriptions'"""
    ).fetchone()
    if not table or "'email'" in table["sql"]:
        return
    database.commit()
    database.execute("PRAGMA foreign_keys = OFF")
    database.executescript("""
        CREATE TABLE notification_subscriptions_email (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ('sms', 'email', 'webhook')),
            destination TEXT NOT NULL,
            country TEXT CHECK(country IN ('Nepal', 'Tibet', 'India', 'Pakistan')),
            minimum_severity TEXT NOT NULL DEFAULT 'critical'
                CHECK(minimum_severity IN ('warning', 'critical')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            UNIQUE(channel, destination, country)
        );
        INSERT INTO notification_subscriptions_email
            SELECT * FROM notification_subscriptions;

        CREATE TABLE notification_deliveries_email (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
            subscription_id INTEGER NOT NULL
                REFERENCES notification_subscriptions_email(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'sending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            queued_at TEXT NOT NULL,
            last_attempt_at TEXT,
            sent_at TEXT,
            provider_message_id TEXT,
            error_message TEXT,
            UNIQUE(alert_id, subscription_id)
        );
        INSERT INTO notification_deliveries_email
            SELECT * FROM notification_deliveries;

        DROP TABLE notification_deliveries;
        DROP TABLE notification_subscriptions;
        ALTER TABLE notification_subscriptions_email
            RENAME TO notification_subscriptions;
        ALTER TABLE notification_deliveries_email
            RENAME TO notification_deliveries;
        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
            ON notification_deliveries(status, queued_at);
    """)
    database.execute("PRAGMA foreign_keys = ON")


def ensure_schema_columns(database):
    columns = {row["name"] for row in database.execute("PRAGMA table_info(readings)")}
    if "available_at" not in columns:
        database.execute("ALTER TABLE readings ADD COLUMN available_at TEXT")
        database.execute("UPDATE readings SET available_at = created_at WHERE available_at IS NULL")
    if "quality_status" not in columns:
        database.execute(
            "ALTER TABLE readings ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unverified'"
        )
    alert_columns = {row["name"] for row in database.execute("PRAGMA table_info(alerts)")}
    if "authorized_at" not in alert_columns:
        database.execute("ALTER TABLE alerts ADD COLUMN authorized_at TEXT")
    if "authorized_by" not in alert_columns:
        database.execute("ALTER TABLE alerts ADD COLUMN authorized_by TEXT")
    if "authorization_note" not in alert_columns:
        database.execute("ALTER TABLE alerts ADD COLUMN authorization_note TEXT")
    if "evidence_reading_id" not in alert_columns:
        database.execute(
            "ALTER TABLE alerts ADD COLUMN evidence_reading_id INTEGER "
            "REFERENCES readings(id) ON DELETE SET NULL"
        )
    subscription_columns = {
        row["name"] for row in database.execute("PRAGMA table_info(notification_subscriptions)")
    }
    if "environment" not in subscription_columns:
        database.execute(
            "ALTER TABLE notification_subscriptions ADD COLUMN environment TEXT "
            "NOT NULL DEFAULT 'test' CHECK(environment IN ('test', 'operational'))"
        )
    delivery_columns = {
        row["name"] for row in database.execute("PRAGMA table_info(notification_deliveries)")
    }
    if "recipient_acknowledged_at" not in delivery_columns:
        database.execute(
            "ALTER TABLE notification_deliveries ADD COLUMN recipient_acknowledged_at TEXT"
        )
    if "recipient_acknowledgement_note" not in delivery_columns:
        database.execute(
            "ALTER TABLE notification_deliveries ADD COLUMN recipient_acknowledgement_note TEXT"
        )
    if "action_status" not in delivery_columns:
        database.execute(
            "ALTER TABLE notification_deliveries ADD COLUMN action_status TEXT "
            "NOT NULL DEFAULT 'unknown' "
            "CHECK(action_status IN ('unknown', 'received', 'acted', 'unreachable'))"
        )
    model_columns = {
        row["name"] for row in database.execute("PRAGMA table_info(model_runs)")
    }
    model_column_definitions = {
        "artifact_schema": "TEXT",
        "dataset_sha256": "TEXT",
        "protocol_sha256": "TEXT",
        "feature_schema_json": "TEXT NOT NULL DEFAULT '[]'",
        "station_id": "TEXT",
        "reference_datum": "TEXT",
        "fit_available_at": "TEXT",
        "training_domain_json": "TEXT NOT NULL DEFAULT '{}'",
        "research_only": "INTEGER NOT NULL DEFAULT 1 CHECK(research_only IN (0, 1))",
    }
    for column, definition in model_column_definitions.items():
        if column not in model_columns:
            database.execute(f"ALTER TABLE model_runs ADD COLUMN {column} {definition}")


def seed_research_context(database):
    columns = ", ".join(HAZARD_CASE)
    placeholders = ", ".join("?" for _ in HAZARD_CASE)
    cursor = database.execute(
        f"INSERT INTO hazard_cases ({columns}) VALUES ({placeholders})",
        tuple(HAZARD_CASE.values()),
    )
    for location in DOWNSTREAM_LOCATIONS:
        database.execute(
            """INSERT INTO downstream_locations
               (case_id, name, district, latitude, longitude, exposure, sequence_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cursor.lastrowid, *location),
        )
    checked_at = utcnow().isoformat()
    for provider, evidence_type, access_scope, status, source_url, limitation in DISCOVERY_CHECKS:
        database.execute(
            """INSERT INTO discovery_checks
               (provider, evidence_type, access_scope, status, checked_at, source_url, limitation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (provider, evidence_type, access_scope, status, checked_at, source_url, limitation),
        )


def seed_imagery_catalog(database):
    now = utcnow()
    database.execute(
        """INSERT INTO glacier_catalogs
           (catalog_key, catalog_name, catalog_version, spatial_boundary, ingested_at,
            coverage_statement, is_authoritative)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (
            "default", "HimalayaWatch deterministic demonstration catalog", "demo-2026.09",
            "Representative centroid points across Nepal, Tibet, India, and Pakistan",
            now.isoformat(),
            "48 demonstration records; not a complete glacier inventory. Import a "
            "licensed GLIMS or RGI export for authoritative analysis.",
        ),
    )
    for inventory_id, name, country, region, latitude, longitude in DEMO_GLACIERS:
        database.execute(
            """INSERT INTO glacier_inventory
               (inventory_id, name, country, region, latitude, longitude,
                min_longitude, min_latitude, max_longitude, max_latitude,
                source_name, source_url, catalog_version, ingested_at, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                inventory_id, name, country, region, latitude, longitude,
                longitude - 0.03, latitude - 0.03, longitude + 0.03, latitude + 0.03,
                "HimalayaWatch deterministic demo catalog",
                "https://www.glims.org/RGI/rgi7_intro.html",
                "demo-2026.09", now.isoformat(),
            ),
        )
    refresh_demo_imagery(database, now=now)


def expand_demo_glacier_catalog(database):
    catalog = database.execute(
        "SELECT * FROM glacier_catalogs WHERE catalog_key = 'default'"
    ).fetchone()
    if not catalog or catalog["is_authoritative"]:
        return
    now = utcnow()
    inserted = 0
    for inventory_id, name, country, region, latitude, longitude in DEMO_GLACIERS:
        cursor = database.execute(
            """INSERT OR IGNORE INTO glacier_inventory
               (inventory_id, name, country, region, latitude, longitude,
                min_longitude, min_latitude, max_longitude, max_latitude,
                source_name, source_url, catalog_version, ingested_at, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                inventory_id, name, country, region, latitude, longitude,
                longitude - 0.03, latitude - 0.03, longitude + 0.03, latitude + 0.03,
                "HimalayaWatch deterministic demo catalog",
                "https://www.glims.org/RGI/rgi7_intro.html",
                "demo-2026.09", now.isoformat(),
            ),
        )
        inserted += cursor.rowcount
    database.execute(
        """UPDATE glacier_catalogs
           SET spatial_boundary = ?, coverage_statement = ?, ingested_at = ?
           WHERE catalog_key = 'default'""",
        (
            "Representative centroid points across Nepal, Tibet, India, and Pakistan",
            f"{len(DEMO_GLACIERS)} demonstration records; not a complete glacier "
            "inventory. Import a licensed GLIMS or RGI export for authoritative analysis.",
            now.isoformat(),
        ),
    )
    if inserted:
        refresh_demo_imagery(database, now=now)


def refresh_demo_imagery(database, now=None):
    now = now or utcnow()
    database.execute("DELETE FROM glacier_imagery WHERE is_synthetic = 1")
    glaciers = database.execute(
        "SELECT inventory_id FROM glacier_inventory WHERE is_demo = 1 ORDER BY inventory_id"
    ).fetchall()
    for index, glacier in enumerate(glaciers):
        inventory_id = glacier["inventory_id"]
        for offset in (8 + index % 5, 38 + index % 11, 70 + index % 13):
            acquired = now - timedelta(days=offset, hours=index % 12)
            available = acquired + timedelta(hours=6 + index % 9)
            database.execute(
                """INSERT INTO glacier_imagery
                   (glacier_id, provider_item_id, acquired_at, available_at, source,
                    processing_level, cloud_cover, thumbnail_url, thumbnail_kind,
                    external_url, quality_status, is_synthetic, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'none', ?, ?, 1, ?, ?)""",
                (
                    inventory_id, f"HW-SYNTH-{inventory_id}-{offset}",
                    acquired.isoformat(), available.isoformat(),
                    "Synthetic demo observation metadata", "DEMO-NOT-SATELLITE",
                    float((index * 17 + offset) % 86),
                    "https://documentation.dataspace.copernicus.eu/APIs/STAC.html",
                    "unverified",
                    json.dumps({
                        "label": "SYNTHETIC DEMO - NOT SATELLITE IMAGERY",
                        "purpose": "Interface and API behavior testing",
                    }),
                    now.isoformat(),
                ),
            )


def seed_corridor_assets(database):
    now = utcnow()
    for asset in CORRIDOR_ASSETS:
        columns = ", ".join(asset)
        placeholders = ", ".join("?" for _ in asset)
        database.execute(
            f"INSERT OR IGNORE INTO assets ({columns}) VALUES ({placeholders})",
            tuple(asset.values()),
        )
    database.execute(
        """UPDATE assets
           SET measurement_kind = 'river-level and weather context',
               units = 'm',
               warning_threshold = 3.0,
               critical_threshold = 5.0,
               rapid_rise_threshold = 0.5,
               notes = ?
           WHERE slug = 'syabrubesi-hydromet-reference'
             AND units = 'degC'
             AND data_classification LIKE 'Synthetic scenario location%'""",
        (
            "Shown to make the monitoring gap explicit, not to imply a functioning "
            "sensor. Thresholds are synthetic exercise values, not official "
            "flood-warning criteria.",
        ),
    )
    samples = {
        "yala-glacier": ("glacier_velocity_m_day", 0.12, "m/day"),
        "langtang-glacier": ("glacier_velocity_m_day", 0.16, "m/day"),
        "yala-aws": ("temperature_c", -3.4, "degC"),
        "syabrubesi-hydromet-reference": ("river_level_m", 1.2, "m"),
    }
    for slug, (metric, value, units) in samples.items():
        asset = database.execute("SELECT * FROM assets WHERE slug = ?", (slug,)).fetchone()
        if database.execute(
            "SELECT 1 FROM readings WHERE asset_id = ? AND metric = ? LIMIT 1",
            (asset["id"], metric),
        ).fetchone():
            continue
        observed = now - timedelta(hours=8)
        database.execute(
            """INSERT INTO readings
               (asset_id, metric, value, units, observed_at, available_at,
                quality_status, source_url, is_synthetic, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'unverified', ?, 1, ?, ?)""",
            (
                asset["id"], metric, value, units, observed.isoformat(),
                (observed + timedelta(hours=2)).isoformat(), asset["source_url"],
                json.dumps({
                    "label": "SYNTHETIC DEMO DATA",
                    "coverage_note": "No live operational feed is connected.",
                }),
                now.isoformat(),
            ),
        )
        database.execute(
            "UPDATE assets SET observation_time = ? WHERE id = ?",
            (observed.isoformat(), asset["id"]),
        )


def seed_regional_assets(database):
    now = utcnow()
    for index, asset in enumerate(REGIONAL_ASSETS):
        columns = ", ".join(asset)
        placeholders = ", ".join("?" for _ in asset)
        database.execute(
            f"INSERT OR IGNORE INTO assets ({columns}) VALUES ({placeholders})",
            tuple(asset.values()),
        )
        row = database.execute("SELECT * FROM assets WHERE slug = ?", (asset["slug"],)).fetchone()
        if database.execute(
            "SELECT 1 FROM readings WHERE asset_id = ? LIMIT 1", (row["id"],)
        ).fetchone():
            continue
        observed = now - timedelta(hours=12 + index % 18)
        if row["asset_type"] == "glacier":
            metric, value, units = "glacier_velocity_m_day", 0.08 + index * 0.003, "m/day"
        elif row["asset_type"] == "lake":
            metric, value, units = "water_level_m", 2.1 + (index % 4) * 0.2, "m"
        else:
            metric, value, units = "temperature_c", -5.0 + (index % 9), "degC"
        database.execute(
            """INSERT INTO readings
               (asset_id, metric, value, units, observed_at, available_at,
                quality_status, source_url, is_synthetic, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'unverified', ?, 1, ?, ?)""",
            (
                row["id"], metric, value, units, observed.isoformat(),
                (observed + timedelta(hours=3)).isoformat(), row["source_url"],
                json.dumps({
                    "label": "SYNTHETIC DEMO DATA",
                    "coverage_note": "Source metadata only; no live feed is connected.",
                }),
                now.isoformat(),
            ),
        )
        database.execute(
            "UPDATE assets SET observation_time = ? WHERE id = ?",
            (observed.isoformat(), row["id"]),
        )

def seed_db(database):
    now = utcnow()
    for asset in ASSETS:
        columns = ", ".join(asset)
        placeholders = ", ".join("?" for _ in asset)
        database.execute(
            f"INSERT INTO assets ({columns}) VALUES ({placeholders})", tuple(asset.values())
        )

    samples = {
        "imja-tsho": [4.20, 4.28, 4.34, 4.52],
        "tsho-rolpa": [3.82, 3.87, 3.91, 3.95],
        "longbasaba-lake": [3.92, 4.04, 4.23, 4.45],
        "evk2cnh-pyramid": [417.2, 418.1, 417.8],
        "nam-co-station": [414.0, 415.4, 416.2],
        "lukla-weather": [5.4],
        "khumbu-glacier": [0.42, 0.46],
        "ngo-zumpa-glacier": [0.21, 0.24],
        "rongbuk-glacier": [0.18, 0.20],
    }
    for slug, values in samples.items():
        asset = database.execute("SELECT * FROM assets WHERE slug = ?", (slug,)).fetchone()
        for index, value in enumerate(values):
            age = 6 + (len(values) - index - 1) * 24
            if slug == "lukla-weather":
                age = 72
            observed = now - timedelta(hours=age)
            metric = (
                "water_level_m" if asset["asset_type"] == "lake"
                else "glacier_velocity_m_day" if asset["asset_type"] == "glacier"
                else "co2_ppm" if asset["measurement_kind"] == "measured CO2"
                else "modeled_co2_ppm" if "modeled CO2" in asset["measurement_kind"]
                else "temperature_c"
            )
            database.execute(
                """INSERT INTO readings
                   (asset_id, metric, value, units, observed_at, available_at,
                    quality_status, source_url, is_synthetic, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'unverified', ?, 1, ?, ?)""",
                (
                    asset["id"], metric, value, asset["units"], observed.isoformat(),
                    (observed + timedelta(hours=2)).isoformat(), asset["source_url"],
                    json.dumps({"label": "SYNTHETIC DEMO DATA"}),
                    now.isoformat(),
                ),
            )
        database.execute(
            "UPDATE assets SET observation_time = ? WHERE id = ?",
            ((now - timedelta(hours=age)).isoformat(), asset["id"]),
        )

    from .services import evaluate_all_assets

    evaluate_all_assets(database, now=now)


@click.command("init-db")
@click.option("--no-seed", is_flag=True, help="Create tables without representative demo data.")
def init_db_command(no_seed):
    init_db(seed=not no_seed)
    click.echo("Initialized database" + (" without seed data." if no_seed else " with demo data."))


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)

    from .providers import refresh_command
    from .providers import discovery_command

    app.cli.add_command(refresh_command)
    app.cli.add_command(discovery_command)
    with app.app_context():
        init_db()
