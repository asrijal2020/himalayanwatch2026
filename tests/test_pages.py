import pytest


@pytest.mark.parametrize("path", ["/", "/alerts", "/about", "/workbench"])
def test_pages_load(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert b"RESEARCH PROTOTYPE" in response.data


def test_missing_page(client):
    assert client.get("/missing").status_code == 404
    response = client.get("/api/missing")
    assert response.status_code == 404
    assert response.get_json()["error"]["message"] == "endpoint not found"


def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_alert_page_has_email_recipient_list(client):
    response = client.get("/alerts")
    assert b"Notify SMS and email recipients in parallel" in response.data
    assert b'value="email"' in response.data


def test_alert_page_marks_operator_key_required_when_configured(app):
    app.config["OPERATOR_API_KEY"] = "operator-secret"
    response = app.test_client().get("/alerts")
    assert response.status_code == 200
    assert b"Role authentication required" in response.data
    assert b'required aria-required="true"' in response.data


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_himalayawatch_safety_language(client):
    dashboard = client.get("/").data
    assert b"HimalayaWatch" in dashboard
    assert b"Human authorization required" in dashboard
    assert b"9 m" in dashboard

    about = client.get("/about").data
    assert b"Movement alone is not evidence" in about
    assert b"Missing or stale observations are not evidence of safety" in about


def test_integrated_workbench_summary(client):
    response = client.get("/api/workbench/summary")
    assert response.status_code == 200
    body = response.get_json()
    assert body["operational"] is False
    assert body["public_delivery_enabled"] is False
    assert body["tabs"] == [
        "overview",
        "evidence",
        "sources",
        "acquisition",
        "observations",
        "prediction",
        "synthetic",
    ]
    assert len(body["synthetic_scenarios"]) == 10
