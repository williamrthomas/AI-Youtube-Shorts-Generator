"""Address normalization and fuzzy matching (FR-020, FR-021).

The canonical form is deliberately lossy — it exists to make two observations
of the same house collide. The original source formatting is always retained
on the observation itself.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

STREET_SUFFIXES = {
    "street": "st",
    "st": "st",
    "road": "rd",
    "rd": "rd",
    "avenue": "ave",
    "ave": "ave",
    "av": "ave",
    "drive": "dr",
    "dr": "dr",
    "lane": "ln",
    "ln": "ln",
    "court": "ct",
    "ct": "ct",
    "circle": "cir",
    "cir": "cir",
    "boulevard": "blvd",
    "blvd": "blvd",
    "place": "pl",
    "pl": "pl",
    "terrace": "ter",
    "ter": "ter",
    "trail": "trl",
    "trl": "trl",
    "highway": "hwy",
    "hwy": "hwy",
    "route": "rte",
    "rte": "rte",
    "rt": "rte",
    "point": "pt",
    "pt": "pt",
    "extension": "ext",
    "way": "way",
    "loop": "loop",
    "cove": "cv",
    "cv": "cv",
    "island": "is",
    "neck": "neck",
    "landing": "lndg",
    "ridge": "rdg",
    "harbor": "hbr",
    "hbr": "hbr",
    "shore": "shr",
    "beach": "bch",
}

DIRECTIONS = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}

UNIT_MARKERS = ("unit", "apt", "apartment", "#", "ste", "suite", "lot")

_PUNCTUATION = re.compile(r"[.,;:*\"']")
_WHITESPACE = re.compile(r"\s+")
_UNIT_SPLIT = re.compile(
    r"\b(?:unit|apt|apartment|ste|suite|lot)\b\.?\s*#?\s*([\w-]+)$|#\s*([\w-]+)$",
    re.IGNORECASE,
)

MAINE_ALIASES = {
    "so portland": "south portland",
    "s portland": "south portland",
    "no berwick": "north berwick",
    "mt desert": "mount desert",
    "mt. desert": "mount desert",
    "bar hbr": "bar harbor",
    "ogunquit village": "ogunquit",
    "cape eliz": "cape elizabeth",
}


@dataclass(frozen=True)
class NormalizedAddress:
    street: str
    unit: str | None
    city: str
    state: str
    postal_code: str | None
    canonical: str

    @property
    def key(self) -> str:
        """Stable identity used for deduplication across sources."""

        return hashlib.sha1(self.canonical.encode("utf-8")).hexdigest()[:20]


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def fold_text(value: str) -> str:
    """Public case/accent/punctuation folding, shared with claim matching."""

    return _fold(value)


def normalize_city(city: str | None) -> str:
    if not city:
        return ""
    folded = _fold(city)
    return MAINE_ALIASES.get(folded, folded)


def normalize_street(street: str | None) -> tuple[str, str | None]:
    """Return ``(canonical_street, unit)``."""

    if not street:
        return "", None
    folded = _fold(street)
    unit: str | None = None
    match = _UNIT_SPLIT.search(folded)
    if match:
        unit = (match.group(1) or match.group(2) or "").strip() or None
        folded = folded[: match.start()].strip()

    tokens: list[str] = []
    for raw_token in folded.split():
        token = DIRECTIONS.get(raw_token, raw_token)
        token = STREET_SUFFIXES.get(token, token)
        tokens.append(token)
    return " ".join(tokens).strip(), unit


def normalize_postal(postal: str | None) -> str | None:
    if not postal:
        return None
    digits = re.sub(r"\D", "", postal)
    return digits[:5] if len(digits) >= 5 else None


def parse_address(
    raw: str | None,
    *,
    street: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
) -> NormalizedAddress:
    """Normalize an address from either a single raw line or parts.

    A raw line like ``"12 Ocean Ave Unit 3, Boothbay Harbor, ME 04538"`` is
    split on commas; explicit parts always win over parsed ones.
    """

    parsed_street, parsed_city, parsed_state, parsed_postal = "", "", "", None
    if raw:
        chunks = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        if chunks:
            parsed_street = chunks[0]
        if len(chunks) >= 2:
            parsed_city = chunks[1]
        if len(chunks) >= 3:
            tail = chunks[2].split()
            if tail:
                parsed_state = tail[0]
            if len(tail) >= 2:
                parsed_postal = tail[1]

    street_value, unit = normalize_street(street or parsed_street)
    city_value = normalize_city(city or parsed_city)
    state_value = (state or parsed_state or "ME").strip().upper()[:2] or "ME"
    postal_value = normalize_postal(postal_code or parsed_postal)

    canonical_parts = [street_value]
    if unit:
        canonical_parts.append(f"unit {unit}")
    canonical_parts.extend(part for part in (city_value, state_value.lower()) if part)
    if postal_value:
        canonical_parts.append(postal_value)
    canonical = " ".join(part for part in canonical_parts if part)

    return NormalizedAddress(
        street=street_value,
        unit=unit,
        city=city_value,
        state=state_value,
        postal_code=postal_value,
        canonical=canonical,
    )


def display_address(address: NormalizedAddress) -> str:
    """Title-cased human-facing rendering of a normalized address."""

    street = " ".join(
        token.upper() if token in {"n", "s", "e", "w", "ne", "nw", "se", "sw"} else token.title()
        for token in address.street.split()
    )
    parts = [street]
    if address.unit:
        parts.append(f"Unit {address.unit.upper()}")
    tail = ", ".join(part for part in (address.city.title(), address.state) if part)
    joined = ", ".join(part for part in (" ".join(parts), tail) if part)
    return f"{joined} {address.postal_code}".strip() if address.postal_code else joined


def token_similarity(left: str, right: str) -> float:
    """Jaccard similarity over normalized tokens; 1.0 is identical."""

    left_tokens = set(_fold(left).split())
    right_tokens = set(_fold(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return round(len(intersection) / len(union), 4)


def addresses_match(
    left: NormalizedAddress, right: NormalizedAddress, threshold: float = 0.82
) -> bool:
    """Fuzzy address equality used as the last dedup signal (FR-021)."""

    if left.canonical == right.canonical:
        return True
    if left.city and right.city and left.city != right.city:
        return False
    if left.unit != right.unit:
        return False
    if left.postal_code and right.postal_code and left.postal_code != right.postal_code:
        return False
    return token_similarity(left.street, right.street) >= threshold
