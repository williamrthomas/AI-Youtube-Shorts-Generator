"""Hard filters and exclusions (FR-024 .. FR-026)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import PropertyRecord
from mpvf.selection.theme import theme_fit


@dataclass(frozen=True)
class FilterOutcome:
    passed: bool
    reason: str = ""


@dataclass
class FeatureHistory:
    """Prior appearances, used for the exclusion window (FR-026)."""

    last_featured_at: datetime | None = None
    last_featured_price: int | None = None


def _price_change_pct(old: int | None, new: int | None) -> float:
    if not old or not new:
        return 0.0
    return abs(new - old) / old * 100


def apply_hard_filters(record: PropertyRecord, template: SearchTemplate) -> FilterOutcome:
    """Evaluate template hard filters against a canonical property."""

    canonical = record.canonical
    filters = template.hard_filters

    if filters.state and (record.state or "").upper() != filters.state.upper():
        return FilterOutcome(False, f"state {record.state} is not {filters.state}")

    if canonical.status not in filters.status:
        return FilterOutcome(False, f"status {canonical.status} not permitted by template")

    if canonical.property_type not in filters.property_types:
        return FilterOutcome(False, f"property type {canonical.property_type} not permitted")

    if filters.price_max is not None:
        if canonical.price is None:
            return FilterOutcome(False, "no price available but template sets a ceiling")
        if canonical.price > filters.price_max:
            return FilterOutcome(
                False, f"price {canonical.price} above ceiling {filters.price_max}"
            )

    if filters.price_min is not None and (canonical.price or 0) < filters.price_min:
        return FilterOutcome(False, f"price {canonical.price} below floor {filters.price_min}")

    if canonical.usable_image_count() < filters.min_usable_images:
        return FilterOutcome(
            False,
            f"only {canonical.usable_image_count()} images, need {filters.min_usable_images}",
        )

    if filters.min_year_built is not None:
        if canonical.year_built is None:
            return FilterOutcome(False, "year built unknown but template requires a minimum")
        if canonical.year_built < filters.min_year_built:
            return FilterOutcome(False, f"built {canonical.year_built} before minimum")

    if filters.max_year_built is not None:
        if canonical.year_built is None:
            return FilterOutcome(False, "year built unknown but template requires a maximum")
        if canonical.year_built > filters.max_year_built:
            return FilterOutcome(False, f"built {canonical.year_built} after maximum")

    if filters.min_acres is not None and (canonical.acres or 0) < filters.min_acres:
        return FilterOutcome(False, f"acreage {canonical.acres} below {filters.min_acres}")

    if filters.towns:
        town = (record.city or "").lower()
        if town not in {name.lower() for name in filters.towns}:
            return FilterOutcome(False, f"town {record.city} not in template town list")

    return FilterOutcome(True)


def apply_exclusions(
    record: PropertyRecord,
    template: SearchTemplate,
    history: FeatureHistory | None = None,
    now: datetime | None = None,
) -> FilterOutcome:
    """Evaluate exclusion rules, including the repeat-feature window."""

    canonical = record.canonical
    exclusions = template.exclusions
    now = now or datetime.now(UTC)

    if canonical.status in exclusions.statuses:
        return FilterOutcome(False, f"excluded status {canonical.status}")

    if exclusions.land_only and canonical.property_type == "land":
        return FilterOutcome(False, "land-only listing excluded by template")

    if exclusions.auction and canonical.facts.get("auction"):
        return FilterOutcome(False, "auction listing excluded by template")

    if exclusions.towns and (record.city or "").lower() in {t.lower() for t in exclusions.towns}:
        return FilterOutcome(False, f"town {record.city} excluded by template")

    if history and history.last_featured_at:
        last = history.last_featured_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        window = timedelta(days=exclusions.featured_within_days)
        if now - last <= window:
            change = _price_change_pct(history.last_featured_price, canonical.price)
            if change < exclusions.reprise_price_change_pct:
                days = (now - last).days
                return FilterOutcome(
                    False,
                    f"featured {days} days ago with only a {change:.1f}% price change",
                )
    return FilterOutcome(True)


def evaluate_eligibility(
    record: PropertyRecord,
    template: SearchTemplate,
    history: FeatureHistory | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, list[str]]:
    """Combined hard-filter, exclusion and theme gate.

    Returns ``(eligible, reason_if_not, theme_signals)``.
    """

    outcome = apply_hard_filters(record, template)
    if not outcome.passed:
        return False, outcome.reason, []

    outcome = apply_exclusions(record, template, history, now)
    if not outcome.passed:
        return False, outcome.reason, []

    passes, _ratio, signals = theme_fit(record, template.theme_rules)
    if not passes:
        return False, "does not satisfy template theme rules", signals
    return True, "", signals
