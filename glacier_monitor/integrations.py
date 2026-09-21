import hashlib
import json
import re
from datetime import datetime, timezone

import click
from flask import current_app

from .db import get_db
from .services import evaluate_asset, validate_reading
from .source_adapters import HttpsJsonAdapter, JsonFileAdapter

CONTRACT_VERSION = "himalayawatch.observation.v1"
MEASUREMENT_REFERENCES = {
    "water_level_m": "lake water level relative to provider-defined local datum",
    "river_level_m": "river stage relative to provider-defined local gauge datum",
    "glacier_velocity_m_day": "surface velocity; not displacement or collapse probability",
    "precipitation_mm": "accumulated precipitation over provider-defined interval",
    "temperature_c": "air temperature at provider-defined sensor height",
    "co2_ppm": "direct atmospheric mole fraction measurement",
    "modeled_co2_ppm": "modeled atmospheric estimate; not direct measurement",
}


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def validate_event_envelope(payload, provider=None):
    if not isinstance(payload, dict):
        raise ValueError("event must be a JSON object")
    required = (
        "event_id",
        "asset",
        "metric",
        "value",
        "observed_at",
        "available_at",
        "quality_status",
        "source_url",
        "is_synthetic",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing required event field(s): {', '.join(missing)}")
    event_id = payload["event_id"]
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
        raise ValueError("event_id must be a string between 1 and 200 characters")
    source_provider = provider or payload.get("provider")
    if not isinstance(source_provider, str) or not 1 <= len(source_provider) <= 100:
        raise ValueError("provider must be a string between 1 and 100 characters")
    event_type = payload.get("event_type", "sensor.reading")
    if event_type != "sensor.reading":
        raise ValueError("event_type must be sensor.reading")
    normalized = dict(payload)
    normalized["provider"] = source_provider
    normalized["event_type"] = event_type
    contract_version = normalized.get("contract_version", CONTRACT_VERSION)
    if contract_version != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")
    normalized["contract_version"] = contract_version
    uncertainty = normalized.get("uncertainty", {})
    if not isinstance(uncertainty, dict):
        raise ValueError("uncertainty must be a JSON object")
    normalized["uncertainty"] = uncertainty
    measurement_reference = normalized.get("measurement_reference")
    if measurement_reference is not None and (
        not isinstance(measurement_reference, str)
        or not 1 <= len(measurement_reference.strip()) <= 300
    ):
        raise ValueError("measurement_reference must be a string of at most 300 characters")
    return normalized


def ingest_event(database, payload, provider=None):
    event = validate_event_envelope(payload, provider=provider)
    existing = database.execute(
        "SELECT id, status, reading_id FROM source_events WHERE event_id = ?",
        (event["event_id"],),
    ).fetchone()
    if existing:
        evidence = database.execute(
            "SELECT evidence_id FROM evidence_records WHERE source_event_id = ?",
            (existing["id"],),
        ).fetchone()
        return {
            "event_id": event["event_id"],
            "evidence_id": evidence["evidence_id"] if evidence else None,
            "status": existing["status"],
            "reading_id": existing["reading_id"],
            "duplicate": True,
        }

    received_at = utcnow().isoformat()
    cursor = database.execute(
        """INSERT INTO source_events
           (event_id, provider, asset_slug, event_type, payload_json, received_at, status)
           VALUES (?, ?, ?, ?, ?, ?, 'received')""",
        (
            event["event_id"],
            event["provider"],
            event["asset"],
            event["event_type"],
            json.dumps(event, sort_keys=True),
            received_at,
        ),
    )
    source_event_id = cursor.lastrowid
    try:
        asset = database.execute(
            "SELECT * FROM assets WHERE slug = ?", (event["asset"],)
        ).fetchone()
        if not asset:
            raise ValueError("asset not found")
        reading_payload = {
            key: event[key]
            for key in (
                "metric",
                "value",
                "observed_at",
                "available_at",
                "quality_status",
                "source_url",
                "is_synthetic",
                "metadata",
            )
            if key in event
        }
        metadata = dict(reading_payload.get("metadata") or {})
        metadata.update(
            {"source_event_id": event["event_id"], "provider": event["provider"]}
        )
        reading_payload["metadata"] = metadata
        reading = validate_reading(reading_payload, asset)
        reading_cursor = database.execute(
            """INSERT INTO readings
               (asset_id, metric, value, units, observed_at, available_at, quality_status,
                source_url, is_synthetic, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset["id"], *reading.values()),
        )
        canonical_payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
        payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        measurement_reference = (
            event.get("measurement_reference")
            or MEASUREMENT_REFERENCES[event["metric"]]
        )
        evidence_id = f"evidence:{event['event_id']}"
        quality_checks = {
            "asset_resolved": True,
            "metric_allowed_for_asset": True,
            "units_normalized": True,
            "observation_precedes_availability": True,
            "provenance_present": True,
            "synthetic": bool(event["is_synthetic"]),
        }
        database.execute(
            """INSERT INTO evidence_records
               (evidence_id, source_event_id, reading_id, asset_id, contract_version,
                metric, measurement_reference, observed_at, available_at, quality_status,
                uncertainty_json, provenance_url, is_synthetic, payload_sha256,
                quality_checks_json, review_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                evidence_id,
                source_event_id,
                reading_cursor.lastrowid,
                asset["id"],
                event["contract_version"],
                event["metric"],
                measurement_reference,
                reading["observed_at"],
                reading["available_at"],
                reading["quality_status"],
                json.dumps(event["uncertainty"], sort_keys=True),
                reading["source_url"],
                reading["is_synthetic"],
                payload_sha256,
                json.dumps(quality_checks, sort_keys=True),
                utcnow().isoformat(),
            ),
        )
        database.execute(
            """UPDATE assets SET observation_time = ?,
               operational_status = CASE WHEN asset_type = 'station'
                                    THEN 'online' ELSE operational_status END
               WHERE id = ?""",
            (reading["observed_at"], asset["id"]),
        )
        updated_asset = database.execute(
            "SELECT * FROM assets WHERE id = ?", (asset["id"],)
        ).fetchone()
        evaluate_asset(database, updated_asset)
        processed_at = utcnow().isoformat()
        database.execute(
            """UPDATE source_events
               SET status = 'processed', processed_at = ?, reading_id = ?
               WHERE id = ?""",
            (processed_at, reading_cursor.lastrowid, source_event_id),
        )
        database.commit()
        return {
            "event_id": event["event_id"],
            "evidence_id": evidence_id,
            "status": "processed",
            "reading_id": reading_cursor.lastrowid,
            "duplicate": False,
        }
    except ValueError as exc:
        database.execute(
            """UPDATE source_events
               SET status = 'rejected', processed_at = ?, error_message = ?
               WHERE id = ?""",
            (utcnow().isoformat(), str(exc), source_event_id),
        )
        database.commit()
        raise


def publish_event(event):
    connection_string = current_app.config.get("AZURE_EVENTHUB_CONNECTION_STRING")
    fully_qualified_namespace = current_app.config.get("AZURE_EVENTHUB_NAMESPACE")
    eventhub_name = current_app.config.get("AZURE_EVENTHUB_NAME")
    if not eventhub_name:
        raise RuntimeError("AZURE_EVENTHUB_NAME is not configured")
    try:
        from azure.eventhub import EventData, EventHubProducerClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("Azure Event Hubs dependencies are not installed") from exc

    if connection_string:
        producer = EventHubProducerClient.from_connection_string(
            connection_string, eventhub_name=eventhub_name
        )
    elif fully_qualified_namespace:
        producer = EventHubProducerClient(
            fully_qualified_namespace=fully_qualified_namespace,
            eventhub_name=eventhub_name,
            credential=DefaultAzureCredential(),
        )
    else:
        raise RuntimeError(
            "configure AZURE_EVENTHUB_NAMESPACE for managed identity or "
            "AZURE_EVENTHUB_CONNECTION_STRING"
        )
    with producer:
        batch = producer.create_batch()
        batch.add(EventData(json.dumps(event)))
        producer.send_batch(batch)


def archive_event(event):
    account_url = current_app.config.get("AZURE_ARCHIVE_STORAGE_ACCOUNT_URL")
    connection_string = current_app.config.get("AZURE_STORAGE_CONNECTION_STRING")
    if not account_url and not connection_string:
        return None
    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError as exc:
        raise RuntimeError("Azure Blob Storage dependencies are not installed") from exc
    if connection_string:
        service = BlobServiceClient.from_connection_string(connection_string)
    else:
        service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    container = service.get_container_client(
        current_app.config.get("AZURE_ARCHIVE_CONTAINER", "raw-events")
    )
    safe_event_id = re.sub(r"[^A-Za-z0-9._-]", "_", event["event_id"])
    observed_day = event["observed_at"][:10]
    blob_name = f"{event['provider']}/{observed_day}/{safe_event_id}.json"
    try:
        container.upload_blob(
            name=blob_name,
            data=json.dumps(event, sort_keys=True).encode("utf-8"),
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json"),
        )
    except ResourceExistsError:
        pass
    return blob_name


def checkpoint_store():
    account_url = current_app.config.get("AZURE_CHECKPOINT_STORAGE_ACCOUNT_URL")
    connection_string = current_app.config.get("AZURE_STORAGE_CONNECTION_STRING")
    container_name = current_app.config.get(
        "AZURE_CHECKPOINT_CONTAINER", "eventhub-checkpoints"
    )
    if not account_url and not connection_string:
        return None
    try:
        from azure.eventhub.extensions.checkpointstoreblob import BlobCheckpointStore
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("Azure Event Hubs checkpoint dependencies are not installed") from exc
    if connection_string:
        return BlobCheckpointStore.from_connection_string(
            connection_string, container_name
        )
    return BlobCheckpointStore(
        blob_account_url=account_url,
        container_name=container_name,
        credential=DefaultAzureCredential(),
    )


def process_event(event, provider=None):
    mode = current_app.config.get("INGESTION_MODE", "direct")
    normalized = validate_event_envelope(event, provider=provider)
    if mode == "eventhub":
        publish_event(normalized)
        return {"event_id": normalized["event_id"], "status": "accepted", "duplicate": False}
    if mode != "direct":
        raise RuntimeError("INGESTION_MODE must be direct or eventhub")
    return ingest_event(get_db(), normalized)


@click.command("ingest-source")
@click.option("--provider", type=click.Choice(["json-file", "https-json"]), required=True)
@click.option("--path", type=click.Path(exists=True, dir_okay=False))
@click.option("--url")
def ingest_source_command(provider, path, url):
    """Ingest or publish normalized events from a bounded source adapter."""
    if provider == "json-file":
        if not path:
            raise click.ClickException("--path is required for json-file")
        adapter = JsonFileAdapter(path)
    else:
        if not url:
            raise click.ClickException("--url is required for https-json")
        adapter = HttpsJsonAdapter(url)
    processed = 0
    duplicates = 0
    for event in adapter.fetch():
        try:
            result = process_event(event, provider=provider)
        except (ValueError, RuntimeError) as exc:
            raise click.ClickException(f"event ingestion failed: {exc}") from exc
        processed += 1
        duplicates += int(result.get("duplicate", False))
    click.echo(f"{provider}: accepted {processed} event(s); duplicates {duplicates}.")


@click.command("consume-eventhub")
@click.option("--max-events", default=100, show_default=True, type=click.IntRange(1, 10000))
@click.option("--wait-seconds", default=10, show_default=True, type=click.IntRange(1, 300))
def consume_eventhub_command(max_events, wait_seconds):
    """Consume a bounded Event Hubs batch and persist events idempotently."""
    connection_string = current_app.config.get("AZURE_EVENTHUB_CONNECTION_STRING")
    fully_qualified_namespace = current_app.config.get("AZURE_EVENTHUB_NAMESPACE")
    eventhub_name = current_app.config.get("AZURE_EVENTHUB_NAME")
    consumer_group = current_app.config.get("AZURE_EVENTHUB_CONSUMER_GROUP", "$Default")
    if not eventhub_name:
        raise click.ClickException("AZURE_EVENTHUB_NAME is not configured")
    try:
        from azure.eventhub import EventHubConsumerClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise click.ClickException("Azure Event Hubs dependencies are not installed") from exc
    store = checkpoint_store()
    if connection_string:
        consumer = EventHubConsumerClient.from_connection_string(
            connection_string,
            consumer_group=consumer_group,
            eventhub_name=eventhub_name,
            checkpoint_store=store,
        )
    elif fully_qualified_namespace:
        consumer = EventHubConsumerClient(
            fully_qualified_namespace=fully_qualified_namespace,
            eventhub_name=eventhub_name,
            consumer_group=consumer_group,
            credential=DefaultAzureCredential(),
            checkpoint_store=store,
        )
    else:
        raise click.ClickException(
            "configure AZURE_EVENTHUB_NAMESPACE or AZURE_EVENTHUB_CONNECTION_STRING"
        )
    processed = 0

    def on_batch(partition_context, events):
        nonlocal processed
        for received in events:
            if processed >= max_events:
                break
            try:
                event = validate_event_envelope(json.loads(received.body_as_str()))
                archive_event(event)
                ingest_event(get_db(), event)
                processed += 1
            except (ValueError, json.JSONDecodeError) as exc:
                current_app.logger.error("Rejected Event Hubs message: %s", exc)
        if events and store:
            partition_context.update_checkpoint(events[-1])
        consumer.close()

    with consumer:
        consumer.receive_batch(
            on_event_batch=on_batch,
            max_batch_size=max_events,
            max_wait_time=wait_seconds,
            starting_position="-1",
        )
    click.echo(f"Processed {processed} Event Hubs event(s).")


def init_app(app):
    app.cli.add_command(ingest_source_command)
    app.cli.add_command(consume_eventhub_command)
