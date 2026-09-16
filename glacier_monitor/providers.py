from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from flask import current_app

from .db import get_db
from .services import evaluate_all_assets


class ProviderAdapter(ABC):
    name = "base"
    live = False

    @abstractmethod
    def refresh(self, database):
        """Return a result summary after refreshing provider data."""


class DemoProvider(ProviderAdapter):
    name = "demo"

    def refresh(self, database):
        evaluate_all_assets(database)
        return {"provider": self.name, "updated": 0, "message": "Alert rules re-evaluated."}


ADAPTERS = {
    "demo": DemoProvider,
    "glims_nsidc": None,
    "nasa_its_live": None,
    "icimod": None,
    "nepal_dhm": None,
    "copernicus_sentinel": None,
    "noaa_gml_cams": None,
}

DISCOVERY_ENDPOINTS = {
    "Nepal BIPAD": "https://bipadportal.gov.np/api/v1/",
    "Copernicus Sentinel": "https://documentation.dataspace.copernicus.eu/APIs.html",
}


@click.command("refresh-data")
@click.option("--provider", default="demo", show_default=True)
def refresh_command(provider):
    """Refresh data through a pluggable provider adapter."""
    if provider not in ADAPTERS:
        raise click.ClickException(f"Unknown provider: {provider}")
    adapter_type = ADAPTERS[provider]
    if adapter_type is None:
        raise click.ClickException(
            f"{provider} is an adapter placeholder; configure and implement its upstream contract."
        )
    result = adapter_type().refresh(get_db())
    click.echo(
        f"[{datetime.now(timezone.utc).isoformat()}] "
        f"{result['provider']}: {result['message']}"
    )


@click.command("check-discovery")
@click.option("--timeout", default=8, show_default=True, type=click.IntRange(1, 30))
def discovery_command(timeout):
    """Check public catalog reachability without downloading hazard products."""
    database = get_db()
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for provider, url in DISCOVERY_ENDPOINTS.items():
        status = "unreachable"
        try:
            request = Request(url, headers={"User-Agent": "HimalayaWatch/0.1 research-prototype"})
            with urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 400:
                    status = "reachable"
        except HTTPError as exc:
            status = "reachable" if exc.code in {401, 403} else "unreachable"
        except (URLError, TimeoutError):
            status = "unreachable"
        database.execute(
            "UPDATE discovery_checks SET status = ?, checked_at = ? WHERE provider = ?",
            (status, checked_at, provider),
        )
        click.echo(f"{provider}: {status}")
    database.commit()
