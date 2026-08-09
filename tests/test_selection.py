"""Filters, theme rules, scoring, diversity and verification (§19.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mpvf.models.domain import PropertyRecord
from mpvf.selection.filters import (
    FeatureHistory,
    apply_exclusions,
    apply_hard_filters,
    evaluate_eligibility,
)
from mpvf.selection.scoring import (
    build_candidates,
    freshness,
    lineup_quality,
    select_lineup,
    visual_strength,
)
from mpvf.selection.theme import evaluate_signals, theme_fit
from mpvf.selection.verification import (
    build_verification,
    compare_observations,
    verification_confidence,
)


def _by_town(records: list[PropertyRecord], town: str) -> PropertyRecord:
    return next(record for record in records if record.town == town)


class TestHardFilters:
    def test_active_coastal_house_passes(self, records, template):
        assert apply_hard_filters(_by_town(records, "Boothbay Harbor"), template).passed

    def test_price_ceiling_is_enforced(self, records, template):
        record = _by_town(records, "Boothbay Harbor")
        template.hard_filters.price_max = 500000
        outcome = apply_hard_filters(record, template)
        assert not outcome.passed
        assert "above ceiling" in outcome.reason

    def test_land_is_excluded(self, records, template):
        outcome = apply_exclusions(_by_town(records, "Alna"), template)
        assert not outcome.passed
        assert "land-only" in outcome.reason

    def test_pending_is_excluded(self, records, template):
        outcome = apply_hard_filters(_by_town(records, "Rockport"), template)
        assert not outcome.passed
        assert "pending" in outcome.reason

    def test_thin_image_sets_are_rejected(self, records, template):
        record = _by_town(records, "Boothbay Harbor")
        record.canonical.image_urls = record.canonical.image_urls[:3]
        outcome = apply_hard_filters(record, template)
        assert not outcome.passed
        assert "images" in outcome.reason

    def test_year_built_window(self, records, template):
        template.hard_filters.max_year_built = 1899
        assert not apply_hard_filters(_by_town(records, "Boothbay Harbor"), template).passed
        assert apply_hard_filters(_by_town(records, "Camden"), template).passed


class TestRepeatExclusion:
    def test_recently_featured_property_is_excluded(self, records, template):
        record = _by_town(records, "Camden")
        history = FeatureHistory(
            last_featured_at=datetime.now(UTC) - timedelta(days=20),
            last_featured_price=record.canonical.price,
        )
        outcome = apply_exclusions(record, template, history)
        assert not outcome.passed
        assert "featured" in outcome.reason

    def test_a_big_price_cut_lets_it_back_in(self, records, template):
        record = _by_town(records, "Camden")
        history = FeatureHistory(
            last_featured_at=datetime.now(UTC) - timedelta(days=20),
            last_featured_price=int(record.canonical.price * 1.4),
        )
        assert apply_exclusions(record, template, history).passed

    def test_outside_the_window_it_is_eligible_again(self, records, template):
        record = _by_town(records, "Camden")
        history = FeatureHistory(
            last_featured_at=datetime.now(UTC) - timedelta(days=400),
            last_featured_price=record.canonical.price,
        )
        assert apply_exclusions(record, template, history).passed


class TestThemeRules:
    def test_owned_frontage_is_detected_as_a_signal(self, records):
        signals = evaluate_signals(_by_town(records, "Boothbay Harbor"))
        assert "owned_ocean_frontage" in signals
        assert "owned_frontage_measured" in signals
        assert "verified_coastal_village" in signals

    def test_condo_and_deeded_access_signals(self, records):
        signals = evaluate_signals(_by_town(records, "Ogunquit"))
        assert "condo" in signals
        assert "deeded_beach_access" in signals

    def test_price_reduction_signal(self, records):
        assert "price_reduced" in evaluate_signals(_by_town(records, "Camden"))

    def test_inland_land_fails_a_coastal_theme(self, records, template):
        passes, ratio, _signals = theme_fit(_by_town(records, "Alna"), template.theme_rules)
        assert not passes
        assert ratio == 0.0

    def test_theme_fit_ratio_rewards_multiple_signals(self, records, template):
        _passes, single, _ = theme_fit(_by_town(records, "Camden"), template.theme_rules)
        _passes, many, _ = theme_fit(_by_town(records, "Boothbay Harbor"), template.theme_rules)
        assert many >= single

    def test_none_of_blocks_a_property(self, records, template):
        template.theme_rules.none_of = ["condo"]
        passes, _ratio, _signals = theme_fit(_by_town(records, "Ogunquit"), template.theme_rules)
        assert not passes


class TestScoring:
    def test_scores_sum_within_a_hundred(self, records, template):
        candidates = build_candidates(records, template)
        assert all(0 <= candidate.total_score <= 100 for candidate in candidates)

    def test_a_richer_image_set_scores_higher(self, records):
        rich = _by_town(records, "Boothbay Harbor")
        thin = _by_town(records, "Rockport")
        assert visual_strength(rich) > visual_strength(thin)

    def test_freshness_decays_with_age(self, records):
        record = _by_town(records, "Camden")
        record.first_seen_at = datetime.now(UTC)
        fresh = freshness(record)
        record.first_seen_at = datetime.now(UTC) - timedelta(days=200)
        assert freshness(record) < fresh

    def test_unverified_candidates_score_zero_on_evidence(self, records, template):
        record = _by_town(records, "Camden")
        record.verification = None
        candidates = build_candidates([record], template)
        assert candidates[0].scores.evidence_quality == 0.0


class TestLineupSelection:
    def _eligible(self, records, template):
        return [record for record in records if evaluate_eligibility(record, template)[0]]

    def test_selects_the_requested_count_plus_alternates(self, records, template):
        candidates = build_candidates(self._eligible(records, template), template)
        select_lineup(candidates, template)
        assert sum(1 for c in candidates if c.selected) == template.result_count
        assert sum(1 for c in candidates if c.alternate) == template.alternates_count

    def test_ranks_count_down_to_one(self, records, template):
        candidates = build_candidates(self._eligible(records, template), template)
        select_lineup(candidates, template)
        ranks = sorted(c.rank for c in candidates if c.selected)
        assert ranks == list(range(1, template.result_count + 1))

    def test_geographic_diversity_is_enforced(self, records, template):
        eligible = self._eligible(records, template)
        candidates = build_candidates(eligible, template)
        select_lineup(candidates, template)
        towns = [c.property.town for c in candidates if c.selected]
        assert len(set(towns)) == len(towns)

    def test_a_manual_lock_is_never_displaced(self, records, template):
        eligible = self._eligible(records, template)
        candidates = build_candidates(eligible, template)
        weakest = min(candidates, key=lambda c: c.total_score)
        weakest.manual_lock = True
        select_lineup(candidates, template)
        assert weakest.selected

    def test_every_selected_candidate_explains_itself(self, records, template):
        candidates = build_candidates(self._eligible(records, template), template)
        select_lineup(candidates, template)
        for candidate in candidates:
            if candidate.selected:
                assert "Scored" in candidate.selection_reason

    def test_lineup_quality_is_the_mean_score(self, records, template):
        candidates = build_candidates(self._eligible(records, template), template)
        chosen = select_lineup(candidates, template)
        selected = [c for c in chosen if c.selected]
        expected = round(sum(c.total_score for c in selected) / len(selected), 2)
        assert lineup_quality(selected) == expected


class TestVerification:
    def test_matching_sources_verify_cleanly(self, records):
        record = _by_town(records, "Camden")
        result = build_verification(
            record.property_key,
            record.canonical,
            record.canonical.model_copy(deep=True),
            "brokerage",
        )
        assert result.verified
        assert result.confidence > 0.9

    def test_status_disagreement_blocks(self, records):
        record = _by_town(records, "Camden")
        other = record.canonical.model_copy(deep=True)
        other.status = "sold"
        result = build_verification(record.property_key, record.canonical, other, "brokerage")
        assert not result.verified
        assert any("status" in conflict for conflict in result.blocking_conflicts)

    def test_small_price_drift_is_tolerated(self, records):
        record = _by_town(records, "Camden")
        other = record.canonical.model_copy(deep=True)
        other.price = int(record.canonical.price * 1.01)
        result = build_verification(record.property_key, record.canonical, other, "brokerage")
        assert result.verified

    def test_large_price_drift_blocks(self, records):
        record = _by_town(records, "Camden")
        other = record.canonical.model_copy(deep=True)
        other.price = int(record.canonical.price * 1.2)
        result = build_verification(record.property_key, record.canonical, other, "brokerage")
        assert not result.verified

    def test_no_verification_source_is_not_verified(self, records):
        record = _by_town(records, "Camden")
        result = build_verification(record.property_key, record.canonical, None)
        assert not result.verified
        assert result.blocking_conflicts == ["unverified"]

    def test_confidence_falls_when_fields_disagree(self, records):
        record = _by_town(records, "Camden")
        agree = compare_observations(record.canonical, record.canonical.model_copy(deep=True))
        other = record.canonical.model_copy(deep=True)
        other.beds = 99
        other.square_feet = 10
        disagree = compare_observations(record.canonical, other)
        assert verification_confidence(disagree) < verification_confidence(agree)
