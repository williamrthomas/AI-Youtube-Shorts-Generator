"""Generic brokerage / second-portal verification adapter (§11.3).

Verification prefers the brokerage that actually holds the listing. Resolution
order is canonical link, MLS search, address+brokerage search, address search
restricted to known brokerage domains, then a configured alternate portal.
Each step down the ladder lowers the verification confidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from mpvf.acquisition.dom import parse_html
from mpvf.config.settings import AcquisitionSettings
from mpvf.models.domain import ListingObservation, SourceHealth
from mpvf.normalization.listing import build_observation
from mpvf.observability.logging import get_logger

logger = get_logger("adapters.brokerage")

_JSON_LD = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.DOTALL | re.I
)
_MLS = re.compile(r"\bMLS\s*#?\s*[:\-]?\s*([A-Z0-9\-]{5,})", re.I)


@dataclass
class SourceDomains:
    """Operator-curated allow list of verification domains (config file)."""

    brokerages: list[str]
    portals: list[str]
    blocked: list[str]

    @classmethod
    def load(cls, path: Path | str) -> SourceDomains:
        path = Path(path)
        if not path.exists():
            return cls(brokerages=[], portals=[], blocked=[])
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            brokerages=list(payload.get("brokerages") or []),
            portals=list(payload.get("portals") or []),
            blocked=list(payload.get("blocked") or []),
        )

    def is_allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        if any(host.endswith(domain) for domain in self.blocked):
            return False
        allowed = self.brokerages + self.portals
        return not allowed or any(host.endswith(domain) for domain in allowed)


class BrokerageAdapter:
    """Fetches and parses a verification page over plain HTTP."""

    name = "brokerage"
    version = "1.0"

    def __init__(
        self,
        domains: SourceDomains | None = None,
        settings: AcquisitionSettings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.domains = domains or SourceDomains([], [], [])
        self.settings = settings or AcquisitionSettings()
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": self.settings.user_agent},
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    def healthcheck(self) -> SourceHealth:
        allowed = len(self.domains.brokerages) + len(self.domains.portals)
        return SourceHealth(
            name=self.name,
            available=True,
            detail=f"{allowed} configured verification domains",
        )

    # -- resolution -------------------------------------------------------
    def candidate_urls(self, observation: ListingObservation) -> list[tuple[str, float]]:
        """Return ``(url, confidence_weight)`` candidates in resolution order."""

        candidates: list[tuple[str, float]] = []
        canonical = observation.facts.get("canonical_url") or observation.facts.get("brokerage_url")
        if isinstance(canonical, str) and canonical.startswith("http"):
            candidates.append((canonical, 1.0))
        for key in ("verification_url", "listing_url"):
            value = getattr(observation, key, None) or observation.facts.get(key)
            if (
                isinstance(value, str)
                and value.startswith("http")
                and value != observation.source_url
            ):
                candidates.append((value, 0.85))
        return [(url, weight) for url, weight in candidates if self.domains.is_allowed(url)]

    def resolve(self, observation: ListingObservation) -> ListingObservation | None:
        for url, _weight in self.candidate_urls(observation):
            try:
                page = self.fetch(url)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "verification fetch failed",
                    extra={"adapter": self.name, "detail": {"url": url, "error": str(exc)}},
                )
                continue
            return self.parse(page, url)
        return None

    def fetch(self, url: str) -> str:
        if not self.domains.is_allowed(url):
            raise ValueError(f"verification domain not allowed: {url}")
        response = self._http().get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise ValueError(f"unexpected content type for {url}: {content_type}")
        return response.text

    # -- parsing ----------------------------------------------------------
    def parse(self, html: str, url: str) -> ListingObservation:
        fields = extract_jsonld_listing(html)
        document = parse_html(html)
        page_text = document.text()

        fields.setdefault("listing_url", url)
        fields.setdefault("state", "ME")
        if not fields.get("mls_number") and (match := _MLS.search(page_text)):
            fields["mls_number"] = match.group(1)
        if not fields.get("description_text"):
            description = document.select_one("meta[name=description]")
            if description is not None:
                fields["description_text"] = description.attr("content")
        if not fields.get("image_urls"):
            urls: list[str] = []
            for image in document.select("img[src]"):
                src = image.attr("src")
                if src.startswith("http") and src not in urls:
                    urls.append(src)
            fields["image_urls"] = urls[:60]
        observation = build_observation(self.name, fields, url)
        observation.facts["verification_url"] = url
        return observation

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


_LD_KEYS = {
    "name": "address",
    "streetAddress": "street",
    "addressLocality": "city",
    "addressRegion": "state",
    "postalCode": "postal_code",
    "price": "price",
    "numberOfRooms": "beds",
    "numberOfBathroomsTotal": "baths",
    "yearBuilt": "year_built",
    "description": "description_text",
}


def extract_jsonld_listing(html: str) -> dict[str, Any]:
    """Pull listing fields out of schema.org JSON-LD when a site publishes it."""

    fields: dict[str, Any] = {}
    for block in _JSON_LD.findall(html):
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            entries = graph if isinstance(graph, list) else [item]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                _flatten_ld(entry, fields)
    return fields


def _flatten_ld(entry: dict[str, Any], fields: dict[str, Any]) -> None:
    for key, target in _LD_KEYS.items():
        value = entry.get(key)
        if value is not None and target not in fields:
            fields[target] = value
    address = entry.get("address")
    if isinstance(address, dict):
        _flatten_ld(address, fields)
    offers = entry.get("offers")
    if isinstance(offers, dict):
        if offers.get("price") is not None:
            fields.setdefault("price", offers["price"])
        availability = str(offers.get("availability", ""))
        if availability:
            fields.setdefault("status", availability.rsplit("/", 1)[-1])
    photos = entry.get("photo") or entry.get("image")
    if isinstance(photos, str):
        fields.setdefault("image_urls", [photos])
    elif isinstance(photos, list):
        urls = [p for p in photos if isinstance(p, str)]
        if urls:
            fields.setdefault("image_urls", urls)
    broker = entry.get("provider") or entry.get("seller") or entry.get("broker")
    if isinstance(broker, dict) and broker.get("name"):
        fields.setdefault("brokerage_name", broker["name"])
