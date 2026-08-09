"""Candidate scoring and selection with geographic diversity (§7.5)."""

from __future__ import annotations

from datetime import UTC, datetime

from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import Candidate, PropertyRecord, ScoreBreakdown
from mpvf.selection.theme import theme_fit

_STORY_MARKERS = (
    "sea captain",
    "national register",
    "historic",
    "lighthouse",
    "converted",
    "architect",
    "original",
    "restored",
    "renovated",
    "custom",
    "handcrafted",
    "island",
    "working farm",
    "post and beam",
    "shipbuilder",
    "granite",
)


def visual_strength(record: PropertyRecord) -> float:
    """0..1 from image count and variety signals available pre-download (FR-043)."""

    images = record.canonical.usable_image_count()
    count_score = min(1.0, images / 24)
    variety_bonus = 0.0
    blob = (record.canonical.description_text or "").lower()
    if any(word in blob for word in ("aerial", "drone")):
        variety_bonus += 0.1
    if any(word in blob for word in ("view", "sunset", "shoreline", "dock")):
        variety_bonus += 0.1
    return round(min(1.0, count_score * 0.8 + variety_bonus), 3)


def story_value(record: PropertyRecord) -> float:
    """0..1 for architectural distinction, history, or a price contrast (FR-044)."""

    blob = f"{record.canonical.description_text or ''}".lower()
    hits = sum(1 for marker in _STORY_MARKERS if marker in blob)
    score = min(0.7, hits * 0.12)

    year = record.canonical.year_built
    if year and year < 1900:
        score += 0.15
    if record.canonical.acres and record.canonical.acres >= 10:
        score += 0.05
    price = record.canonical.price
    sqft = record.canonical.square_feet
    if price and sqft and sqft > 0:
        per_sqft = price / sqft
        if per_sqft < 250:
            score += 0.10  # a genuine value contrast is a story
    if record.canonical.previous_price and price and price < record.canonical.previous_price:
        score += 0.05
    return round(min(1.0, score), 3)


def evidence_quality(record: PropertyRecord) -> float:
    """0..1 from verification confidence and source agreement (FR-040)."""

    verification = record.verification
    if verification is None:
        return 0.0
    if verification.blocked:
        return 0.0
    base = verification.confidence
    if len({obs.source for obs in record.observations}) >= 2:
        base = min(1.0, base + 0.05)
    return round(base, 3)


def freshness(record: PropertyRecord, now: datetime | None = None) -> float:
    """0..1 where a listing seen for the first time today scores highest."""

    now = now or datetime.now(UTC)
    listed = record.canonical.listed_at or record.first_seen_at
    if listed.tzinfo is None:
        listed = listed.replace(tzinfo=UTC)
    age_days = max(0.0, (now - listed).total_seconds() / 86400)
    if age_days <= 7:
        return 1.0
    if age_days >= 180:
        return 0.1
    return round(max(0.1, 1.0 - (age_days - 7) / 173 * 0.9), 3)


def asset_completeness(record: PropertyRecord) -> float:
    """0..1 for having enough structured facts to build a chapter (FR-055)."""

    canonical = record.canonical
    required = (
        canonical.price,
        canonical.beds,
        canonical.baths,
        canonical.square_feet,
        canonical.city,
        canonical.brokerage_name,
    )
    present = sum(1 for value in required if value not in (None, ""))
    bonus = 0.1 if canonical.usable_image_count() >= 12 else 0.0
    return round(min(1.0, present / len(required) + bonus), 3)


def score_candidate(
    record: PropertyRecord,
    template: SearchTemplate,
    now: datetime | None = None,
) -> tuple[ScoreBreakdown, list[str]]:
    """Weighted 0..100 score plus the theme signals that produced it."""

    weights = template.scoring
    _passes, fit_ratio, signals = theme_fit(record, template.theme_rules)

    breakdown = ScoreBreakdown(
        theme_fit=round(weights.theme_fit * fit_ratio, 2),
        visual_strength=round(weights.visual_strength * visual_strength(record), 2),
        story_value=round(weights.story_value * story_value(record), 2),
        evidence_quality=round(weights.evidence_quality * evidence_quality(record), 2),
        freshness=round(weights.freshness * freshness(record, now), 2),
        geographic_diversity=0.0,  # assigned during selection
        asset_completeness=round(weights.asset_completeness * asset_completeness(record), 2),
    )
    return breakdown, signals


