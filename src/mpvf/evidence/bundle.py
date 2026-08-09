"""Evidence-bundle assembly (§7.8).

The bundle is the writer's entire world. If a fact is not in here, it may not
be spoken. Building it is deliberately boring: gather, tier, deduplicate, mark
conflicts, cap context beats, attach pronunciation and broker credits.
"""

from __future__ import annotations

from datetime import datetime

from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import (
    AssetRecord,
    BrokerCredit,
    Candidate,
    Claim,
    EvidenceBundle,
    PronunciationEntry,
    PropertyRecord,
    SourceRecord,
    utcnow,
)
from mpvf.research.claims import dedupe_claims, limit_context_claims, mark_conflicts
from mpvf.speech.pronunciation import PronunciationLexicon

PROHIBITED_PHRASES = (
    "best investment",
    "guaranteed rental income",
    "once-in-a-lifetime",
    "can't lose",
    "will only go up",
    "steal of a deal",
    "guaranteed appreciation",
)


def broker_credit_for(record: PropertyRecord) -> BrokerCredit:
    """Credit comes from the verification listing, never an inferred company (FR-035)."""

    verification = record.verification
    observation = verification.observation if verification and verification.observation else None
    source = observation or record.canonical
    return BrokerCredit(
        property_key=record.property_key,
        brokerage_name=source.brokerage_name or record.canonical.brokerage_name,
        agent_name=source.agent_name or record.canonical.agent_name,
        verification_url=(verification.verification_url if verification else None)
        or record.canonical.listing_url,
    )


def pronunciation_entries(
    records: list[PropertyRecord], lexicon: PronunciationLexicon
) -> list[PronunciationEntry]:
    """Collect lexicon entries for every place name that will be spoken (FR-113)."""

    terms: set[str] = set()
    for record in records:
        if record.city:
            terms.add(record.city)
        water_body = record.canonical.waterfront.water_body
        if water_body:
            terms.update(water_body.split())
        address = record.canonical.raw_address or ""
        terms.update(part for part in address.replace(",", " ").split() if len(part) > 5)

    entries: list[PronunciationEntry] = []
    seen: set[str] = set()
    for term in sorted(terms):
        entry = lexicon.lookup(term)
        if entry and entry.term.lower() not in seen:
            entries.append(entry)
            seen.add(entry.term.lower())
    return entries


def episode_claims(
    checked_at: datetime, candidates: list[Candidate], sources: list[SourceRecord]
) -> list[Claim]:
    """Facts about the episode itself that the pipeline can attest to.

    The intro and closing legitimately describe our own process ("checked this
    morning", "credited to the listing brokerage"). Those statements still need
    evidence, so the run supplies it rather than the writer asserting it.
    """

    selected = [candidate for candidate in candidates if candidate.selected]
    verification_sources = [
        source.source_id for source in sources if source.type in {"verification", "discovery"}
    ]
    stamp = checked_at.strftime("%B %d, %Y")
    return [
        Claim(
            property_key=None,
            text=f"Every price and status in this episode was checked on {stamp}.",
            type="status",
            attribute="episode_checked_at",
            value=checked_at.isoformat(),
            sources=verification_sources,
            confidence=1.0,
            scope="region",
        ),
        Claim(
            property_key=None,
            text="Each listing is credited to the brokerage that brought it to market.",
            type="attribution",
            attribute="episode_attribution",
            sources=verification_sources,
            confidence=1.0,
            scope="region",
        ),
        Claim(
            property_key=None,
            text=f"This episode features {len(selected)} properties, all in Maine.",
            type="location",
            attribute="episode_lineup",
            value=len(selected),
            sources=verification_sources,
            confidence=1.0,
            scope="region",
        ),
    ]


