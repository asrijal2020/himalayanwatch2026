import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from glacier_monitor.workbench import import_bundle, import_native_backup


def receipt(**overrides):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "receipt_id": "receipt-001",
        "source_id": "copernicus-dataspace-stac",
        "source_item_id": "S2-item-001",
        "source_url": "https://stac.dataspace.copernicus.eu/",
        "source_role": "observation",
        "observed_start": (now - timedelta(hours=3)).isoformat(),
        "observed_end": (now - timedelta(hours=2)).isoformat(),
        "source_available_at": (now - timedelta(hours=1)).isoformat(),
        "availability_basis": "provider_published",
        "locally_first_seen_at": (now - timedelta(minutes=50)).isoformat(),
        "processed_at": (now - timedelta(minutes=45)).isoformat(),
        "rights_status": "approved",
        "rights_ref": "https://dataspace.copernicus.eu/terms-and-conditions",
        "quality_status": "validated",
        "quality_review_ref": "review:scientist-001",
        "crs": "EPSG:4326",
        "units": None,
        "resolution": "catalog metadata",
        "content_sha256": hashlib.sha256(b"catalog-response").hexdigest(),
        "byte_count": 1234,
        "algorithm_id": "bounded-stac-discovery",
        "algorithm_version": "1.0",
        "code_commit": "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124",
        "limitations": ["Metadata eligibility only; no pixels were acquired."],
        "eligibility_state": "eligible_as_of",
    }
    payload.update(overrides)
    return payload


def river_request(**overrides):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    observations = []
    for index, value in enumerate((2.0, 2.1, 2.25, 2.45)):
        observed = now - timedelta(hours=3.25 - index)
        observations.append({
            "observed_at": observed.isoformat(),
            "available_at": (observed + timedelta(minutes=5)).isoformat(),
            "availability_basis": "provider_published",
            "quality_status": "validated",
            "value": value,
        })
    payload = {
        "station_id": "HW-SYB-01",
        "reference_datum": "provider local gauge datum",
        "units": "m",
        "issue_time": now.isoformat(),
        "horizon_hours": 1,
        "lookback_hours": 12,
        "max_gap_hours": 2,
        "max_age_hours": 1.1,
        "max_rate_m_per_hour": 0.5,
        "mode": "shadow",
        "observations": observations,
    }
    payload.update(overrides)
    return payload


def test_integration_catalog_discloses_license_and_api_boundaries(client):
    response = client.get("/api/integrations/catalog")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["repository_license"] == "not_declared"
    assert payload["code_copied"] is False
    by_id = {item["id"]: item for item in payload["data"]}
    assert by_id["river"]["status"] == "metadata_sample_only"
    assert by_id["foundry"]["status"] == "disabled"
    assert "hazard detection" in by_id["sentinel2"]["limitation"]
    assert {"sentinel1", "hkh_glaciers", "hkh_lakes", "emsr927_bidur"} <= set(by_id)


def test_research_archive_record_detail(client):
    listing = client.get("/api/integrations/research-archive").get_json()["data"]
    if not listing:
        return
    response = client.get(f"/api/integrations/research-archive/{listing[0]['id']}")
    assert response.status_code == 200
    record = response.get_json()["data"]
    assert record["id"] == listing[0]["id"]
    assert isinstance(record["result"], dict)
    assert record["research_only"] is True


def test_acquisition_specification_and_eligibility(client):
    specification = client.get("/api/acquisition/specification")
    assert specification.status_code == 200
    payload = specification.get_json()
    assert payload["case_id"] == "rasuwa-kyirong-2026"
    assert payload["public_delivery_enabled"] is False
    assert len(payload["sources"]) >= 20

    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    response = client.post(
        "/api/acquisition/eligibility",
        json={
            "cutoff": cutoff.isoformat(),
            "purpose": "as_of_input",
            "max_age_hours": 24,
            "evidence": {
                "source_id": "emsr927_syapru",
                "observed_end": (cutoff - timedelta(hours=1)).isoformat(),
                "available_at": (cutoff - timedelta(minutes=30)).isoformat(),
                "availability_basis": "first_seen",
                "rights_confirmed": True,
                "quality": "accepted",
            },
        },
    )
    assert response.status_code == 200
    result = response.get_json()["data"]
    assert result["eligible"] is False
    assert "reference_or_context_only" in result["reasons"]


def test_receipt_requires_rights_and_proven_availability_for_as_of(client):
    rejected = client.post(
        "/api/acquisition-receipts",
        json=receipt(rights_status="pending"),
    )
    assert rejected.status_code == 400
    assert "approved rights" in rejected.get_json()["error"]["message"]

    created = client.post("/api/acquisition-receipts", json=receipt())
    assert created.status_code == 201
    stored = created.get_json()["data"]
    assert stored["source_role"] == "observation"
    assert stored["eligibility_state"] == "eligible_as_of"
    assert stored["limitations"][0].startswith("Metadata eligibility")
    assert client.get("/api/acquisition-receipts").get_json()["count"] == 1


