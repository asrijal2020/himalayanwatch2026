import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click
from flask import current_app
from flask.cli import with_appcontext

from .db import get_db


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "snowfall",
    "wind_speed_10m",
)


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
        tzinfo=timezone.utc, microsecond=0
    ).isoformat()


def fetch_open_meteo(latitude, longitude, past_days=14, timeout=20):
    if not 1 <= past_days <= 31:
        raise ValueError("past_days must be between 1 and 31")
    query = urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(HOURLY_FIELDS),
        "past_days": past_days,
        "forecast_days": 1,
        "timezone": "UTC",
    })
    source_url = f"{OPEN_METEO_URL}?{query}"
    request = Request(
        source_url,
        headers={"User-Agent": "HimalayaWatch/0.4 research-decision-support"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Open-Meteo returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Open-Meteo request failed: {exc}") from exc
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise RuntimeError("Open-Meteo response did not contain hourly weather")
    count = len(hourly["time"])
    for field in HOURLY_FIELDS:
        if not isinstance(hourly.get(field), list) or len(hourly[field]) != count:
            raise RuntimeError(f"Open-Meteo response has invalid {field} values")
    return {
        "source_url": source_url,
        "grid_latitude": float(payload["latitude"]),
        "grid_longitude": float(payload["longitude"]),
        "grid_elevation_m": payload.get("elevation"),
        "hourly": hourly,
    }


def store_weather(database, asset, result, fetched_at=None):
    fetched_at = fetched_at or datetime.now(timezone.utc).replace(microsecond=0)
    hourly = result["hourly"]
    inserted = 0
    updated = 0
    accepted = 0
    database.execute(
        """DELETE FROM weather_observations
           WHERE asset_id = ? AND provider = 'Open-Meteo' AND observed_at > ?""",
        (asset["id"], fetched_at.isoformat()),
    )
    for index, observed_at in enumerate(hourly["time"]):
        normalized_time = _utc(observed_at)
        if datetime.fromisoformat(normalized_time) > fetched_at:
            continue
        accepted += 1
        values = [
            hourly["temperature_2m"][index],
            hourly["relative_humidity_2m"][index],
            hourly["precipitation"][index],
            hourly["rain"][index],
            hourly["snowfall"][index],
            hourly["wind_speed_10m"][index],
        ]
        existing = database.execute(
            """SELECT id FROM weather_observations
               WHERE asset_id = ? AND observed_at = ? AND provider = 'Open-Meteo'""",
            (asset["id"], normalized_time),
        ).fetchone()
        database.execute(
            """INSERT INTO weather_observations
               (asset_id, observed_at, temperature_c, relative_humidity_pct,
                precipitation_mm, rain_mm, snowfall_cm, wind_speed_kmh,
                grid_latitude, grid_longitude, grid_elevation_m, provider,
                source_url, data_classification, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Open-Meteo', ?, 'modeled', ?)
               ON CONFLICT(asset_id, observed_at, provider) DO UPDATE SET
                 temperature_c = excluded.temperature_c,
                 relative_humidity_pct = excluded.relative_humidity_pct,
                 precipitation_mm = excluded.precipitation_mm,
                 rain_mm = excluded.rain_mm,
                 snowfall_cm = excluded.snowfall_cm,
                 wind_speed_kmh = excluded.wind_speed_kmh,
                 grid_latitude = excluded.grid_latitude,
                 grid_longitude = excluded.grid_longitude,
                 grid_elevation_m = excluded.grid_elevation_m,
                 source_url = excluded.source_url,
                 fetched_at = excluded.fetched_at""",
            (
                asset["id"], normalized_time, *values,
                result["grid_latitude"], result["grid_longitude"],
                result["grid_elevation_m"], result["source_url"], fetched_at.isoformat(),
            ),
        )
        if existing:
            updated += 1
        else:
            inserted += 1
    database.commit()
    return {"inserted": inserted, "updated": updated, "total": accepted}


def refresh_asset_weather(database, asset, past_days=14):
    result = fetch_open_meteo(
        asset["latitude"],
        asset["longitude"],
        past_days=past_days,
        timeout=current_app.config["WEATHER_HTTP_TIMEOUT_SECONDS"],
    )
    summary = store_weather(database, asset, result)
    return {**summary, "asset": asset["slug"]}


def serialize_weather(database, asset, days=14):
    if not 1 <= days <= 31:
        raise ValueError("days must be between 1 and 31")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = [
        dict(row)
        for row in database.execute(
            """SELECT observed_at, temperature_c, relative_humidity_pct,
                      precipitation_mm, rain_mm, snowfall_cm, wind_speed_kmh,
                      grid_latitude, grid_longitude, grid_elevation_m, provider,
                      source_url, data_classification, fetched_at
               FROM weather_observations
               WHERE asset_id = ? AND observed_at >= ?
               ORDER BY observed_at""",
            (asset["id"], cutoff.replace(microsecond=0).isoformat()),
        )
    ]
    daily = defaultdict(list)
    for row in rows:
        daily[row["observed_at"][:10]].append(row)
    summaries = []
    for date, observations in daily.items():
        temperatures = [row["temperature_c"] for row in observations if row["temperature_c"] is not None]
        humidities = [row["relative_humidity_pct"] for row in observations if row["relative_humidity_pct"] is not None]
        winds = [row["wind_speed_kmh"] for row in observations if row["wind_speed_kmh"] is not None]
        summaries.append({
            "date": date,
            "temperature_min_c": min(temperatures) if temperatures else None,
            "temperature_max_c": max(temperatures) if temperatures else None,
            "temperature_mean_c": sum(temperatures) / len(temperatures) if temperatures else None,
            "relative_humidity_mean_pct": sum(humidities) / len(humidities) if humidities else None,
            "precipitation_total_mm": sum(row["precipitation_mm"] or 0 for row in observations),
            "snowfall_total_cm": sum(row["snowfall_cm"] or 0 for row in observations),
            "wind_speed_max_kmh": max(winds) if winds else None,
            "hours": len(observations),
        })
    latest = rows[-1] if rows else None
    grid = rows[0] if rows else None
    return {
        "asset": {"slug": asset["slug"], "name": asset["name"]},
        "period_days": days,
        "period_start": rows[0]["observed_at"] if rows else None,
        "period_end": latest["observed_at"] if latest else None,
        "count": len(rows),
        "latest": latest,
        "daily": summaries,
        "hourly": rows,
        "collection_basis": {
            "provider": "Open-Meteo",
            "sampling": "hourly",
            "timezone": "UTC",
            "aggregation": "Daily mean temperature from available hourly samples",
            "window": f"Rolling previous {days} days through the latest stored hour",
            "grid_latitude": grid["grid_latitude"] if grid else None,
            "grid_longitude": grid["grid_longitude"] if grid else None,
            "grid_elevation_m": grid["grid_elevation_m"] if grid else None,
            "partial_days_possible": True,
        },
        "data_classification": "modeled",
        "interpretation": (
            "Open-Meteo values are gridded weather-model estimates, not measurements "
            "from the monitored asset. Validate against partner stations before model training."
        ),
    }


@click.command("refresh-weather")
@click.option("--asset", "asset_slug", help="Refresh one asset slug; defaults to all assets.")
@click.option("--past-days", default=14, show_default=True, type=click.IntRange(1, 31))
@with_appcontext
def refresh_weather_command(asset_slug, past_days):
    """Refresh credential-free gridded weather history from Open-Meteo."""
    database = get_db()
    if asset_slug:
        assets = database.execute("SELECT * FROM assets WHERE slug = ?", (asset_slug,)).fetchall()
        if not assets:
            raise click.ClickException(f"Unknown asset: {asset_slug}")
    else:
        assets = database.execute("SELECT * FROM assets ORDER BY slug").fetchall()
    inserted = updated = failures = 0
    for asset in assets:
        try:
            result = refresh_asset_weather(database, asset, past_days=past_days)
            inserted += result["inserted"]
            updated += result["updated"]
            click.echo(f"{asset['slug']}: {result['total']} hourly records")
        except RuntimeError as exc:
            failures += 1
            click.echo(f"{asset['slug']}: failed: {exc}", err=True)
    if failures:
        raise click.ClickException(
            f"Weather refresh completed with {failures} failed asset(s); "
            f"{inserted} inserted and {updated} updated."
        )
    click.echo(f"Weather refresh complete: {inserted} inserted, {updated} updated.")


def init_app(app):
    app.cli.add_command(refresh_weather_command)