def build_bundle(
    run_id: str,
    template: SearchTemplate,
    candidates: list[Candidate],
    claims: list[Claim],
    sources: list[SourceRecord],
    assets: list[AssetRecord],
    lexicon: PronunciationLexicon,
    checked_at: datetime | None = None,
) -> EvidenceBundle:
    """Assemble and normalize the bundle for the selected lineup."""

    selected = [candidate for candidate in candidates if candidate.selected or candidate.alternate]
    records = [candidate.property for candidate in selected]
    keys = {record.property_key for record in records}

    stamp = checked_at or utcnow()
    scoped_claims = [
        claim for claim in claims if claim.property_key in keys or claim.property_key is None
    ]
    scoped_claims.extend(episode_claims(stamp, selected, sources))
    scoped_claims = mark_conflicts(limit_context_claims(dedupe_claims(scoped_claims)))

    referenced_sources = {claim_source for claim in scoped_claims for claim_source in claim.sources}
    scoped_sources = [
        source
        for source in sources
        if source.source_id in referenced_sources or source.property_key in keys
    ]

    bundle = EvidenceBundle(
        run_id=run_id,
        template_slug=template.slug,
        template_version=template.version,
        checked_at=stamp,
        properties=records,
        candidates=selected,
        sources=scoped_sources,
        claims=scoped_claims,
        assets=[asset for asset in assets if asset.property_key in keys],
        pronunciations=pronunciation_entries(records, lexicon),
        broker_credits=[broker_credit_for(record) for record in records],
        prohibited_claims=list(PROHIBITED_PHRASES),
        uncertain_claims=[
            claim.text for claim in scoped_claims if claim.conflict_state == "unresolved"
        ],
    )
    return bundle


def bundle_gaps(bundle: EvidenceBundle, template: SearchTemplate) -> list[str]:
    """Problems that should block scripting rather than surface later (FR-072)."""

    problems: list[str] = []
    selected = [candidate for candidate in bundle.candidates if candidate.selected]
    if len(selected) < template.result_count:
        problems.append(
            f"{len(selected)} selected properties, template requires {template.result_count}"
        )

    for candidate in selected:
        key = candidate.property_key
        eligible = bundle.eligible_claims_for(key)
        if not any(claim.type == "price" for claim in eligible):
            problems.append(f"{key}: no evidence-backed price")
        if not any(claim.type == "status" for claim in eligible):
            problems.append(f"{key}: no evidence-backed status")
        if not any(claim.type == "location" for claim in eligible):
            problems.append(f"{key}: no evidence-backed location")
        credit = bundle.credit_for(key)
        if credit is None or not (credit.brokerage_name or credit.agent_name):
            problems.append(f"{key}: no broker credit from the verification source")
        assets = bundle.assets_for(key)
        if len(assets) < 6:
            problems.append(f"{key}: only {len(assets)} approved images")
    return problems


def claim_index(bundle: EvidenceBundle) -> dict[str, Claim]:
    return {claim.claim_id: claim for claim in bundle.claims}


def writer_view(bundle: EvidenceBundle, template: SearchTemplate) -> dict:
    """The exact, minimal payload handed to the script generator.

    Deliberately excludes raw source text: the writer works from validated
    claims, not from prose it might paraphrase into a new fact.
    """

    selected = sorted(
        [c for c in bundle.candidates if c.selected],
        key=lambda c: c.rank or 0,
    )
    properties = []
    for candidate in selected:
        record = candidate.property
        key = record.property_key
        credit = bundle.credit_for(key)
        properties.append(
            {
                "property_key": key,
                "rank": candidate.rank,
                "town": record.town,
                "price": record.price,
                "score": candidate.total_score,
                "theme_signals": candidate.theme_signals,
                "claims": [
                    {
                        "claim_id": claim.claim_id,
                        "text": claim.text,
                        "type": claim.type,
                        "scope": claim.scope,
                        "confidence": claim.confidence,
                    }
                    for claim in bundle.eligible_claims_for(key)
                ],
                "broker_credit": credit.spoken() if credit else "",
                "image_categories": sorted({asset.category for asset in bundle.assets_for(key)}),
            }
        )
    return {
        "template": {
            "name": template.name,
            "promise": template.editorial_promise,
            "tone": template.tone,
            "result_count": template.result_count,
            "target_words": template.target_words,
            "max_segment_words": template.max_segment_words,
        },
        "checked_at": bundle.checked_at.isoformat(),
        "properties": properties,
        "prohibited_phrases": bundle.prohibited_claims,
        "uncertain_claims": bundle.uncertain_claims,
    }
