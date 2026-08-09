"""Source capture and reliability tiering (§12.1, FR-061, FR-062, FR-068).

Wikipedia and Wikimedia Commons are read through the MediaWiki API rather than
scraped, and Commons media keeps creator, license and attribution so the
description can credit it correctly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from mpvf.acquisition.dom import parse_html
from mpvf.models.domain import ReliabilityTier, SourceRecord, utcnow
from mpvf.observability.logging import get_logger

logger = get_logger("research.sources")

TIER_A_SUFFIXES = (
    ".gov",
    "maine.gov",
    "nps.gov",
    "loc.gov",
    "npgallery.nps.gov",
    "mainelegislature.org",
)
TIER_B_SUFFIXES = (
    ".edu",
    "mainehistory.org",
    "mainememory.net",
    "pressherald.com",
    "bangordailynews.com",
    "themainemonitor.org",
    "newenglandhistoricalsociety.com",
    "historicnewengland.org",
    "library.org",
    ".lib.me.us",
)
TIER_C_SUFFIXES = (
    "wikipedia.org",
    "commons.wikimedia.org",
    "visitmaine.com",
    "britannica.com",
    "nationalregisterofhistoricplaces.com",
)

USER_AGENT = "MPVF/0.1 (Maine Property Video Factory; research; contact: operator)"


def classify_tier(url: str) -> ReliabilityTier:
    """Assign the §12.1 reliability tier from the host."""

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "D"
    if any(host.endswith(suffix) or suffix in host for suffix in TIER_A_SUFFIXES):
        return "A"
    if any(host.endswith(suffix) or suffix in host for suffix in TIER_B_SUFFIXES):
        return "B"
    if any(host.endswith(suffix) or suffix in host for suffix in TIER_C_SUFFIXES):
        return "C"
    return "D"


@dataclass
class CapturedSource:
    record: SourceRecord
    text: str

    @property
    def usable_for_facts(self) -> bool:
        """Tier D is a discovery lead only, never final evidence (§12.1)."""

        return self.record.reliability_tier in {"A", "B", "C"}


class SourceCapture:
    """Fetches and stores research sources with provenance."""

    def __init__(self, client: httpx.Client | None = None, max_chars: int = 20_000) -> None:
        self._client = client
        self.max_chars = max_chars

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=25.0, follow_redirects=True
            )
        return self._client

    def fetch_page(self, url: str, property_key: str | None = None) -> CapturedSource | None:
        try:
            response = self._http().get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("source fetch failed", extra={"detail": {"url": url, "error": str(exc)}})
            return None

        html = response.text
        document = parse_html(html)
        title_node = document.select_one("title")
        text = document.text()[: self.max_chars]
        record = SourceRecord(
            property_key=property_key,
            type="research",
            url=url,
            title=title_node.text() if title_node else url,
            publisher=(urlparse(url).hostname or ""),
            content_hash=hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest(),
            reliability_tier=classify_tier(url),
            retrieved_at=utcnow(),
            excerpt=text[:600],
        )
        return CapturedSource(record=record, text=text)

    # -- MediaWiki --------------------------------------------------------
    def fetch_wikipedia(self, title: str, property_key: str | None = None) -> CapturedSource | None:
        """Read an article summary + extract through the MediaWiki API (FR-068)."""

        api = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "prop": "extracts|info",
            "explaintext": "1",
            "exsectionformat": "plain",
            "inprop": "url",
            "redirects": "1",
            "titles": title,
        }
        try:
            response = self._http().get(api, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "wikipedia fetch failed", extra={"detail": {"title": title, "error": str(exc)}}
            )
            return None

        pages = (payload.get("query") or {}).get("pages") or {}
        for page in pages.values():
            if "missing" in page:
                continue
            extract = (page.get("extract") or "")[: self.max_chars]
            if not extract:
                continue
            url = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{quote(title)}"
            record = SourceRecord(
                property_key=property_key,
                type="research",
                url=url,
                title=page.get("title", title),
                publisher="Wikipedia",
                content_hash=hashlib.sha256(extract.encode("utf-8")).hexdigest(),
                reliability_tier="C",
                retrieved_at=utcnow(),
                excerpt=extract[:600],
            )
            return CapturedSource(record=record, text=extract)
        return None

    def fetch_commons_media(self, search: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search Commons for reusable contextual media with full attribution."""

        api = "https://commons.wikimedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrsearch": search,
            "gsrlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": "1920",
        }
        try:
            response = self._http().get(api, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "commons search failed", extra={"detail": {"search": search, "error": str(exc)}}
            )
            return []

        results: list[dict[str, Any]] = []
        for page in ((payload.get("query") or {}).get("pages") or {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            results.append(
                {
                    "title": page.get("title", ""),
                    "url": info.get("thumburl") or info.get("url", ""),
                    "descriptionurl": info.get("descriptionurl", ""),
                    "width": info.get("thumbwidth") or info.get("width", 0),
                    "height": info.get("thumbheight") or info.get("height", 0),
                    "license": _meta_value(meta, "LicenseShortName"),
                    "artist": _strip_html(_meta_value(meta, "Artist")),
                    "credit": _strip_html(_meta_value(meta, "Credit")),
                    "usage_terms": _meta_value(meta, "UsageTerms"),
                }
            )
        return [item for item in results if item["url"]]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _meta_value(meta: dict[str, Any], key: str) -> str:
    entry = meta.get(key)
    if isinstance(entry, dict):
        return str(entry.get("value", ""))
    return str(entry or "")


def _strip_html(value: str) -> str:
    return parse_html(value).text() if "<" in value else value


def attribution_line(media: dict[str, Any]) -> str:
    """Build the credit string Commons requires us to carry (FR-068)."""

    parts = [media.get("title", "").replace("File:", "").strip()]
    if media.get("artist"):
        parts.append(f"by {media['artist']}")
    if media.get("license"):
        parts.append(f"({media['license']})")
    if media.get("descriptionurl"):
        parts.append(media["descriptionurl"])
    return " ".join(part for part in parts if part)
