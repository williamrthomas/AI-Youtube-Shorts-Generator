"""Zillow discovery adapter (§11.2).

Browser work is confined to this file; parsing lives in ``parser.py`` so CI can
exercise the logic against fixtures without a browser. On a challenge the
adapter stops and reports the source unavailable — it never tries to get past
one (FR-015, §3.2).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mpvf.acquisition.browser import BrowserSession, BrowserUnavailable
from mpvf.adapters.base import AccessChallenge, SelectorFailure
from mpvf.adapters.zillow import parser
from mpvf.adapters.zillow import selectors_v1 as sel
from mpvf.config.settings import AcquisitionSettings
from mpvf.models.domain import (
    DiscoveryQuery,
    ListingObservation,
    RawListingCard,
    RawListingPage,
    SourceHealth,
    utcnow,
)
from mpvf.observability.logging import get_logger

logger = get_logger("adapters.zillow")


class ZillowAdapter:
    """Playwright-driven discovery against operator-configured search URLs."""

    name = "zillow"
    version = f"1.0/{sel.VERSION}"

    def __init__(
        self,
        settings: AcquisitionSettings | None = None,
        debug_dir: Path | str | None = None,
        session: BrowserSession | None = None,
    ) -> None:
        self.settings = settings or AcquisitionSettings()
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self._session = session

    # -- browser ----------------------------------------------------------
    def _browser(self) -> BrowserSession:
        if self._session is None:
            self._session = BrowserSession(self.settings)
        return self._session

    def healthcheck(self) -> SourceHealth:
        try:
            self._browser().ensure_available()
        except BrowserUnavailable as exc:
            return SourceHealth(name=self.name, available=False, detail=str(exc))
        return SourceHealth(name=self.name, available=True, detail="browser runtime present")

    # -- discovery --------------------------------------------------------
    def discover(self, query: DiscoveryQuery) -> list[RawListingCard]:
        cards: list[RawListingCard] = []
        browser = self._browser()
        for url in query.search_urls:
            for page_number in range(1, query.max_pages + 1):
                page_url = _paginate(url, page_number)
                try:
                    html = browser.fetch_html(page_url)
                except BrowserUnavailable as exc:
                    raise AccessChallenge(self.name, f"browser unavailable: {exc}") from exc

                marker = parser.detect_challenge(html)
                if marker:
                    self._save_debug(page_url, html, reason=f"challenge-{marker}")
                    raise AccessChallenge(self.name, marker)

                try:
                    page_cards = parser.extract_cards(
                        html, page_url, limit=query.max_cards_per_page
                    )
                except SelectorFailure as failure:
                    path = self._save_debug(page_url, html, reason="selector-failure")
                    raise SelectorFailure(self.name, failure.selector, str(path)) from failure

                self._save_debug(page_url, html, reason=f"page-{page_number}")
                cards.extend(page_cards)
                logger.info(
                    "zillow page parsed",
                    extra={
                        "adapter": self.name,
                        "detail": {"url": page_url, "cards": len(page_cards)},
                    },
                )
                if len(page_cards) < 5:  # last page of results
                    break
                time.sleep(self.settings.request_delay_seconds)
        return cards

    def fetch_listing(self, url: str) -> RawListingPage:
        browser = self._browser()
        html = browser.fetch_html(url)
        marker = parser.detect_challenge(html)
        if marker:
            self._save_debug(url, html, reason=f"challenge-{marker}")
            raise AccessChallenge(self.name, marker)
        path = self._save_debug(url, html, reason="listing")
        time.sleep(self.settings.request_delay_seconds)
        return RawListingPage(
            source=self.name,
            url=url,
            html=html,
            fields={"raw_path": str(path) if path else None},
            captured_at=utcnow(),
        )

    # -- parsing ----------------------------------------------------------
    def parse_card(self, raw: RawListingCard) -> ListingObservation:
        return parser.parse_card(raw)

    def parse_listing(self, raw: RawListingPage) -> ListingObservation:
        return parser.parse_listing(raw)

    # -- debug artifacts (FR-014) -----------------------------------------
    def _save_debug(self, url: str, html: str, reason: str) -> Path | None:
        if not self.debug_dir:
            return None
        from mpvf.pipeline.artifacts import safe_filename

        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%H%M%S")
        name = safe_filename(f"{reason}-{stamp}-{url.split('/')[-1] or 'index'}")[:100]
        path = self.debug_dir / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


def _paginate(url: str, page_number: int) -> str:
    """Append Zillow's page token to a configured search URL."""

    if page_number <= 1:
        return url
    separator = "&" if "?" in url else "?"
    if url.rstrip("/").endswith(f"/{page_number - 1}_p"):
        return url
    base = url.rstrip("/")
    if "?" in base:
        return f"{base}{separator}page={page_number}"
    return f"{base}/{page_number}_p/"


def fields_summary(observation: ListingObservation) -> dict[str, Any]:
    """Compact dictionary used in logs and the candidate dashboard."""

    return {
        "address": observation.raw_address,
        "price": observation.price,
        "status": observation.status,
        "beds": observation.beds,
        "baths": observation.baths,
        "sqft": observation.square_feet,
        "images": observation.usable_image_count(),
    }