def explain(candidate: Candidate, template: SearchTemplate) -> str:
    """Plain-language selection rationale shown in the dashboard (FR-041)."""

    record = candidate.property
    scores = candidate.scores
    bits: list[str] = []
    top = sorted(
        scores.model_dump().items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]
    label = {
        "theme_fit": "fits the template theme",
        "visual_strength": "strong image set",
        "story_value": "has a story worth telling",
        "evidence_quality": "cleanly verified",
        "freshness": "recently listed",
        "geographic_diversity": "adds geographic spread",
        "asset_completeness": "complete structured facts",
    }
    for name, value in top:
        if value > 0:
            bits.append(f"{label.get(name, name)} ({value:.1f}/{getattr(template.scoring, name)})")
    signals = ", ".join(candidate.theme_signals[:4]) or "no explicit theme signals"
    price = f"${record.price:,}" if record.price else "price unknown"
    return (
        f"{record.town}, {price}. Scored {candidate.total_score:.1f}/100: "
        + "; ".join(bits)
        + f". Theme signals: {signals}."
    )


def build_candidates(
    records: list[PropertyRecord],
    template: SearchTemplate,
    now: datetime | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for record in records:
        breakdown, signals = score_candidate(record, template, now)
        candidate = Candidate(
            property=record,
            scores=breakdown,
            total_score=breakdown.total(),
            theme_signals=signals,
        )
        candidates.append(candidate)
    return candidates


def select_lineup(
    candidates: list[Candidate],
    template: SearchTemplate,
    now: datetime | None = None,
) -> list[Candidate]:
    """Pick the lineup, enforcing geographic diversity (FR-045, FR-046).

    Selection is greedy on total score with a diversity bonus for towns not yet
    represented and a penalty for repeats. Manually locked candidates are taken
    first and never displaced (FR-047).
    """

    weights = template.scoring
    eligible = [c for c in candidates if c.eligible and c.exclusion_reason is None]
    locked = [c for c in eligible if c.manual_lock]
    pool = [c for c in eligible if not c.manual_lock]

    chosen: list[Candidate] = []
    towns: dict[str, int] = {}

    def diversity_points(candidate: Candidate) -> float:
        if not template.require_geographic_diversity:
            return weights.geographic_diversity * 0.5
        seen = towns.get(candidate.property.town.lower(), 0)
        if seen == 0:
            return float(weights.geographic_diversity)
        return max(0.0, weights.geographic_diversity - seen * weights.geographic_diversity)

    def commit(candidate: Candidate) -> None:
        candidate.scores.geographic_diversity = round(diversity_points(candidate), 2)
        candidate.total_score = candidate.scores.total()
        town = candidate.property.town.lower()
        towns[town] = towns.get(town, 0) + 1
        chosen.append(candidate)

    for candidate in sorted(locked, key=lambda c: c.total_score, reverse=True):
        commit(candidate)

    while pool and len(chosen) < template.result_count + template.alternates_count:
        pool.sort(key=lambda c: c.total_score + diversity_points(c), reverse=True)
        commit(pool.pop(0))

    for index, candidate in enumerate(chosen):
        if index < template.result_count:
            candidate.selected = True
            candidate.alternate = False
            candidate.rank = template.result_count - index  # countdown ordering
        else:
            candidate.selected = False
            candidate.alternate = True
            candidate.rank = None
        candidate.selection_reason = explain(candidate, template)

    for candidate in candidates:
        if candidate not in chosen and candidate.exclusion_reason is None:
            candidate.exclusion_reason = "not in top lineup"
    return chosen


def lineup_quality(selected: list[Candidate]) -> float:
    """Mean score of the selected lineup, used by the skip gate (§2.2)."""

    if not selected:
        return 0.0
    return round(sum(c.total_score for c in selected) / len(selected), 2)
