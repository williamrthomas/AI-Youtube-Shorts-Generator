"""Fixture adapter: reads listings from local JSON or saved HTML.

This is a first-class adapter, not a test double. It lets an operator run the
whole pipeline offline (a saved-search export, a captured page set, a manual
shortlist) and it is what the CI integration tests drive (§19.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mpvf.models.domain import (
    DiscoveryQuery,
    ListingObservation,
    RawListingCard,
    RawListingPage,
    SourceHealth,
    utcnow,
)
from mpvf.normalization.listing import build_observation


class FixtureAdapter:
    """Serves observations from a directory of ``*.json`` / ``*.html`` files."""

    name = "fixture"
    version = "1.0"

    def __init__(self, directory: Path | str, html_parser: Any | None = None) -> None:
        self.directory = Path(directory)
        self.html_parser = html_parser

    def healthcheck(self) -> SourceHealth:
        exists = self.directory.exists()
        count = len(list(self.directory.glob("*.json"))) if exists else 0
        return SourceHealth(
            name=self.name,
            available=exists,
            detail=f"{count} json fixtures in {self.directory}" if exists else "directory missing",
        )

    def _payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                payloads.extend(item for item in data if isinstance(item, dict))
            elif isinstance(data, dict):
                if isinstance(data.get("listings"), list):
                    payloads.extend(item for item in data["listings"] if isinstance(item, dict))
                else:
                    payloads.append(data)
        return payloads

    def discover(self, query: DiscoveryQuery) -> list[RawListingCard]:
        cards: list[RawListingCard] = []
        for payload in self._payloads():
            if query.state and str(payload.get("state", query.state)).upper() != query.state:
                continue
            cards.append(
                RawListingCard(
                    source=self.name,
                    source_url=str(payload.get("listing_url") or self.directory),
                    fields=payload,
                    captured_at=utcnow(),
                )
            )
        return cards[: query.max_pages * query.max_cards_per_page]

    def fetch_listing(self, url: str) -> RawListingPage:
        for payload in self._payloads():
            if str(payload.get("listing_url")) == url:
                return RawListingPage(source=self.name, url=url, fields=payload)
        candidate = self.directory / f"{url.rstrip('/').split('/')[-1]}.html"
        if candidate.exists():
            return RawListingPage(
                source=self.name, url=url, html=candidate.read_text(encoding="utf-8")
            )
        raise FileNotFoundError(f"no fixture for {url}")

    def parse_card(self, raw: RawListingCard) -> ListingObservation:
        return build_observation(self.name, dict(raw.fields), raw.source_url)

    def parse_listing(self, raw: RawListingPage) -> ListingObservation:
        if raw.fields:
            return build_observation(self.name, dict(raw.fields), raw.url)
        if self.html_parser is not None:
            return self.html_parser(raw.html, raw.url)
        raise ValueError(f"fixture page {raw.url} has neither fields nor an html parser")

    # Verification-adapter surface: resolve by matching MLS number or address.
    def resolve(self, observation: ListingObservation) -> ListingObservation | None:
        for payload in self._payloads():
            candidate = build_observation(
                self.name, dict(payload), str(payload.get("listing_url") or "")
            )
            if observation.mls_number and candidate.mls_number == observation.mls_number:
                return candidate
            if (
                observation.raw_address
                and candidate.raw_address
                and observation.raw_address.strip().lower() == candidate.raw_address.strip().lower()
            ):
                return candidate
        return None
