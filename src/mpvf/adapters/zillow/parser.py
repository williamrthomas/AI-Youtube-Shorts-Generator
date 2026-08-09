"""Pure parsing for the Zillow adapter.

Nothing here touches the network, so every path is covered by fixture tests
(§19.2). The Playwright adapter supplies HTML; this module turns it into
observations.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mpvf.acquisition.dom import Node, embedded_json_blocks, parse_html
from mpvf.adapters.base import SelectorFailure
from mpvf.adapters.zillow import selectors_v1 as sel
from mpvf.models.domain import ListingObservation, RawListingCard, RawListingPage
from mpvf.normalization.listing import build_observation

SOURCE = "zillow"

_ZPID = re.compile(r"/(\d{6,})_zpid")
_BEDS = re.compile(r"([\d.]+)\s*(?:bd|bds|bed|beds)\b", re.I)
_BATHS = re.compile(r"([\d.]+)\s*(?:ba|bath|baths)\b", re.I)
_SQFT = re.compile(r"([\d,]+)\s*sqft", re.I)
_ACRES = re.compile(r"([\d.,]+)\s*acres?\b", re.I)


def detect_challenge(html: str) -> str | None:
    """Return the challenge marker found in ``html``, if any (FR-015)."""

    blob = html[:200_000].lower()
    for marker in sel.CHALLENGE_MARKERS:
        if marker in blob:
            return marker
    return None


def looks_empty(html: str) -> bool:
    blob = html.lower()
    return any(marker in blob for marker in sel.EMPTY_RESULT_MARKERS)


def _first(node: Node, selectors: list[str]) -> str:
    return node.first_text(*selectors)


def _first_node(node: Node, selectors: list[str]) -> Node | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found is not None:
            return found
    return None


def _absolute(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"https://www.zillow.com{url}"
    return url


def _images_from(node: Node) -> list[str]:
    urls: list[str] = []
    for selector in sel.CARD_IMAGE + sel.DETAIL_IMAGES:
        for element in node.select(selector):
            candidate = element.attr("src") or element.attr("srcset").split(" ")[0]
            candidate = _absolute(candidate.strip())
            if candidate.startswith("http") and candidate not in urls:
                urls.append(candidate)
    return urls


def _details_from_text(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if match := _BEDS.search(text):
        fields["beds"] = match.group(1)
    if match := _BATHS.search(text):
        fields["baths"] = match.group(1)
    if match := _SQFT.search(text):
        fields["square_feet"] = match.group(1)
    if match := _ACRES.search(text):
        fields["acres"] = match.group(1)
    return fields


def extract_cards(html: str, source_url: str, limit: int = 60) -> list[RawListingCard]:
    """Extract raw result cards from a search page (FR-010, FR-011)."""

    challenge = detect_challenge(html)
    if challenge:
        raise SelectorFailure(SOURCE, f"challenge:{challenge}")

    document = parse_html(html)
    cards: list[Node] = []
    for selector in sel.RESULT_CARD:
        cards = document.select(selector)
        if cards:
            break
    if not cards:
        if looks_empty(html):
            return []
        raise SelectorFailure(SOURCE, " | ".join(sel.RESULT_CARD))

    results: list[RawListingCard] = []
    for card in cards[:limit]:
        link_node = _first_node(card, sel.CARD_LINK)
        listing_url = _absolute(link_node.attr("href")) if link_node else ""
        detail_text = (
            " ".join(node.text() for node in card.select(sel.CARD_DETAILS[0])) or card.text()
        )
        fields: dict[str, Any] = {
            "listing_url": listing_url,
            "price": _first(card, sel.CARD_PRICE),
            "address": _first(card, sel.CARD_ADDRESS),
            "status": _first(card, sel.CARD_STATUS) or "for sale",
            "brokerage_name": _first(card, sel.CARD_BROKER),
            "image_urls": _images_from(card),
            "card_text": card.text(),
        }
        fields.update(_details_from_text(detail_text))
        if listing_url and (match := _ZPID.search(listing_url)):
            fields["source_property_id"] = match.group(1)
        results.append(RawListingCard(source=SOURCE, source_url=source_url, html="", fields=fields))
    return results


def parse_card(raw: RawListingCard) -> ListingObservation:
    fields = dict(raw.fields)
    fields.setdefault("state", "ME")
    return build_observation(SOURCE, fields, raw.source_url)


def _fields_from_hydration(html: str) -> dict[str, Any]:
    """Read hydration JSON when the page ships one (§11.2: prefer stable data)."""

    fields: dict[str, Any] = {}
    for script_id, body in embedded_json_blocks(html).items():
        if script_id not in sel.JSON_SCRIPT_IDS:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        found = _search_property_payload(payload)
        if found:
            fields.update(found)
            break
    return fields


_PAYLOAD_KEYS = {
    "zpid": "source_property_id",
    "price": "price",
    "streetAddress": "street",
    "city": "city",
    "state": "state",
    "zipcode": "postal_code",
    "bedrooms": "beds",
    "bathrooms": "baths",
    "livingArea": "square_feet",
    "lotAreaValue": "acres",
    "yearBuilt": "year_built",
    "homeType": "property_type",
    "homeStatus": "status",
    "description": "description_text",
    "latitude": "latitude",
    "longitude": "longitude",
    "monthlyHoaFee": "hoa_fee",
    "mlsid": "mls_number",
}


def _search_property_payload(payload: Any, depth: int = 0) -> dict[str, Any] | None:
    """Depth-first search for a dict that looks like a property record."""

    if depth > 12:
        return None
    if isinstance(payload, dict):
        if "zpid" in payload and ("price" in payload or "homeStatus" in payload):
            mapped = {
                target: payload[key]
                for key, target in _PAYLOAD_KEYS.items()
                if payload.get(key) is not None
            }
            broker = payload.get("attributionInfo") or {}
            if isinstance(broker, dict):
                if broker.get("brokerName"):
                    mapped["brokerage_name"] = broker["brokerName"]
                if broker.get("agentName"):
                    mapped["agent_name"] = broker["agentName"]
                if broker.get("mlsId"):
                    mapped.setdefault("mls_number", broker["mlsId"])
            photos = payload.get("photos") or payload.get("responsivePhotos") or []
            urls: list[str] = []
            for photo in photos if isinstance(photos, list) else []:
                url = _photo_url(photo)
                if url:
                    urls.append(url)
            if urls:
                mapped["image_urls"] = urls
            return mapped
        for value in payload.values():
            found = _search_property_payload(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _search_property_payload(item, depth + 1)
            if found:
                return found
    return None


def _photo_url(photo: Any) -> str | None:
    if isinstance(photo, str):
        return photo
    if isinstance(photo, dict):
        if isinstance(photo.get("url"), str):
            return photo["url"]
        for key in ("mixedSources", "sources"):
            block = photo.get(key)
            if isinstance(block, dict):
                for entries in block.values():
                    if isinstance(entries, list) and entries:
                        last = entries[-1]
                        if isinstance(last, dict) and isinstance(last.get("url"), str):
                            return last["url"]
            if isinstance(block, list) and block:
                last = block[-1]
                if isinstance(last, dict) and isinstance(last.get("url"), str):
                    return last["url"]
    return None


def parse_listing_html(html: str, url: str) -> ListingObservation:
    """Parse a listing detail page, preferring hydration JSON over the DOM."""

    challenge = detect_challenge(html)
    if challenge:
        raise SelectorFailure(SOURCE, f"challenge:{challenge}")

    document = parse_html(html)
    fields: dict[str, Any] = {
        "listing_url": url,
        "price": _first(document, sel.DETAIL_PRICE),
        "address": _first(document, sel.DETAIL_ADDRESS),
        "status": _first(document, sel.DETAIL_STATUS) or "for sale",
        "brokerage_name": _first(document, sel.DETAIL_BROKER),
        "hoa_fee": _first(document, sel.DETAIL_HOA),
        "description_text": _first(document, sel.DETAIL_DESCRIPTION),
        "image_urls": _images_from(document),
        "state": "ME",
    }
    facts_text = (
        " ".join(node.text() for node in document.select(sel.DETAIL_FACTS[0])) or document.text()
    )
    fields.update(_details_from_text(facts_text))

    hydration = _fields_from_hydration(html)
    for key, value in hydration.items():
        if key == "image_urls":
            merged = list(dict.fromkeys([*value, *fields.get("image_urls", [])]))
            fields["image_urls"] = merged
        elif value not in (None, ""):
            fields[key] = value

    if match := _ZPID.search(url):
        fields.setdefault("source_property_id", match.group(1))
    if not fields.get("price") and not fields.get("address"):
        raise SelectorFailure(SOURCE, " | ".join(sel.DETAIL_PRICE + sel.DETAIL_ADDRESS))
    return build_observation(SOURCE, fields, url)


def parse_listing(raw: RawListingPage) -> ListingObservation:
    return parse_listing_html(raw.html, raw.url)
