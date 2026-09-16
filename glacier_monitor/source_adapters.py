import ipaddress
import json
import socket
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MAX_SOURCE_BYTES = 2 * 1024 * 1024


class SourceAdapter(ABC):
    name = "base"

    @abstractmethod
    def fetch(self):
        """Return an iterable of normalized source-event dictionaries."""


def _event_list(document):
    if isinstance(document, list):
        events = document
    elif isinstance(document, dict) and isinstance(document.get("events"), list):
        events = document["events"]
    else:
        raise ValueError("source document must be an array or an object with an events array")
    if len(events) > 1000:
        raise ValueError("source document exceeds the 1000-event safety limit")
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("every source event must be a JSON object")
    return events


class JsonFileAdapter(SourceAdapter):
    name = "json-file"

    def __init__(self, path):
        self.path = Path(path)

    def fetch(self):
        if not self.path.is_file():
            raise ValueError(f"source file does not exist: {self.path}")
        if self.path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("source file exceeds the 2 MiB safety limit")
        return _event_list(json.loads(self.path.read_text(encoding="utf-8")))


def validate_public_https_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must be an HTTPS URL without embedded credentials")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ValueError("source URL hostname could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("source URL must resolve only to public IP addresses")
    return url


class HttpsJsonAdapter(SourceAdapter):
    name = "https-json"

    def __init__(self, url, timeout=15):
        self.url = validate_public_https_url(url)
        self.timeout = timeout

    def fetch(self):
        request = Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "HimalayaWatch/0.2"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_SOURCE_BYTES:
                raise ValueError("source response exceeds the 2 MiB safety limit")
            body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError("source response exceeds the 2 MiB safety limit")
        return _event_list(json.loads(body.decode("utf-8")))


ADAPTER_STATUS = {
    "json-file": "Implemented normalized JSON file ingestion.",
    "https-json": "Implemented bounded HTTPS JSON ingestion.",
    "nepal-dhm": "Requires a permitted machine-readable DHM endpoint and field mapping.",
    "icimod": "Requires a permitted machine-readable ICIMOD endpoint and field mapping.",
    "pmd-pakistan": "Requires a permitted machine-readable PMD endpoint and field mapping.",
    "india-himansh": "Requires a permitted machine-readable NCPOR endpoint and field mapping.",
}
