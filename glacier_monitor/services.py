import json
from datetime import datetime, timezone


ALLOWED_METRICS = {
    "lake": {"water_level_m": "m"},
    "glacier": {"glacier_velocity_m_day": "m/day"},
    "station": {
        "co2_ppm": "ppm",
        "modeled_co2_ppm": "ppm-equivalent",
        "temperature_c": "degC",
        "precipitation_mm": "mm",
        "river_level_m": "m",
    },
}

DECISION_FRESHNESS_HOURS = {
    "river_level_m": 0.5,
    "precipitation_mm": 1,
    "temperature_c": 3,
    "water_level_m": 6,
    "co2_ppm": 24,
    "modeled_co2_ppm": 24,
    "glacier_velocity_m_day": 168,
}


def freshness_limit_hours(asset, metric):
    return min(float(asset["stale_after_hours"]), DECISION_FRESHNESS_HOURS[metric])


def parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def validate_reading(payload, asset):
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    missing = [key for key in ("metric", "value", "observed_at") if key not in payload]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    metric = payload["metric"]
    expected = ALLOWED_METRICS[asset["asset_type"]]
    if metric not in expected:
        raise ValueError(
            f"metric must be one of: {', '.join(sorted(expected))} for this asset"
        )
    if isinstance(payload["value"], bool) or not isinstance(payload["value"], (int, float)):
        raise ValueError("value must be a number")
    if not -10000 <= float(payload["value"]) <= 100000:
        raise ValueError("value is outside the accepted range")
    observed_at = parse_timestamp(payload["observed_at"])
    now = datetime.now(timezone.utc)
    if observed_at > now.replace(microsecond=0):
        raise ValueError("observed_at cannot be in the future")
    available_at = parse_timestamp(payload.get("available_at", now.isoformat()))
    if available_at < observed_at:
        raise ValueError("available_at cannot be earlier than observed_at")
    if available_at > now:
        raise ValueError("available_at cannot be in the future")
    quality_status = payload.get("quality_status", "unverified")
    if quality_status not in {"validated", "provisional", "unverified", "rejected"}:
        raise ValueError(
            "quality_status must be one of: validated, provisional, unverified, rejected"
        )
    source_url = payload.get("source_url")
    if source_url and not source_url.startswith(("https://", "http://")):
        raise ValueError("source_url must use http or https")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    if "is_synthetic" in payload and not isinstance(payload["is_synthetic"], bool):
        raise ValueError("is_synthetic must be true or false")
    return {
        "metric": metric,
        "value": float(payload["value"]),
        "units": expected[metric],
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "quality_status": quality_status,
        "source_url": source_url or asset["source_url"],
        "is_synthetic": 1 if payload.get("is_synthetic", False) else 0,
        "metadata_json": json.dumps(metadata),
        "created_at": now.replace(microsecond=0).isoformat(),
    }