def test_reference_receipt_cannot_be_as_of_input(client):
    response = client.post(
        "/api/acquisition-receipts",
        json=receipt(source_role="reference"),
    )
    assert response.status_code == 400
    assert "observation/forcing" in response.get_json()["error"]["message"]


def test_river_baseline_returns_persistence_and_capped_trend(client):
    response = client.post("/api/river-baselines", json=river_request())
    assert response.status_code == 200
    result = response.get_json()["data"]
    assert result["status"] == "candidate"
    assert result["research_only"] is True
    assert result["forecasts"]["persistence_m"] == 2.45
    assert result["forecasts"]["capped_rate_m_per_hour"] <= 0.5
    assert result["uncertainty"] is None
    assert len(result["input_hash"]) == 64


def test_river_baseline_withholds_unproven_as_of_availability(client):
    payload = river_request()
    for observation in payload["observations"]:
        observation["availability_basis"] = "unknown"
        observation["available_at"] = None
    response = client.post("/api/river-baselines", json=payload)
    assert response.status_code == 200
    result = response.get_json()["data"]
    assert result["status"] == "withheld"
    assert result["eligible_count"] == 0
    assert {
        item["reason"] for item in result["exclusions"]
    } == {"availability_unproven"}


def test_retrospective_baseline_does_not_use_future_measurements(client):
    payload = river_request(mode="retrospective")
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    payload["observations"].append({
        "observed_at": future.isoformat(),
        "available_at": future.isoformat(),
        "availability_basis": "provider_published",
        "quality_status": "validated",
        "value": 99,
    })
    response = client.post("/api/river-baselines", json=payload)
    result = response.get_json()["data"]
    assert result["status"] == "candidate"
    assert any(item["reason"] == "future_measurement" for item in result["exclusions"])
    assert result["forecasts"]["persistence_m"] == 2.45


def test_bounded_workbench_bundle_import(database, client):
    bundle = {
        "schema": "himalayawatch.workbench-import.v1",
        "source_repository": "https://github.com/harineupane_microsoft/NepalFloodProject",
        "source_commit": "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124",
        "receipts": [receipt()],
        "observation_comparisons": [{
            "comparison_id": "comparison-001",
            "source_run_id": "outline-run-001",
            "input_hash": hashlib.sha256(b"comparison").hexdigest(),
            "before_observed_start": "2026-06-01T00:00:00+00:00",
            "before_observed_end": "2026-06-01T01:00:00+00:00",
            "after_observed_start": "2026-09-01T00:00:00+00:00",
            "after_observed_end": "2026-09-01T01:00:00+00:00",
            "common_coverage_fraction": 0.82,
            "object_count": 1,
            "review_status": "pending",
            "metrics": [{
                "object_id": "lake-001",
                "kind": "lake",
                "before_area_m2": 100,
                "after_area_m2": 115,
                "net_area_m2": 15
            }],
            "limitations": [
                "Area change does not establish lake depth, dam stability, or flood probability."
            ],
        }],
        "model_runs": [{
            "run_id": "imported-ridge-001",
            "model_name": "River Ridge research model",
            "model_version": "0.1",
            "hazard_type": "flood",
            "catchment": "rasuwa-trishuli",
            "input_hash": hashlib.sha256(b"model-input").hexdigest(),
            "input_evidence_ids": [],
            "output": {"horizon_hours": 1, "forecast_m": 3.2},
            "uncertainty": {"residual_radius_m": None},
            "status": "withheld",
            "withholding_reason": "No permitted real training archive.",
            "created_at": "2026-09-16T00:00:00+00:00",
            "created_by": "NepalFloodProject export",
            "artifact_schema": "river-ridge-0.1",
            "dataset_sha256": "a" * 64,
            "protocol_sha256": "b" * 64,
            "feature_schema": [
                "latest_m", "rate_m_per_hour", "age_hours", "range_m"
            ],
            "station_id": "HW-SYB-01",
            "reference_datum": "provider local gauge datum",
            "fit_available_at": "2026-09-15T00:00:00+00:00",
            "training_domain": {"latest_m": {"min": 0.5, "max": 6.0}},
        }],
    }
    summary = import_bundle(database, bundle)
    assert summary == {
        "receipts": 1,
        "observation_comparisons": 1,
        "model_runs": 1,
    }
    comparisons = client.get("/api/observation-comparisons").get_json()
    assert comparisons["count"] == 1
    assert comparisons["data"][0]["metrics"][0]["net_area_m2"] == 15
    assert "flood probability" in comparisons["data"][0]["limitations"][0]
    model = client.get("/api/model-runs").get_json()["data"][0]
    assert model["artifact_schema"] == "river-ridge-0.1"
    assert model["feature_schema"][-1] == "range_m"
    assert model["research_only"] is True


