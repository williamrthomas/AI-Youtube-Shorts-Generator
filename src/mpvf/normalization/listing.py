"""Listing field parsing and normalization (FR-020 .. FR-025).

Everything here is deliberately tolerant of messy source text and strict about
what it emits: an unparseable value becomes ``None`` rather than a guess.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from mpvf.config.templates import ListingStatus, PropertyType
from mpvf.models.domain import ListingObservation, WaterfrontFacts

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

STATUS_PATTERNS: tuple[tuple[str, ListingStatus], ...] = (
    ("active under contract", "active_under_contract"),
    ("under contract", "active_under_contract"),
    ("accepting backup", "contingent"),
    ("contingent", "contingent"),
    ("pending", "pending"),
    ("sold", "sold"),
    ("closed", "sold"),
    ("off market", "off_market"),
    ("off-market", "off_market"),
    ("withdrawn", "off_market"),
    ("expired", "off_market"),
    ("coming soon", "unknown"),
    ("for sale", "active"),
    ("active", "active"),
    ("new listing", "active"),
    ("price cut", "active"),
)

TYPE_PATTERNS: tuple[tuple[str, PropertyType], ...] = (
    ("single family", "single_family"),
    ("single-family", "single_family"),
    ("cape", "single_family"),
    ("colonial", "single_family"),
    ("ranch", "single_family"),
    ("cottage", "seasonal_residence"),
    ("camp", "seasonal_residence"),
    ("seasonal", "seasonal_residence"),
    ("condo", "condo"),
    ("condominium", "condo"),
    ("townhouse", "townhouse"),
    ("townhome", "townhouse"),
    ("multi family", "multi_family"),
    ("multi-family", "multi_family"),
    ("duplex", "multi_family"),
    ("farm", "farm"),
    ("lot", "land"),
    ("land", "land"),
    ("acreage", "land"),
    ("house", "single_family"),
)

WATER_BODY_HINTS = (
    "bay",
    "harbor",
    "cove",
    "reach",
    "sound",
    "river",
    "lake",
    "pond",
    "ocean",
    "atlantic",
    "thorofare",
)


def parse_price(value: Any) -> int | None:
    """Parse ``$1,250,000``/``1.25M``/``850K`` into an integer of dollars."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value).strip().lower().replace("$", "").replace(",", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        amount = float(match.group().replace(",", "")) * multiplier
    except ValueError:
        return None
    return int(round(amount)) if amount > 0 else None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_number(value)
    return int(round(number)) if number is not None else None


def parse_square_feet(value: Any) -> int | None:
    number = parse_int(value)
    if number is None or number <= 0:
        return None
    # Guard against acreage or price leaking into the sqft field.
    return number if 120 <= number <= 60_000 else None


def parse_acres(value: Any) -> float | None:
    """Parse acreage; converts an explicit square-foot lot size."""

    if value is None:
        return None
    text = str(value).lower()
    number = parse_number(text)
    if number is None or number <= 0:
        return None
    if "sq" in text and "acre" not in text:
        return round(number / 43_560, 3)
    return round(number, 3)


def parse_year_built(value: Any) -> int | None:
    year = parse_int(value)
    if year is None:
        return None
    current = datetime.now(UTC).year + 2
    return year if 1600 <= year <= current else None


def parse_status(value: Any) -> ListingStatus:
    if not value:
        return "unknown"
    text = str(value).lower()
    for needle, status in STATUS_PATTERNS:
        if needle in text:
            return status
    return "unknown"


def parse_property_type(*values: Any) -> PropertyType:
    haystack = " ".join(str(value).lower() for value in values if value)
    for needle, property_type in TYPE_PATTERNS:
        if needle in haystack:
            return property_type
    return "other"


def parse_beds_baths(value: Any) -> float | None:
    number = parse_number(value)
    if number is None or number < 0 or number > 40:
        return None
    return round(number, 1)


def parse_hoa(value: Any) -> float | None:
    number = parse_number(value)
    if number is None or number < 0:
        return None
    text = str(value).lower()
    if "year" in text or "annual" in text or "/yr" in text:
        return round(number / 12, 2)
    return round(number, 2)


