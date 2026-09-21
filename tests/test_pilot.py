from datetime import datetime, timedelta, timezone


def event(event_id="pilot-river-1", **overrides):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "event_id": event_id,
        "provider": "approved-shadow-feed",
        "event_type": "sensor.reading",
        "contract_version": "himalayawatch.observation.v1",
        "asset": "syabrubesi-hydromet-reference",
        "metric": "river_level_m",
        "measurement_reference": "river stage relative to local gauge datum HW-SYB-01",
        "value": 5.4,
        "observed_at": (now - timedelta(minutes=5)).isoformat(),
        "available_at": (now - timedelta(minutes=2)).isoformat(),
        "quality_status": "validated",
        "uncertainty": {"plus_minus_m": 0.03, "confidence": 0.95},
        "source_url": "https://example.org/pilot/river/1",
        "is_synthetic": False,
    }
    payload.update(overrides)
    return payload


def test_shadow_pilot_page_and_readiness_load(client):
    assert client.get("/pilot").status_code == 200
    readiness = client.get("/api/pilot-readiness")
    assert readiness.status_code == 200
    payload = readiness.get_json()
    assert payload["mode"] == "shadow_pilot"
    assert payload["public_warning_enabled"] is False
    assert payload["overall_state"] == "not_ready"
    assert any(item["freshness_limit_hours"] == 0.5 for item in payload["coverage"])


def test_source_event_creates_versioned_evidence_receipt(client):
    response = client.post("/api/source-events", json=event())
    assert response.status_code == 201
    evidence_id = response.get_json()["data"]["evidence_id"]
    evidence = client.get("/api/evidence").get_json()["data"][0]
    assert evidence["evidence_id"] == evidence_id
    assert evidence["contract_version"] == "himalayawatch.observation.v1"
    assert evidence["measurement_reference"].startswith("river stage")
    assert len(evidence["payload_sha256"]) == 64
    assert evidence["uncertainty"]["plus_minus_m"] == 0.03
    assert evidence["review_status"] == "pending"


def test_unknown_contract_version_is_rejected(client):
    response = client.post(
        "/api/source-events",
        json=event(contract_version="himalayawatch.observation.v2"),
    )
    assert response.status_code == 400
    assert "contract_version" in response.get_json()["error"]["message"]


def test_scientific_review_and_model_lineage(app):
    app.config.update(SCIENTIST_API_KEY="scientist-secret", OPERATOR_API_KEY="")
    client = app.test_client()
    created = client.post("/api/source-events", json=event())
    evidence = client.get("/api/evidence").get_json()["data"][0]

    unauthorized = client.post(
        f"/api/evidence/{evidence['id']}/review",
        json={"decision": "approved", "reviewed_by": "Glaciologist"},
    )
    assert unauthorized.status_code == 401

    headers = {"X-API-Key": "scientist-secret"}
    approved = client.post(
        f"/api/evidence/{evidence['id']}/review",
        headers=headers,
        json={
            "decision": "approved",
            "reviewed_by": "Rasuwa scientific duty officer",
            "note": "Gauge datum and timing receipt checked.",
        },
    )
    assert approved.status_code == 200

    model = client.post(
        "/api/model-runs",
        headers=headers,
        json={
            "run_id": "ridge-river-shadow-001",
            "model_name": "Ridge river baseline",
            "model_version": "0.3.0",
            "hazard_type": "flood",
            "catchment": "rasuwa-trishuli",
            "input_evidence_ids": [created.get_json()["data"]["evidence_id"]],
            "created_by": "Scientific workbench",
            "output": {"horizon_minutes": 30, "stage_change_m": 0.4},
            "uncertainty": {"interval_m": [0.1, 0.8]},
            "artifact_schema": "river-ridge-0.1",
            "dataset_sha256": "a" * 64,
            "protocol_sha256": "b" * 64,
            "feature_schema": [
                "latest_m", "rate_m_per_hour", "age_hours", "range_m"
            ],
            "station_id": "HW-SYB-01",
            "reference_datum": "provider local gauge datum",
            "fit_available_at": datetime.now(timezone.utc).isoformat(),
            "training_domain": {"latest_m": {"min": 0.2, "max": 7.0}},
        },
    )
    assert model.status_code == 201
    run = model.get_json()["data"]
    assert run["status"] == "candidate"
    assert len(run["input_hash"]) == 64
    assert run["artifact_schema"] == "river-ridge-0.1"
    assert run["feature_schema"][0] == "latest_m"
    assert run["research_only"] is True
    accepted = client.post(
        f"/api/model-runs/{run['id']}/review",
        headers=headers,
        json={
            "decision": "accepted",
            "reviewed_by": "Independent hydrology reviewer",
            "note": "Accepted for shadow scoring only.",
        },
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["data"]["status"] == "accepted"


def test_model_run_is_withheld_for_unapproved_evidence(client):
    created = client.post("/api/source-events", json=event())
    response = client.post(
        "/api/model-runs",
        json={
            "run_id": "withheld-001",
            "model_name": "Experimental model",
            "model_version": "0.1",
            "hazard_type": "flood",
            "catchment": "rasuwa-trishuli",
            "input_evidence_ids": [created.get_json()["data"]["evidence_id"]],
            "created_by": "Test workbench",
        },
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["status"] == "withheld"
    assert "scientific approval" in response.get_json()["data"]["withholding_reason"]
