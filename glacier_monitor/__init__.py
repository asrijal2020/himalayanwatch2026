import os
from pathlib import Path

from flask import Flask

from . import db
from . import imagery
from . import integrations
from . import notifications
from . import pilot
from . import workbench
from . import weather
from . import earth_engine
from . import icimod
from .routes import bp


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "glacier_monitor.db")
        ),
        WRITE_API_KEY=os.environ.get("WRITE_API_KEY", ""),
        INGEST_API_KEY=os.environ.get("INGEST_API_KEY", ""),
        OPERATOR_API_KEY=os.environ.get("OPERATOR_API_KEY", ""),
        SCIENTIST_API_KEY=os.environ.get("SCIENTIST_API_KEY", ""),
        AUTHORITY_API_KEY=os.environ.get("AUTHORITY_API_KEY", ""),
        SENTINEL_STAC_URL=os.environ.get(
            "SENTINEL_STAC_URL", "https://stac.dataspace.copernicus.eu/v1/search"
        ),
        INGESTION_MODE=os.environ.get("INGESTION_MODE", "direct"),
        AZURE_EVENTHUB_NAMESPACE=os.environ.get("AZURE_EVENTHUB_NAMESPACE", ""),
        AZURE_EVENTHUB_NAME=os.environ.get("AZURE_EVENTHUB_NAME", ""),
        AZURE_EVENTHUB_CONSUMER_GROUP=os.environ.get(
            "AZURE_EVENTHUB_CONSUMER_GROUP", "himalayawatch-processors"
        ),
        AZURE_EVENTHUB_CONNECTION_STRING=os.environ.get(
            "AZURE_EVENTHUB_CONNECTION_STRING", ""
        ),
        AZURE_STORAGE_CONNECTION_STRING=os.environ.get(
            "AZURE_STORAGE_CONNECTION_STRING", ""
        ),
        AZURE_ARCHIVE_STORAGE_ACCOUNT_URL=os.environ.get(
            "AZURE_ARCHIVE_STORAGE_ACCOUNT_URL", ""
        ),
        AZURE_ARCHIVE_CONTAINER=os.environ.get(
            "AZURE_ARCHIVE_CONTAINER", "raw-events"
        ),
        AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL=os.environ.get(
            "AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL", ""
        ),
        AZURE_CHECKPOINT_CONTAINER=os.environ.get(
            "AZURE_CHECKPOINT_CONTAINER", "eventhub-checkpoints"
        ),
        AZURE_COMMUNICATION_ENDPOINT=os.environ.get("AZURE_COMMUNICATION_ENDPOINT", ""),
        AZURE_COMMUNICATION_CONNECTION_STRING=os.environ.get(
            "AZURE_COMMUNICATION_CONNECTION_STRING", ""
        ),
        AZURE_SMS_SENDER=os.environ.get("AZURE_SMS_SENDER", ""),
        AZURE_EMAIL_SENDER=os.environ.get("AZURE_EMAIL_SENDER", ""),
        NOTIFICATION_WEBHOOK_SECRET=os.environ.get("NOTIFICATION_WEBHOOK_SECRET", ""),
        NOTIFICATION_TIMEOUT_SECONDS=int(
            os.environ.get("NOTIFICATION_TIMEOUT_SECONDS", "10")
        ),
        NOTIFICATION_MAX_ATTEMPTS=int(
            os.environ.get("NOTIFICATION_MAX_ATTEMPTS", "5")
        ),
        NOTIFICATION_WORKERS=int(os.environ.get("NOTIFICATION_WORKERS", "4")),
        WEATHER_HTTP_TIMEOUT_SECONDS=int(os.environ.get("WEATHER_HTTP_TIMEOUT_SECONDS", "20")),
        GEE_PROJECT=os.environ.get("GEE_PROJECT", ""),
        GEE_SERVICE_ACCOUNT=os.environ.get("GEE_SERVICE_ACCOUNT", ""),
        GEE_PRIVATE_KEY_FILE=os.environ.get("GEE_PRIVATE_KEY_FILE", ""),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.register_blueprint(bp)
    app.register_blueprint(imagery.bp)
    app.register_blueprint(pilot.bp)
    app.register_blueprint(workbench.bp)
    app.register_blueprint(earth_engine.bp)
    app.register_blueprint(icimod.bp)
    db.init_app(app)
    imagery.init_app(app)
    integrations.init_app(app)
    notifications.init_app(app)
    workbench.init_app(app)
    weather.init_app(app)
    earth_engine.init_app(app)
    icimod.init_app(app)

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://*.tile.openstreetmap.org "
            "https://*.dataspace.copernicus.eu; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "script-src 'self' https://unpkg.com; connect-src 'self'"
        )
        return response

    return app