def test_workbench_import_cli(app, tmp_path):
    bundle = {
        "schema": "himalayawatch.workbench-import.v1",
        "source_repository": "https://github.com/harineupane_microsoft/NepalFloodProject",
        "source_commit": "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124",
        "receipts": [receipt(receipt_id="cli-receipt")],
        "observation_comparisons": [],
        "model_runs": [],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    result = app.test_cli_runner().invoke(
        args=["import-workbench-export", str(path)]
    )
    assert result.exit_code == 0
    assert "1 receipt(s)" in result.output


def test_bundle_rejects_wrong_repository(database):
    try:
        import_bundle(
            database,
            {
                "schema": "himalayawatch.workbench-import.v1",
                "source_repository": "https://example.org/untrusted",
                "receipts": [],
                "observation_comparisons": [],
                "model_runs": [],
            },
        )
    except ValueError as exc:
        assert "source_repository" in str(exc)
    else:
        raise AssertionError("untrusted bundle should be rejected")


def test_failed_bundle_import_is_atomic(database):
    bundle = {
        "schema": "himalayawatch.workbench-import.v1",
        "source_repository": "https://github.com/harineupane_microsoft/NepalFloodProject",
        "source_commit": "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124",
        "receipts": [receipt(receipt_id="atomic-receipt")],
        "observation_comparisons": [{
            "comparison_id": "invalid-comparison",
            "input_hash": "not-a-sha256",
            "before_observed_start": "2026-06-01T00:00:00+00:00",
            "before_observed_end": "2026-06-01T01:00:00+00:00",
            "after_observed_start": "2026-09-01T00:00:00+00:00",
            "after_observed_end": "2026-09-01T01:00:00+00:00",
            "object_count": 1,
            "review_status": "pending",
            "metrics": [],
            "limitations": [],
        }],
        "model_runs": [],
    }
    try:
        import_bundle(database, bundle)
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("invalid bundle should be rejected")
    count = database.execute(
        "SELECT COUNT(*) FROM acquisition_receipts WHERE receipt_id = ?",
        ("atomic-receipt",),
    ).fetchone()[0]
    assert count == 0


def test_native_backup_imports_metadata_and_research_records(database, tmp_path):
    root = tmp_path / "backup"
    local = root / "prototype" / ".local"
    (local / "acquisition-checks").mkdir(parents=True)
    (local / "observation-checks").mkdir()
    backup_manifest = {
        "FormatVersion": 1,
        "CreatedUtc": "2026-09-14T10:22:59Z",
        "GitHead": "a7aaa50bcf40e3f264b9aeb76eb4017a0146a124",
    }
    (root / "BACKUP-MANIFEST.json").write_text(
        json.dumps(backup_manifest), encoding="utf-8"
    )
    record = {
        "id": "catalogue-record",
        "recorded_at": "2026-09-07T17:00:32Z",
        "kind": "catalogue",
        "result": {
            "query": {"collection": "sentinel-2-l2a"},
            "items": [],
            "complete": False,
        },
    }
    records_hash = hashlib.sha256(
        json.dumps(
            [record], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    (local / "acquisition-checks" / "rasuwa-acquisition-manifest.json").write_text(
        json.dumps({
            "schema_version": "0.2",
            "case_id": "rasuwa-kyirong-2026",
            "records": [record],
            "records_sha256": records_hash,
        }),
        encoding="utf-8",
    )
    comparison_manifest = {
        "before": {"evidence": {
            "observed_start": "2026-08-20T00:00:00Z",
            "observed_end": "2026-08-20T00:01:00Z",
        }},
        "after": {"evidence": {
            "observed_start": "2026-08-27T00:00:00Z",
            "observed_end": "2026-08-27T00:01:00Z",
        }},
    }
    comparison_hash = hashlib.sha256(
        json.dumps(
            comparison_manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    (local / "observation-checks" / "observation-report.json").write_text(
        json.dumps({
            "kind": "outline_comparison",
            "generated_at": "2026-09-08T10:18:36Z",
            "input_sha256": comparison_hash,
            "input_manifest": comparison_manifest,
            "coverage_fraction": 0.9,
            "objects": [{"object_id": "lake-demo", "kind": "lake"}],
            "limitations": ["Synthetic geometry; no warning skill."],
        }),
        encoding="utf-8",
    )
    for database_name, include_run in (
        ("workbench.sqlite3", True),
        ("predictions.sqlite3", False),
        ("synthetic.sqlite3", False),
    ):
        connection = sqlite3.connect(local / database_name)
        connection.execute(
            """CREATE TABLE runs (
               id TEXT PRIMARY KEY, created_at TEXT NOT NULL, input_hash TEXT NOT NULL,
               input_json TEXT NOT NULL, result_json TEXT NOT NULL)"""
        )
        if include_run:
            input_document = {"mode": "synthetic_exercise"}
            input_hash = hashlib.sha256(
                json.dumps(
                    input_document, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (
                    "run-1",
                    "2026-09-11T14:06:31Z",
                    input_hash,
                    json.dumps(input_document),
                    json.dumps({
                        "mode": "synthetic_exercise",
                        "title": "Synthetic research run",
                        "issue_time": "2026-09-01T12:00:00Z",
                    }),
                ),
            )
        connection.commit()
        connection.close()

    summary = import_native_backup(database, root)
    assert summary["acquisition_receipts"] == 1
    assert summary["observation_comparisons"] == 1
    assert summary["research_records"] == 1
    assert summary["synthetic_records"] == 1
    assert summary["raw_binary_files_copied"] is False
    assert import_native_backup(database, root)["already_imported"] is True
