"""Source-adapter contract (§11.1).

Adapters are the only place that knows about a specific website. Everything
downstream consumes ``ListingObservation``. An adapter that meets a login wall,
CAPTCHA or automated-access challenge reports it and stops (FR-015) — it never
attempts to work around one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mpvf.models.domain import (
    DiscoveryQuery,
    ListingObservation,
    RawListingCard,
    RawListingPage,
    SourceHealth,
)


class AccessChallenge(RuntimeError):
    """Raised when a source presents a challenge we must not bypass."""

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: access challenge detected ({detail})")
        self.source = source
        self.detail = detail


class SelectorFailure(RuntimeError):
    """Raised when the page structure no longer matches the adapter (FR-014)."""

    def __init__(self, source: str, selector: str, artifact_path: str | None = None) -> None:
        super().__init__(f"{source}: selector produced nothing: {selector}")
        self.source = source
        self.selector = selector
        self.artifact_path = artifact_path


@runtime_checkable
class ListingSourceAdapter(Protocol):
    name: str
    version: str

    def healthcheck(self) -> SourceHealth: ...

    def discover(self, query: DiscoveryQuery) -> list[RawListingCard]: ...

    def fetch_listing(self, url: str) -> RawListingPage: ...

    def parse_card(self, raw: RawListingCard) -> ListingObservation: ...

    def parse_listing(self, raw: RawListingPage) -> ListingObservation: ...


@runtime_checkable
class VerificationAdapter(Protocol):
    """Resolves an authoritative second read of a listing (§11.3)."""

    name: str
    version: str

    def healthcheck(self) -> SourceHealth: ...

    def resolve(
        self,
        observation: ListingObservation,
    ) -> ListingObservation | None: ...
