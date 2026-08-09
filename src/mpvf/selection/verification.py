"""Verification comparison and conflict policy (§7.4).

The rule that matters: a material conflict is never resolved by picking a
side. It blocks the candidate, and the alternate takes its place (§13.6).
"""

from __future__ import annotations

from typing import Any

from mpvf.models.domain import FieldComparison, ListingObservation, VerificationResult
from mpvf.normalization.address import parse_address, token_similarity

MATERIAL_FIELDS = ("status", "price", "property_type", "address")
PRICE_TOLERANCE_PCT = 2.0

_FIELD_WEIGHTS = {
    "status": 0.25,
    "price": 0.25,
    "address": 0.20,
    "property_type": 0.10,
    "beds": 0.05,
    "baths": 0.05,
    "square_feet": 0.05,
    "year_built": 0.05,
}

_ACTIVE_EQUIVALENT = {"active", "for_sale", "unknown"}


def _compare_price(discovery: int | None, verification: int | None) -> FieldComparison:
    comparison = FieldComparison(
        field="price", discovery_value=discovery, verification_value=verification, material=True
    )
    if discovery is None or verification is None:
        comparison.agrees = discovery == verification
        comparison.confidence = 0.4 if comparison.agrees else 0.2
        comparison.note = "missing price on one side"
        return comparison
    delta_pct = abs(verification - discovery) / max(discovery, 1) * 100
    comparison.agrees = delta_pct <= PRICE_TOLERANCE_PCT
    comparison.confidence = 1.0 if delta_pct == 0 else max(0.0, 1.0 - delta_pct / 10)
    comparison.note = f"{delta_pct:.2f}% difference"
    return comparison


def _compare_status(discovery: str, verification: str) -> FieldComparison:
    comparison = FieldComparison(
        field="status", discovery_value=discovery, verification_value=verification, material=True
    )
    if discovery == verification:
        comparison.confidence = 1.0
        return comparison
    if {discovery, verification} <= _ACTIVE_EQUIVALENT:
        comparison.confidence = 0.75
        comparison.note = "one side reported an unspecified active state"
        return comparison
    comparison.agrees = False
    comparison.confidence = 0.0
    comparison.note = "status disagreement"
    return comparison


def _compare_address(
    discovery: ListingObservation, verification: ListingObservation
) -> FieldComparison:
    left = parse_address(
        discovery.raw_address,
        street=discovery.street,
        city=discovery.city,
        state=discovery.state,
        postal_code=discovery.postal_code,
    )
    right = parse_address(
        verification.raw_address,
        street=verification.street,
        city=verification.city,
        state=verification.state,
        postal_code=verification.postal_code,
    )
    similarity = (
        1.0
        if left.canonical and left.canonical == right.canonical
        else token_similarity(left.canonical, right.canonical)
    )
    return FieldComparison(
        field="address",
        discovery_value=left.canonical,
        verification_value=right.canonical,
        agrees=similarity >= 0.80,
        material=True,
        confidence=round(similarity, 3),
        note=f"address similarity {similarity:.2f}",
    )


def _compare_scalar(field: str, left: Any, right: Any, tolerance: float = 0.0) -> FieldComparison:
    comparison = FieldComparison(field=field, discovery_value=left, verification_value=right)
    if left is None or right is None:
        comparison.confidence = 0.5
        comparison.note = "value missing on one side"
        return comparison
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        span = max(abs(left), abs(right), 1)
        comparison.agrees = abs(left - right) / span <= tolerance
        comparison.confidence = 1.0 if comparison.agrees else 0.3
    else:
        comparison.agrees = str(left).lower() == str(right).lower()
        comparison.confidence = 1.0 if comparison.agrees else 0.3
    return comparison


def compare_observations(
    discovery: ListingObservation, verification: ListingObservation
) -> list[FieldComparison]:
    """Field-by-field agreement with confidence (FR-033)."""

    comparisons = [
        _compare_status(discovery.status, verification.status),
        _compare_price(discovery.price, verification.price),
        _compare_address(discovery, verification),
        FieldComparison(
            field="property_type",
            discovery_value=discovery.property_type,
            verification_value=verification.property_type,
            agrees=discovery.property_type == verification.property_type
            or "other" in {discovery.property_type, verification.property_type},
            material=True,
            confidence=1.0 if discovery.property_type == verification.property_type else 0.5,
        ),
        _compare_scalar("beds", discovery.beds, verification.beds),
        _compare_scalar("baths", discovery.baths, verification.baths),
        _compare_scalar("square_feet", discovery.square_feet, verification.square_feet, 0.05),
        _compare_scalar("year_built", discovery.year_built, verification.year_built),
    ]
    return comparisons


def verification_confidence(comparisons: list[FieldComparison]) -> float:
    total_weight = 0.0
    score = 0.0
    for comparison in comparisons:
        weight = _FIELD_WEIGHTS.get(comparison.field, 0.02)
        total_weight += weight
        score += weight * comparison.confidence
    return round(score / total_weight, 3) if total_weight else 0.0


def build_verification(
    property_key: str,
    discovery: ListingObservation,
    verification: ListingObservation | None,
    source_name: str | None = None,
    source_penalty: float = 0.0,
) -> VerificationResult:
    """Assemble the verification record, including blocking conflicts (FR-034)."""

    if verification is None:
        return VerificationResult(
            property_key=property_key,
            verified=False,
            confidence=0.0,
            detail="no verification source resolved",
            blocking_conflicts=["unverified"],
        )

    comparisons = compare_observations(discovery, verification)
    blocking = [
        f"{c.field}: {c.discovery_value!r} vs {c.verification_value!r} ({c.note})"
        for c in comparisons
        if c.material and not c.agrees
    ]
    confidence = max(0.0, verification_confidence(comparisons) - source_penalty)
    return VerificationResult(
        property_key=property_key,
        verified=not blocking,
        verification_source=source_name or verification.source,
        verification_url=verification.listing_url or verification.source_url,
        observation=verification,
        comparisons=comparisons,
        confidence=confidence,
        blocking_conflicts=blocking,
        detail="verified" if not blocking else "material conflict",
    )


def final_status_disqualifies(status: str, allowed: list[str]) -> bool:
    """Pre-publication status recheck rule (FR-037)."""

    return status not in allowed
