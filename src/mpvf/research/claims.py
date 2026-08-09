"""Claim construction (§7.7, §7.8).

Two sources of claims:

1. **Listing claims** — derived deterministically from verified structured
   fields. These are the only claims allowed to carry a price or a status.
2. **Context claims** — extracted from research sources. A property-specific
   historical claim requires a source that names the property (FR-065); an
   extraction that only supports town context is stored with ``scope="town"``
   so the writer can never present it as the history of the house (FR-064).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from mpvf.generation.provider import GenerationError, GenerationProvider, Message
from mpvf.models.domain import Claim, PropertyRecord, SourceRecord
from mpvf.normalization.address import fold_text
from mpvf.research.sources import CapturedSource

MAX_CONTEXT_CLAIMS_PER_PROPERTY = 2


# -- deterministic listing claims -----------------------------------------


def _fmt_money(value: int) -> str:
    return f"${value:,}"


def listing_claims(record: PropertyRecord, source_ids: list[str]) -> list[Claim]:
    """Structured facts turned into narratable claims with evidence refs."""

    canonical = record.canonical
    key = record.property_key
    confidence = record.verification.confidence if record.verification else 0.5
    claims: list[Claim] = []

    def add(
        text: str,
        claim_type: str,
        attribute: str,
        value: Any = None,
        unit: str | None = None,
        bump: float = 0.0,
    ) -> None:
        claims.append(
            Claim(
                property_key=key,
                text=text,
                type=claim_type,  # type: ignore[arg-type]
                attribute=attribute,
                value=value,
                unit=unit,
                sources=source_ids,
                confidence=round(min(1.0, confidence + bump), 3),
                scope="property",
            )
        )

    if canonical.price:
        add(
            f"The asking price is {_fmt_money(canonical.price)}.",
            "price",
            "asking_price",
            canonical.price,
            "usd",
        )
    if canonical.previous_price and canonical.price and canonical.previous_price > canonical.price:
        cut = canonical.previous_price - canonical.price
        add(
            f"The price was reduced from {_fmt_money(canonical.previous_price)} by {_fmt_money(cut)}.",
            "price",
            "price_reduction",
            cut,
            "usd",
        )
    add(
        f"The listing status is {canonical.status.replace('_', ' ')}.",
        "status",
        "status",
        canonical.status,
    )

    town = record.city or "Maine"
    add(f"The property is in {town}, Maine.", "location", "town", town)

    if canonical.beds:
        add(
            f"It has {_number(canonical.beds)} bedrooms.",
            "listing_fact",
            "beds",
            canonical.beds,
            "beds",
        )
    if canonical.baths:
        add(
            f"It has {_number(canonical.baths)} bathrooms.",
            "listing_fact",
            "baths",
            canonical.baths,
            "baths",
        )
    if canonical.square_feet:
        add(
            f"The house measures approximately {canonical.square_feet:,} square feet.",
            "measurement",
            "square_feet",
            canonical.square_feet,
            "sqft",
        )
    if canonical.acres:
        add(
            f"The lot is approximately {canonical.acres} acres.",
            "measurement",
            "acres",
            canonical.acres,
            "acres",
        )
    if canonical.year_built:
        add(
            f"The house was built in {canonical.year_built}.",
            "listing_fact",
            "year_built",
            canonical.year_built,
            "year",
        )
    if canonical.hoa_fee:
        add(
            f"The association fee is approximately {_fmt_money(int(canonical.hoa_fee))} per month.",
            "listing_fact",
            "hoa_fee",
            canonical.hoa_fee,
            "usd_month",
        )
    if canonical.property_type == "condo":
        add("The property is a condominium.", "listing_fact", "property_type", "condo")
    if canonical.seasonal:
        add("The property is described as a seasonal residence.", "listing_fact", "seasonal", True)

    water = canonical.waterfront
    if water.owned_frontage_feet:
        body = water.water_body or "the water"
        add(
            f"The property includes approximately {int(water.owned_frontage_feet)} feet of owned frontage on {body}.",
            "measurement",
            "frontage_feet",
            water.owned_frontage_feet,
            "feet",
        )
    elif water.frontage_type not in ("unknown", "none"):
        body = water.water_body or f"{water.frontage_type} water"
        add(
            f"The property has {water.frontage_type} frontage on {body}.",
            "listing_fact",
            "frontage_type",
            water.frontage_type,
        )
    if water.deeded_access:
        add("The property carries deeded water access.", "listing_fact", "deeded_access", True)
    if water.water_view and not water.owned_frontage_feet:
        add(
            "The listing describes water views from the property.",
            "listing_fact",
            "water_view",
            True,
        )

    if canonical.brokerage_name or canonical.agent_name:
        who = " of ".join(part for part in (canonical.agent_name, canonical.brokerage_name) if part)
        add(f"The listing is represented by {who}.", "attribution", "brokerage", who)

    return claims


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


# -- model-assisted context claims ----------------------------------------


class ExtractedClaim(BaseModel):
    text: str = Field(description="One sentence, factual, no adjectives of praise")
    supporting_excerpt: str = Field(
        description="Verbatim sentence from the source that supports it"
    )
    scope: str = Field(description="property, town or region")
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


_EXTRACTION_SYSTEM = """You extract verifiable facts from a single source document.

