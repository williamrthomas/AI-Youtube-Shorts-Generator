"""Evidence, claim/evidence matching, script generation and the editorial gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mpvf.evidence.bundle import bundle_gaps, writer_view
from mpvf.generation.provider import DeterministicProvider
from mpvf.models.domain import Claim, OnScreenText
from mpvf.research.claims import (
    dedupe_claims,
    limit_context_claims,
    listing_claims,
    mark_conflicts,
)
from mpvf.scripting import editorial
from mpvf.scripting.fact_validator import classify_claim, parse_claims, validate_script
from mpvf.scripting.generator import compose_draft, generate_script, preserve_locked_segments


class TestListingClaims:
    def test_every_structured_fact_becomes_a_sourced_claim(self, records):
        record = next(r for r in records if r.town == "Boothbay Harbor")
        claims = listing_claims(record, ["src_1"])
        kinds = {claim.type for claim in claims}
        assert {"price", "status", "location", "measurement", "attribution"} <= kinds
        assert all(claim.sources == ["src_1"] for claim in claims)

    def test_frontage_claim_carries_its_number_and_unit(self, records):
        record = next(r for r in records if r.town == "Boothbay Harbor")
        claim = next(c for c in listing_claims(record, ["s"]) if c.unit == "feet")
        assert claim.value == 190
        assert "approximately" in claim.text

    def test_condo_and_hoa_are_claimed_so_they_can_be_stated(self, records):
        record = next(r for r in records if r.town == "Ogunquit")
        texts = " ".join(claim.text for claim in listing_claims(record, ["s"]))
        assert "condominium" in texts
        assert "association fee" in texts

    def test_duplicate_claims_collapse(self, records):
        record = records[0]
        claims = listing_claims(record, ["s"]) + listing_claims(record, ["s"])
        assert len(dedupe_claims(claims)) == len(claims) // 2


class TestClaimPolicy:
    def _context(self, key: str, text: str, scope: str = "town") -> Claim:
        return Claim(
            property_key=key, text=text, type="context", scope=scope, sources=["s"], confidence=0.8
        )

    def test_context_beats_are_capped_per_property(self):
        claims = [self._context("p1", f"Fact number {index}.") for index in range(5)]
        limited = limit_context_claims(claims, per_property=2)
        eligible = [claim for claim in limited if claim.eligible_for_script]
        assert len(eligible) == 2

    def test_conflicting_numbers_are_flagged_not_reconciled(self):
        claims = [
            Claim(
                property_key="p",
                text="It has 3 bedrooms.",
                type="listing_fact",
                attribute="beds",
                value=3,
                unit="beds",
                sources=["a"],
            ),
            Claim(
                property_key="p",
                text="It has 4 bedrooms.",
                type="listing_fact",
                attribute="beds",
                value=4,
                unit="beds",
                sources=["b"],
            ),
        ]
        marked = mark_conflicts(claims)
        assert all(claim.conflict_state == "unresolved" for claim in marked)
        assert not any(claim.eligible_for_script for claim in marked)

    def test_different_attributes_are_not_a_conflict(self):
        """An asking price and a price reduction are both true prices."""

        claims = [
            Claim(
                property_key="p",
                text="The asking price is $985,000.",
                type="price",
                attribute="asking_price",
                value=985000,
                unit="usd",
                sources=["a"],
            ),
            Claim(
                property_key="p",
                text="The price was reduced by $165,000.",
                type="price",
                attribute="price_reduction",
                value=165000,
                unit="usd",
                sources=["a"],
            ),
        ]
        marked = mark_conflicts(claims)
        assert all(claim.conflict_state == "none" for claim in marked)
        assert all(claim.eligible_for_script for claim in marked)


class TestEvidenceBundle:
    def test_bundle_covers_every_selected_property(self, bundle, template):
        selected = [c for c in bundle.candidates if c.selected]
        assert len(selected) == template.result_count
        for candidate in selected:
            assert bundle.eligible_claims_for(candidate.property_key)
            assert bundle.credit_for(candidate.property_key) is not None

    def test_no_gaps_for_a_complete_bundle(self, bundle, template):
        assert bundle_gaps(bundle, template) == []

    def test_missing_broker_credit_is_a_gap(self, bundle, template):
        for credit in bundle.broker_credits:
            credit.brokerage_name = None
            credit.agent_name = None
        assert any("broker credit" in gap for gap in bundle_gaps(bundle, template))

    def test_writer_view_hides_raw_source_text(self, bundle, template):
        view = writer_view(bundle, template)
        payload = str(view)
        assert "properties" in view
        assert "excerpt" not in payload
        assert view["prohibited_phrases"]

    def test_pronunciation_entries_cover_maine_place_names(self, bundle):
        terms = {entry.term.lower() for entry in bundle.pronunciations}
        assert "ogunquit" in terms

    def test_bundle_hash_is_stable(self, bundle):
        assert bundle.content_hash() == bundle.content_hash()


class TestScriptGeneration:
    def test_deterministic_draft_covers_every_property(self, bundle, template):
        draft = compose_draft(bundle, template)
        property_segments = [s for s in draft.segments if s.type == "property"]
        assert len(property_segments) == template.result_count

    def test_generated_script_has_the_required_structure(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        types = [segment.type for segment in script.segments]
        assert types[0] == "cold_open"
        assert "intro" in types
        assert types[-1] == "disclaimer"
        assert len(script.property_segments()) == template.result_count

    def test_disclaimer_states_when_facts_were_checked(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        disclaimer = script.segments[-1]
        assert "checked on" in disclaimer.spoken_text
        assert bundle.checked_at.strftime("%B") in disclaimer.spoken_text

    def test_prices_are_injected_from_structured_fields(self, bundle, template):
        """A hallucinated price in model prose must not survive (FR-104)."""

        provider = DeterministicProvider()
        script = generate_script(bundle, template, provider)
        for segment in script.property_segments():
            record = next(p for p in bundle.properties if p.property_key == segment.property_key)
            if "$" in segment.spoken_text:
                assert f"${record.price:,}" in segment.spoken_text

    def test_locked_segments_survive_regeneration(self, bundle, template):
        first = generate_script(bundle, template, DeterministicProvider())
        locked = first.property_segments()[0]
        locked.locked = True
        locked.spoken_text = "An operator wrote this line by hand."
        second = generate_script(bundle, template, DeterministicProvider(), previous=first)
        preserved = [s for s in second.segments if s.spoken_text == locked.spoken_text]
        assert preserved
        assert second.version == first.version + 1

    def test_broker_credit_overlay_is_attached(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        for segment in script.property_segments():
            assert any(item.kind == "credit" for item in segment.on_screen_text)


class TestFactValidation:
    def test_claim_classification(self):
        assert classify_claim("The asking price is $895,000.") == "numeric"
        assert classify_claim("Listed by Camden Harbour Realty.") == "attribution"
        assert classify_claim("It is a five minute walk to the beach.") == "proximity"
        assert classify_claim("The house was built in 1884.") == "historical"
        assert classify_claim("The light in the front room is lovely.") is None

    def test_a_generated_script_validates_against_its_own_evidence(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        outcome = validate_script(script, bundle)
        assert outcome.passed, [finding.message for finding in outcome.findings]

    def test_an_invented_number_fails(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        segment = script.property_segments()[0]
        segment.spoken_text += " The dock is exactly 412 feet long."
        outcome = validate_script(script, bundle)
        assert not outcome.passed
        assert any("412" in finding.message for finding in outcome.findings)

    def test_prohibited_promotional_claims_fail(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        script.segments[1].spoken_text += " This is the best investment on the coast."
        outcome = validate_script(script, bundle)
        assert any(finding.check == "prohibited_claim" for finding in outcome.findings)

    def test_town_history_narrated_as_house_history_fails(self, bundle, template):
        key = bundle.candidates[0].property_key
        town_claim = Claim(
            property_key=key,
            text="Camden was incorporated in 1791.",
            type="history",
            scope="town",
            sources=["s"],
            confidence=0.8,
        )
        bundle.claims.append(town_claim)
        script = generate_script(bundle, template, DeterministicProvider())
        segment = next(s for s in script.property_segments() if s.property_key == key)
        segment.evidence_refs.append(town_claim.claim_id)
        segment.spoken_text += " The house was built the year the town was incorporated."
        outcome = validate_script(script, bundle)
        assert any(finding.check == "history_scope" for finding in outcome.findings)

    def test_a_missing_property_segment_fails(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        script.segments = [s for s in script.segments if s.type != "property"][:3] + [
            s for s in script.segments if s.type == "property"
        ][:-1]
        outcome = validate_script(script, bundle)
        assert any(finding.check == "property_missing" for finding in outcome.findings)

    def test_citation_map_is_built(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        validate_script(script, bundle)
        assert script.citation_map
        assert all(isinstance(refs, list) for refs in script.citation_map.values())


class TestEditorialGate:
    def _script(self, bundle, template):
        return generate_script(bundle, template, DeterministicProvider())

    def test_slop_phrases_are_penalized(self, bundle, template):
        script = self._script(bundle, template)
        clean, _ = editorial.score_natural_voice(script)
        for segment in script.property_segments():
            segment.spoken_text = (
                "This truly special dream home is nestled by the sea. " + segment.spoken_text
            )
        dirty, findings = editorial.score_natural_voice(script)
        assert dirty < clean
        assert any(finding.check == "slop_phrase" for finding in findings)

    def test_repeated_openings_fail_variety(self, bundle, template):
        script = self._script(bundle, template)
        for segment in script.property_segments():
            segment.spoken_text = "Next up we have a house. " + segment.spoken_text
        _score, findings = editorial.score_variety(script)
        assert any(finding.check == "repeated_openings" for finding in findings)

    def test_identical_transitions_fail(self, bundle, template):
        script = self._script(bundle, template)
        for segment in script.segments:
            if segment.type == "transition":
                segment.spoken_text = "Moving on."
        _score, findings = editorial.score_variety(script)
        assert any(finding.check == "identical_transitions" for finding in findings)

    def test_engagement_bait_is_an_error(self, bundle, template):
        script = self._script(bundle, template)
        script.segments[0].spoken_text += " Stay until the end, you won't believe number one."
        _score, findings = editorial.score_natural_voice(script)
        assert any(finding.check == "engagement_bait" for finding in findings)

    def test_all_perfect_lineup_fails_factual_restraint(self, bundle, template):
        script = self._script(bundle, template)
        for segment in script.property_segments():
            segment.spoken_text = f"A house in {segment.property_key[:5]} priced at $1."
        _score, findings = editorial.score_factual_restraint(script, bundle)
        assert any(finding.check == "everything_is_perfect" for finding in findings)

    def test_adjectives_outnumbering_facts_is_flagged(self, bundle, template):
        script = self._script(bundle, template)
        script.property_segments()[
            0
        ].spoken_text = "A gorgeous, elegant, immaculate, pristine, magnificent home."
        _score, findings = editorial.score_specificity(script)
        assert any(finding.check == "adjectives_outnumber_facts" for finding in findings)

    def test_word_count_outside_template_range_fails_pacing(self, bundle, template):
        script = self._script(bundle, template)
        _score, findings = editorial.score_pacing(script, 5000, (4000, 6000))
        assert any(finding.check == "word_count_out_of_range" for finding in findings)

    def test_the_rubric_totals_a_hundred(self):
        assert sum(editorial.SUBSCORE_MAX.values()) == 100

    def test_gate_produces_a_score_and_findings(self, bundle, template):
        result = editorial.evaluate(
            self._script(bundle, template), bundle, template.target_words, template.word_range
        )
        assert 0 <= result.total <= 100
        assert set(result.subscores) == set(editorial.SUBSCORE_MAX)


class TestSegmentLocking:
    def test_preserve_locked_segments_is_a_no_op_without_locks(self, bundle, template):
        first = generate_script(bundle, template, DeterministicProvider())
        second = generate_script(bundle, template, DeterministicProvider())
        merged = preserve_locked_segments(first, second)
        assert merged is second

    def test_a_locked_segment_keeps_its_overlays(self, bundle, template):
        first = generate_script(bundle, template, DeterministicProvider())
        target = first.property_segments()[0]
        target.locked = True
        target.on_screen_text = [OnScreenText(kind="callout", text="Hand written")]
        second = generate_script(bundle, template, DeterministicProvider(), previous=first)
        restored = next(
            s
            for s in second.segments
            if s.property_key == target.property_key and s.type == "property"
        )
        assert any(item.text == "Hand written" for item in restored.on_screen_text)


class TestStaleness:
    def test_parse_claims_ignores_the_disclaimer(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        parsed = parse_claims(script)
        disclaimer_id = script.segments[-1].segment_id
        assert all(claim.segment_id != disclaimer_id for claim in parsed)

    def test_stale_evidence_is_reported_by_qa(self, bundle, template):
        from mpvf.qa.checks import factual_checks

        bundle.checked_at = datetime.now(UTC) - timedelta(hours=30)
        script = generate_script(bundle, template, DeterministicProvider())
        findings, _outcome = factual_checks(script, bundle)
        assert any(finding.check == "stale_verification" for finding in findings)


class TestPriceInjection:
    """FR-104: a price in prose is always replaced by the verified figure."""

    def _bundle_with_price(self, amount: int):
        from mpvf.models.domain import Claim

        class OneClaim:
            def eligible_claims_for(self, _key):
                return [
                    Claim(
                        property_key="p",
                        text=f"The asking price is ${amount:,}.",
                        type="price",
                        attribute="asking_price",
                        value=amount,
                        unit="usd",
                        sources=["s"],
                    )
                ]

        return OneClaim()

    def test_replaces_a_plain_amount_without_eating_the_period(self):
        from mpvf.scripting.generator import _inject_structured_facts

        text = _inject_structured_facts(
            "The asking price is $1,000,000. It has five bedrooms.",
            "p",
            self._bundle_with_price(985000),
        )
        assert text == "The asking price is $985,000. It has five bedrooms."

    def test_replaces_shorthand_millions(self):
        from mpvf.scripting.generator import _inject_structured_facts

        text = _inject_structured_facts(
            "Asking about $1.2 million.", "p", self._bundle_with_price(985000)
        )
        assert text == "Asking about $985,000."

    def test_replaces_k_shorthand(self):
        from mpvf.scripting.generator import _inject_structured_facts

        text = _inject_structured_facts(
            "Listed at $750K today.", "p", self._bundle_with_price(985000)
        )
        assert text == "Listed at $985,000 today."

    def test_leaves_text_alone_when_there_is_no_price_claim(self):
        from mpvf.scripting.generator import _inject_structured_facts

        class NoClaims:
            def eligible_claims_for(self, _key):
                return []

        assert _inject_structured_facts("Costs $5.", "p", NoClaims()) == "Costs $5."