def detect_waterfront(text: str | None, facts: dict[str, Any] | None = None) -> WaterfrontFacts:
    """Derive structured waterfront facts from listing prose plus known fields."""

    facts = facts or {}
    blob = f"{text or ''} {' '.join(str(v) for v in facts.values())}".lower()
    result = WaterfrontFacts()

    if any(word in blob for word in ("oceanfront", "ocean front", "on the atlantic")):
        result.frontage_type = "ocean"
    elif "lakefront" in blob or "lake front" in blob:
        result.frontage_type = "lake"
    elif "riverfront" in blob or "river front" in blob:
        result.frontage_type = "river"
    elif "pondfront" in blob or "on the pond" in blob:
        result.frontage_type = "pond"
    elif "bayfront" in blob or "on the bay" in blob:
        result.frontage_type = "bay"
    elif "waterfront" in blob:
        result.frontage_type = "unknown"

    # "190 feet of owned water frontage", "±145 ft of shore front", "320' of frontage"
    frontage = re.search(
        r"(\d[\d,]*)\s*(?:\+/-\s*|±\s*)?(?:feet|foot|ft\.?|')[^.]{0,40}?front",
        blob,
    )
    if frontage:
        result.owned_frontage_feet = parse_number(frontage.group(1))

    if "deeded" in blob and ("access" in blob or "right of way" in blob):
        result.deeded_access = True
    if any(phrase in blob for phrase in ("water view", "ocean view", "lake view", "views of the")):
        result.water_view = True

    # A named water body is capitalized in the source prose: "Linekin Bay".
    body = re.search(
        r"\b([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+)?)\s+("
        + "|".join(hint.title() for hint in WATER_BODY_HINTS)
        + r")\b",
        text or "",
    )
    if body:
        result.water_body = f"{body.group(1)} {body.group(2)}"
        if result.frontage_type == "unknown" and result.owned_frontage_feet is not None:
            # "190 feet of owned frontage on Linekin Bay" is tidal frontage even
            # though the prose never says "oceanfront".
            result.frontage_type = _frontage_from_body(body.group(2).lower())
    return result


_BODY_FRONTAGE = {
    "bay": "bay",
    "harbor": "bay",
    "cove": "bay",
    "reach": "bay",
    "sound": "bay",
    "thorofare": "bay",
    "ocean": "ocean",
    "atlantic": "ocean",
    "lake": "lake",
    "pond": "pond",
    "river": "river",
}


def _frontage_from_body(body_word: str) -> str:
    return _BODY_FRONTAGE.get(body_word, "unknown")


def is_seasonal(text: str | None) -> bool | None:
    if not text:
        return None
    blob = text.lower()
    if any(word in blob for word in ("seasonal", "summer only", "not winterized", "three season")):
        return True
    if any(word in blob for word in ("year-round", "year round", "winterized", "full time")):
        return False
    return None


def looks_like_auction(text: str | None) -> bool:
    if not text:
        return False
    blob = text.lower()
    return "auction" in blob or "bidding closes" in blob


def build_observation(source: str, fields: dict[str, Any], source_url: str) -> ListingObservation:
    """Convert an adapter's loose field mapping into a validated observation."""

    description = fields.get("description_text") or fields.get("description")
    # A search card usually carries only a single address line; split it so
    # downstream town filters and locator maps have something to work with.
    raw_address = fields.get("address")
    city, postal, street = fields.get("city"), fields.get("postal_code"), fields.get("street")
    if raw_address and not (city and street):
        parts = [chunk.strip() for chunk in str(raw_address).split(",") if chunk.strip()]
        if parts and not street:
            street = parts[0]
        if len(parts) >= 2 and not city:
            city = parts[1]
        if len(parts) >= 3 and not postal:
            tail = parts[2].split()
            if len(tail) >= 2:
                postal = tail[1]

    observation = ListingObservation(
        source=source,
        source_url=source_url,
        listing_url=fields.get("listing_url"),
        source_property_id=(
            str(fields["source_property_id"]) if fields.get("source_property_id") else None
        ),
        mls_number=(str(fields["mls_number"]) if fields.get("mls_number") else None),
        raw_address=raw_address,
        street=street,
        city=city,
        state=(fields.get("state") or "ME"),
        postal_code=postal,
        latitude=parse_number(fields.get("latitude")),
        longitude=parse_number(fields.get("longitude")),
        price=parse_price(fields.get("price")),
        previous_price=parse_price(fields.get("previous_price")),
        status=parse_status(fields.get("status")),
        property_type=parse_property_type(
            fields.get("property_type"),
            fields.get("home_type"),
            fields.get("status"),  # search cards state the type in the status line
            description,
        ),
        beds=parse_beds_baths(fields.get("beds")),
        baths=parse_beds_baths(fields.get("baths")),
        square_feet=parse_square_feet(fields.get("square_feet")),
        acres=parse_acres(fields.get("lot_size") or fields.get("acres")),
        year_built=parse_year_built(fields.get("year_built")),
        hoa_fee=parse_hoa(fields.get("hoa_fee")),
        seasonal=is_seasonal(description),
        brokerage_name=_clean_broker(fields.get("brokerage_name") or fields.get("broker")),
        agent_name=_clean_broker(fields.get("agent_name")),
        image_urls=[url for url in (fields.get("image_urls") or []) if url],
        description_text=description,
        facts={k: v for k, v in fields.items() if k not in {"html", "image_urls"}},
    )
    observation.waterfront = detect_waterfront(description, fields)
    if looks_like_auction(description):
        observation.facts["auction"] = True
    return observation


def _clean_broker(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(
        r"^(listed by|courtesy of|listing by)\s*:?\s*", "", str(value).strip(), flags=re.I
    )
    text = text.strip(" ,.-")
    return text or None


def price_change_pct(old: int | None, new: int | None) -> float | None:
    if not old or not new:
        return None
    return round((new - old) / old * 100, 2)


def is_fresh(listed_at: datetime | None, days: int = 45) -> bool:
    if listed_at is None:
        return False
    if listed_at.tzinfo is None:
        listed_at = listed_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - listed_at <= timedelta(days=days)