Rules:
- Only state what the document itself says. Never add outside knowledge.
- Each claim must be one sentence, specific, and free of promotional language.
- supporting_excerpt must be copied verbatim from the document.
- scope is "property" only if the document names this exact property by address,
  formal name, or unmistakable description. Otherwise use "town" or "region".
- Prefer facts that explain what the location is actually like over trivia.
- If the document supports nothing useful, return an empty list."""


def extract_context_claims(
    provider: GenerationProvider,
    record: PropertyRecord,
    captured: CapturedSource,
    max_claims: int = 3,
) -> tuple[list[Claim], dict[str, Any] | None]:
    """Ask the provider for claims, then enforce the scope rules in code."""

    if not captured.usable_for_facts:
        return [], None

    address = record.canonical.raw_address or record.normalized_address
    user = (
        f"Property under consideration: {address}, {record.city or ''} Maine.\n"
        f"Year built: {record.canonical.year_built or 'unknown'}.\n\n"
        f"Source title: {captured.record.title}\n"
        f"Source publisher: {captured.record.publisher}\n"
        f"Document:\n{captured.text[:8000]}\n\n"
        f"Extract at most {max_claims} claims."
    )
    messages = [
        Message(role="system", content=_EXTRACTION_SYSTEM),
        Message(role="user", content=user),
    ]
    try:
        result, generation = provider.generate_structured(
            "source_to_claim", messages, ExtractionResult, temperature=0.1
        )
    except GenerationError:
        return [], None

    claims: list[Claim] = []
    for extracted in result.claims[:max_claims]:  # type: ignore[attr-defined]
        if not _excerpt_present(extracted.supporting_excerpt, captured.text):
            continue
        scope = _enforce_scope(extracted.scope, record, captured)
        claims.append(
            Claim(
                property_key=record.property_key,
                text=extracted.text.strip(),
                type="history" if scope == "property" else "context",
                sources=[captured.record.source_id],
                confidence=round(min(extracted.confidence, _tier_ceiling(captured)), 3),
                scope=scope,
                note=extracted.supporting_excerpt[:300],
                eligible_for_script=True,
            )
        )
    return claims, generation.as_dict()


def _excerpt_present(excerpt: str, document: str) -> bool:
    """Reject a claim whose 'verbatim' excerpt is not actually in the source."""

    needle = fold_text(excerpt)[:120]
    if len(needle) < 20:
        return False
    return needle in fold_text(document)


def _tier_ceiling(captured: CapturedSource) -> float:
    return {"A": 0.98, "B": 0.9, "C": 0.75}.get(captured.record.reliability_tier, 0.4)


def _enforce_scope(claimed_scope: str, record: PropertyRecord, captured: CapturedSource) -> str:
    """FR-065: a property-scoped claim needs a source that identifies the property."""

    scope = claimed_scope.strip().lower()
    if scope != "property":
        return "town" if scope == "town" else "region"
    address = record.canonical.raw_address or record.normalized_address
    street = fold_text(address).split(",")[0]
    document = fold_text(f"{captured.record.title} {captured.text}")
    if street and len(street) > 6 and street in document:
        return "property"
    return "town"


def dedupe_claims(claims: list[Claim]) -> list[Claim]:
    seen: set[str] = set()
    unique: list[Claim] = []
    for claim in claims:
        fingerprint = claim.fingerprint()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(claim)
    return unique


def limit_context_claims(
    claims: list[Claim], per_property: int = MAX_CONTEXT_CLAIMS_PER_PROPERTY
) -> list[Claim]:
    """FR-066: at most N history/context beats per property."""

    kept: list[Claim] = []
    counts: dict[str, int] = {}
    for claim in sorted(claims, key=lambda c: (-c.confidence, c.text)):
        if claim.type in {"history", "context"}:
            key = claim.property_key or ""
            if counts.get(key, 0) >= per_property:
                claim.eligible_for_script = False
                claim.note = (claim.note + " | trimmed: context beat limit").strip(" |")
                kept.append(claim)
                continue
            counts[key] = counts.get(key, 0) + 1
        kept.append(claim)
    return kept


_NUMERIC = re.compile(r"\d")


def mark_conflicts(claims: list[Claim]) -> list[Claim]:
    """Flag numeric claims of the same type that disagree (FR-073)."""

    buckets: dict[tuple[str, str], list[Claim]] = {}
    for claim in claims:
        # Only same-attribute claims can contradict each other: an asking price
        # and a price reduction are both prices, and both are true.
        if claim.attribute is None or claim.value is None:
            continue
        if not _NUMERIC.search(str(claim.value)):
            continue
        buckets.setdefault((claim.property_key or "", claim.attribute), []).append(claim)

    for group in buckets.values():
        values = {str(claim.value) for claim in group}
        if len(values) > 1:
            for claim in group:
                claim.conflict_state = "unresolved"
                claim.eligible_for_script = False
                claim.note = (claim.note + " | conflicting values across sources").strip(" |")
    return claims


def build_source_records(captured: list[CapturedSource]) -> list[SourceRecord]:
    return [item.record for item in captured]
