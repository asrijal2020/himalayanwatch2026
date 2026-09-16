# HimalayaWatch Azure event and notification architecture

This implementation translates the supplied reference diagram into a deployable first
slice. It prioritizes deterministic ingestion, evidence quality, human authorization,
auditing, and reliable communication before adding AI-assisted interpretation.

## Implemented now

| Diagram layer | Implementation |
|---|---|
| Satellite / drone products | Existing Sentinel STAC metadata adapter and glacier imagery catalog |
| Weather / river / sensor feeds | Normalized `sensor.reading` event contract; bounded JSON file and HTTPS adapters |
| Field reports | Same event contract can accept approved gateway exports; free-text reports are not automatically converted into alerts |
| Source adapters and validation | `source_adapters.py`, envelope validation, metric/asset validation, provenance, observation and availability times |
| Azure Event Hubs | Optional producer and bounded consumer with a dedicated consumer group |
| Raw/archive storage | Immutable-by-name JSON Blob archive before event evaluation |
| Event processing | Idempotent `source_events`, persisted readings, deterministic threshold and freshness rules |
| Approval and audit | Separate acknowledgement and authorization states, operator identity/note, immutable delivery records |
| Communication and action | Parallel Azure Communication Services SMS/email delivery and signed HTTPS webhooks |

Deployment secrets are stored directly in the Container Apps and Container Apps
Jobs secret stores from secure Bicep parameters. Azure Key Vault is not deployed
or required. Event Hubs, Blob Storage, ACR, and Azure Communication Services
continue to use managed identities instead of connection strings.

## Deliberately outside the automatic notification path

- Microsoft Fabric Real-Time Intelligence, KQL anomaly candidates, Azure AI Search, and
  Foundry agents can support investigation and evidence retrieval later.
- They must not independently convert ambiguous satellite, model, or free-text evidence
  into a public warning.
- Any future model result must preserve source time, availability time, model version,
  uncertainty, missing-data state, and reproducible inputs.

## Runtime flow

1. An approved adapter emits a stable event ID and traceable source URL.
2. The API either processes it directly or publishes it to Event Hubs.
3. The consumer archives the raw event to Blob Storage.
4. Schema, asset, timestamp, metric, quality, and range checks run.
5. The accepted reading is stored idempotently and deterministic rules generate research alerts.
6. Scientists review the evidence. Acknowledgement alone does not notify anyone.
7. An authorized operator can approve only validated, non-synthetic evidence.
8. Matching recipient subscriptions are written to the durable outbox.
9. A worker sends matching SMS, email, and signed-webhook deliveries concurrently and records attempts, provider IDs, failures, and final status.

## Production gaps requiring an owner decision

- Stable, permitted machine-readable contracts and mappings for Nepal DHM, ICIMOD,
  Pakistan PMD, India HIMANSH, and local field gateways.
- Official authority roles, authentication, RBAC, escalation policy, multilingual message
  templates, recipient consent, telecom registration, and 24/7 operational ownership.
- A production database and active-active/disaster-recovery design. SQLite remains suitable
  only for the current prototype and local exercises.
- Validated thresholds, hydrologic/physical models, historical replay, false-alarm targets,
  and end-to-end warning-time exercises.
- Azure region, resource group, subscription, naming, budget, and data-residency decisions.

Until those items are approved, the system is decision support and private exercise
infrastructure—not an official emergency-warning service.