def create_alert(
    database, asset_id, alert_type, severity, reason, now, evidence_reading_id=None
):
    active = database.execute(
        """SELECT id FROM alerts
           WHERE asset_id = ? AND alert_type = ? AND reason = ? AND acknowledged_at IS NULL""",
        (asset_id, alert_type, reason),
    ).fetchone()
    if active:
        if evidence_reading_id is not None:
            database.execute(
                "UPDATE alerts SET evidence_reading_id = ? WHERE id = ?",
                (evidence_reading_id, active["id"]),
            )
        return active["id"]
    cursor = database.execute(
        """INSERT INTO alerts
           (asset_id, alert_type, severity, reason, created_at, evidence_reading_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            alert_type,
            severity,
            reason,
            now.isoformat(),
            evidence_reading_id,
        ),
    )
    return cursor.lastrowid


def evaluate_asset(database, asset, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    readings = database.execute(
        "SELECT * FROM readings WHERE asset_id = ? ORDER BY observed_at DESC LIMIT 2",
        (asset["id"],),
    ).fetchall()
    if not readings:
        return
    latest = readings[0]
    if latest["quality_status"] == "rejected":
        return
    observed = parse_timestamp(latest["observed_at"])

    if asset["asset_type"] == "lake":
        value = latest["value"]
        if asset["critical_threshold"] is not None and value >= asset["critical_threshold"]:
            create_alert(
                database, asset["id"], "threshold_exceedance", "critical",
                f"Water level {value:.2f} m exceeds critical threshold "
                f"{asset['critical_threshold']:.2f} m.", now, latest["id"],
            )
        elif asset["warning_threshold"] is not None and value >= asset["warning_threshold"]:
            create_alert(
                database, asset["id"], "threshold_exceedance", "warning",
                f"Water level {value:.2f} m exceeds warning threshold "
                f"{asset['warning_threshold']:.2f} m.", now, latest["id"],
            )
        if len(readings) == 2:
            previous = readings[1]
            hours = (
                observed - parse_timestamp(previous["observed_at"])
            ).total_seconds() / 3600
            if hours > 0:
                daily_rate = (value - previous["value"]) / hours * 24
                if (
                    asset["rapid_rise_threshold"] is not None
                    and daily_rate >= asset["rapid_rise_threshold"]
                ):
                    create_alert(
                        database, asset["id"], "rapid_rise", "critical",
                        f"Water level rose {daily_rate:.2f} m/day; rapid-rise threshold is "
                        f"{asset['rapid_rise_threshold']:.2f} m/day.", now, latest["id"],
                    )

    if asset["asset_type"] == "glacier":
        threshold = asset["abnormal_movement_threshold"]
        if threshold is not None and latest["value"] >= threshold:
            create_alert(
                database, asset["id"], "abnormal_movement", "critical",
                f"Glacier velocity {latest['value']:.2f} m/day exceeds "
                f"{threshold:.2f} m/day.", now, latest["id"],
            )

    if asset["asset_type"] == "station":
        if latest["metric"] == "river_level_m":
            value = latest["value"]
            if asset["critical_threshold"] is not None and value >= asset["critical_threshold"]:
                create_alert(
                    database, asset["id"], "river_threshold_exceedance", "critical",
                    f"River level {value:.2f} m exceeds critical threshold "
                    f"{asset['critical_threshold']:.2f} m.", now, latest["id"],
                )
            elif asset["warning_threshold"] is not None and value >= asset["warning_threshold"]:
                create_alert(
                    database, asset["id"], "river_threshold_exceedance", "warning",
                    f"River level {value:.2f} m exceeds warning threshold "
                    f"{asset['warning_threshold']:.2f} m.", now, latest["id"],
                )
            previous = database.execute(
                """SELECT * FROM readings
                   WHERE asset_id = ? AND metric = 'river_level_m' AND id != ?
                   ORDER BY observed_at DESC LIMIT 1""",
                (asset["id"], latest["id"]),
            ).fetchone()
            if previous:
                hours = (
                    observed - parse_timestamp(previous["observed_at"])
                ).total_seconds() / 3600
                if hours > 0:
                    hourly_rate = (value - previous["value"]) / hours
                    if (
                        asset["rapid_rise_threshold"] is not None
                        and hourly_rate >= asset["rapid_rise_threshold"]
                    ):
                        create_alert(
                            database, asset["id"], "rapid_river_rise", "critical",
                            f"River level rose {hourly_rate:.2f} m/hour; rapid-rise "
                            f"threshold is {asset['rapid_rise_threshold']:.2f} m/hour.",
                            now, latest["id"],
                        )
        age_hours = (now - observed).total_seconds() / 3600
        if asset["operational_status"] == "offline" or age_hours > asset["stale_after_hours"]:
            severity = "critical" if asset["operational_status"] == "offline" else "warning"
            create_alert(
                database, asset["id"], "sensor_stale", severity,
                f"Sensor is {asset['operational_status']}; latest observation is "
                f"{age_hours:.1f} hours old.", now, latest["id"],
            )


def evaluate_all_assets(database, now=None):
    for asset in database.execute("SELECT * FROM assets").fetchall():
        evaluate_asset(database, asset, now=now)
    database.commit()


def serialize_asset(database, asset, now=None):
    now = now or datetime.now(timezone.utc)
    latest = database.execute(
        "SELECT * FROM readings WHERE asset_id = ? ORDER BY observed_at DESC LIMIT 1",
        (asset["id"],),
    ).fetchone()
    result = dict(asset)
    if latest:
        reading = dict(latest)
        reading["is_synthetic"] = bool(reading["is_synthetic"])
        reading["metadata"] = json.loads(reading.pop("metadata_json"))
        result["latest_reading"] = reading
        age_hours = (
            now - parse_timestamp(latest["observed_at"])
        ).total_seconds() / 3600
        freshness_limit = freshness_limit_hours(asset, latest["metric"])
        result["freshness"] = {
            "age_hours": round(max(age_hours, 0), 1),
            "limit_hours": freshness_limit,
            "state": "fresh" if age_hours <= freshness_limit else "stale",
        }
        if reading["quality_status"] == "rejected":
            result["decision_readiness"] = {
                "state": "withheld",
                "reason": "Latest evidence failed quality checks.",
            }
        elif age_hours > freshness_limit:
            result["decision_readiness"] = {
                "state": "withheld",
                "reason": (
                    f"Latest evidence exceeds the {freshness_limit:g}-hour "
                    "decision-support freshness limit; missing evidence is not evidence of safety."
                ),
            }
        elif reading["is_synthetic"]:
            result["decision_readiness"] = {
                "state": "research_only",
                "reason": "Synthetic evidence supports interface testing, not operational forecasts.",
            }
        else:
            result["decision_readiness"] = {
                "state": "human_review",
                "reason": "Evidence is timely but still requires authorized scientific review.",
            }
    else:
        result["latest_reading"] = None
        result["freshness"] = {"age_hours": None, "state": "unknown"}
        result["decision_readiness"] = {
            "state": "withheld",
            "reason": "No observations available; safety cannot be inferred.",
        }
    return result
