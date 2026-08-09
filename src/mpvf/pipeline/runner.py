"""The pipeline: stage implementations and the run orchestrator.

Every stage reads its inputs from the artifact store, writes one artifact, and
is independently rerunnable. Stages never call each other directly; the runner
sequences them and owns all state transitions.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mpvf.adapters.base import AccessChallenge, SelectorFailure
from mpvf.adapters.registry import get_discovery_adapter, get_verification_adapter
from mpvf.config.settings import Settings
from mpvf.config.templates import SearchTemplate, TemplateRegistry
from mpvf.evidence.bundle import build_bundle, bundle_gaps
from mpvf.generation.provider import build_provider
from mpvf.media.classify import classify_and_score, coverage_report, select_for_property
from mpvf.media.download import AssetDownloader, deduplicate
from mpvf.models.db import (
    Database,
    ListingRow,
    PropertyRow,
    PublicationRow,
    RenderRow,
    RunCandidateRow,
    RunRow,
    ScriptRow,
    StageRow,
)
from mpvf.models.domain import (
    AssetRecord,
    Candidate,
    CaptionCue,
    Claim,
    DiscoveryQuery,
    EvidenceBundle,
    ListingObservation,
    NarrationSegment,
    PropertyRecord,
    QAReport,
    ScenePlan,
    Script,
    SourceRecord,
    utcnow,
)
from mpvf.normalization.dedup import build_property_records
from mpvf.observability.logging import RunLogger, configure_logging
from mpvf.pipeline.artifacts import ArtifactStore, hash_inputs
from mpvf.pipeline.context import RunContext
from mpvf.pipeline.stages import SkipEpisode, StageError, StageResult, StageSpec, registry
from mpvf.pipeline.state import RunState, assert_transition, is_failure
from mpvf.publish import metadata as metadata_builder
from mpvf.publish.package import build_package
from mpvf.publish.youtube import UploadRequest, build_publisher, idempotency_key
from mpvf.qa import checks as qa_checks
from mpvf.render import design
from mpvf.render.ffmpeg import (
    FFmpegUnavailable,
    RenderFailed,
    RenderInputs,
    build_command,
    run_render,
)
from mpvf.render.scene_plan import build_scene_plan
from mpvf.render.thumbnails import build_variants, choose_default, write_variants
from mpvf.research.claims import extract_context_claims, listing_claims
from mpvf.research.planner import plan_for_lineup
from mpvf.research.sources import SourceCapture
from mpvf.scripting.editorial import evaluate as editorial_evaluate
from mpvf.scripting.fact_validator import validate_script
from mpvf.scripting.generator import generate_script, script_to_markdown
from mpvf.selection.filters import FeatureHistory, evaluate_eligibility
from mpvf.selection.scoring import build_candidates, lineup_quality, select_lineup
from mpvf.selection.verification import build_verification
from mpvf.speech.captions import build_cues, write_captions
from mpvf.speech.pronunciation import PronunciationLexicon
from mpvf.speech.tts import Narrator, build_engine, concatenate_wavs

# --------------------------------------------------------------------------
# Stage: discovery
# --------------------------------------------------------------------------


@registry.register(
    "discover",
    order=10,
    produces=RunState.DISCOVERING,
    failure_state=RunState.DISCOVERY_FAILED,
    describe="Fetch listing cards from the template's configured search sources",
)
def stage_discover(context: RunContext) -> dict[str, Any]:
    template = context.template
    logger = context.stage_logger("discover")
    query = DiscoveryQuery(
        template_slug=template.slug,
        search_urls=template.discovery.search_urls,
        max_pages=template.discovery.max_pages,
        max_cards_per_page=template.discovery.max_cards_per_page,
        state=template.hard_filters.state,
    )

    adapters = [template.discovery.adapter, *template.discovery.fallback_adapters]
    observations: list[ListingObservation] = []
    attempted: list[dict[str, Any]] = []

    for name in adapters:
        try:
            adapter = get_discovery_adapter(
                name,
                context.settings,
                debug_dir=context.store.dir("pages"),
                directory=context.get("fixture_dir"),
                session=context.get("browser_session"),
            )
        except KeyError as exc:
            attempted.append({"adapter": name, "outcome": "unknown_adapter"})
            logger.warning("unknown_adapter", str(exc), adapter=name)
            continue

        try:
            cards = adapter.discover(query)
            observations.extend(adapter.parse_card(card) for card in cards)
            attempted.append({"adapter": name, "outcome": "ok", "cards": len(cards)})
            logger.info("discovered", f"{name}: {len(cards)} cards", adapter=name, cards=len(cards))
            if observations:
                break
        except AccessChallenge as exc:
            # FR-015: stop, report, try the fallback. Never bypass.
            attempted.append({"adapter": name, "outcome": "access_challenge", "detail": exc.detail})
            logger.warning(
                "access_challenge",
                f"{name} presented an access challenge; not attempting to bypass it",
                adapter=name,
                detail=exc.detail,
            )
        except SelectorFailure as exc:
            attempted.append(
                {"adapter": name, "outcome": "selector_failure", "artifact": exc.artifact_path}
            )
            logger.error(
                "selector_failure",
                f"{name} page structure no longer matches the adapter",
                adapter=name,
                selector=exc.selector,
                artifact=exc.artifact_path,
            )
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    context.store.write_json(
        "discovery",
        "observations.json",
        {
            "attempted": attempted,
            "observations": [obs.model_dump(mode="json") for obs in observations],
        },
    )
    if not observations:
        raise StageError(
            "no_listings_discovered",
            "no discovery source returned any listings",
            RunState.DISCOVERY_FAILED,
            {"attempted": attempted},
        )
    context.put("observations", observations)
    return {"count": len(observations), "attempted": attempted}


# --------------------------------------------------------------------------
# Stage: normalize
# --------------------------------------------------------------------------


@registry.register(
    "normalize",
    order=20,
    produces=RunState.NORMALIZED,
    failure_state=RunState.INSUFFICIENT_CANDIDATES,
    describe="Deduplicate observations into canonical property records",
)
def stage_normalize(context: RunContext) -> dict[str, Any]:
    observations = context.get("observations") or _load_observations(context.store)
    records = build_property_records(observations)

    eligible: list[PropertyRecord] = []
    rejected: list[dict[str, str]] = []
    for record in records:
        history = _feature_history(context.database, record.property_key)
        ok, reason, signals = evaluate_eligibility(record, context.template, history)
        if ok:
            eligible.append(record)
        else:
            rejected.append(
                {
                    "property_key": record.property_key,
                    "town": record.town,
                    "reason": reason,
                    "signals": ", ".join(signals[:4]),
                }
            )

    context.store.write_json(
        "properties",
        "normalized.json",
        {
            "eligible": [record.model_dump(mode="json") for record in eligible],
            "rejected": rejected,
        },
    )
    context.stage_logger("normalize").info(
        "normalized",
        f"{len(records)} properties, {len(eligible)} eligible",
        total=len(records),
        eligible=len(eligible),
    )
    _persist_properties(context.database, eligible)

    minimum = context.template.result_count
    if len(eligible) < minimum:
        raise StageError(
            "insufficient_candidates",
            f"only {len(eligible)} eligible properties, need at least {minimum}",
            RunState.INSUFFICIENT_CANDIDATES,
            {"eligible": len(eligible), "rejected_sample": rejected[:10]},
        )
    context.put("records", eligible)
    return {"eligible": len(eligible), "rejected": len(rejected)}


def _load_observations(store: ArtifactStore) -> list[ListingObservation]:
    payload = store.read_json("discovery", "observations.json")
    return [ListingObservation.model_validate(item) for item in payload.get("observations", [])]


def _feature_history(database: Database, property_key: str) -> FeatureHistory:
    with database.session() as session:
        row = session.query(PropertyRow).filter_by(property_key=property_key).one_or_none()
        if row is None:
            return FeatureHistory()
        return FeatureHistory(
            last_featured_at=row.last_featured_at, last_featured_price=row.last_featured_price
        )


def _persist_properties(database: Database, records: list[PropertyRecord]) -> None:
    with database.session() as session:
        for record in records:
            row = (
                session.query(PropertyRow).filter_by(property_key=record.property_key).one_or_none()
            )
            if row is None:
                row = PropertyRow(
                    property_key=record.property_key,
                    normalized_address=record.normalized_address,
                    city=record.city,
                    state=record.state,
                    postal_code=record.postal_code,
                    latitude=record.latitude,
                    longitude=record.longitude,
                )
                session.add(row)
                session.flush()
            row.last_seen_at = utcnow()

            listing = session.query(ListingRow).filter_by(property_id=row.id).one_or_none()
            if listing is None:
                listing = ListingRow(property_id=row.id)
                session.add(listing)
            listing.mls_number = record.canonical.mls_number
            listing.canonical_status = record.canonical.status
            listing.canonical_price = record.canonical.price
            listing.property_type = record.canonical.property_type
            listing.brokerage_name = record.canonical.brokerage_name
            listing.agent_name = record.canonical.agent_name
            listing.canonical_url = record.canonical.listing_url


# --------------------------------------------------------------------------
# Stage: verify
# --------------------------------------------------------------------------


@registry.register(
    "verify",
    order=30,
    produces=RunState.VERIFYING,
    failure_state=RunState.VERIFICATION_FAILED,
    describe="Confirm each listing against a second, authoritative source",
)
def stage_verify(context: RunContext) -> dict[str, Any]:
    records: list[PropertyRecord] = context.get("records") or _load_records(context.store)
    logger = context.stage_logger("verify")

    adapter_name = context.get("verification_adapter", "brokerage")
    try:
        adapter = get_verification_adapter(
            adapter_name, context.settings, directory=context.get("fixture_dir")
        )
    except KeyError as exc:
        raise StageError(
            "unknown_verification_adapter", str(exc), RunState.VERIFICATION_FAILED
        ) from exc

    verified = 0
    blocked = 0
    for record in records:
        observation = None
        try:
            observation = adapter.resolve(record.canonical)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the stage
            logger.warning(
                "verification_error",
                f"{record.town}: {exc}",
                property_key=record.property_key,
            )
        penalty = 0.0 if observation and observation.source != record.canonical.source else 0.15
        record.verification = build_verification(
            record.property_key, record.canonical, observation, adapter_name, penalty
        )
        if record.verification.verified:
            verified += 1
        elif record.verification.blocked:
            blocked += 1
            logger.warning(
                "verification_blocked",
                f"{record.town}: {'; '.join(record.verification.blocking_conflicts[:2])}",
                property_key=record.property_key,
            )

    close = getattr(adapter, "close", None)
    if callable(close):
        close()

    context.store.write_json(
        "properties",
        "verified.json",
        [record.model_dump(mode="json") for record in records],
    )
    context.put("records", records)

    usable = [record for record in records if record.verification and record.verification.verified]
    if len(usable) < context.template.result_count:
        raise StageError(
            "insufficient_verified",
            f"only {len(usable)} properties verified cleanly, need {context.template.result_count}",
            RunState.INSUFFICIENT_CANDIDATES,
            {"verified": len(usable), "blocked": blocked},
        )
    return {"verified": verified, "blocked": blocked}


def _load_records(store: ArtifactStore) -> list[PropertyRecord]:
    for name, filename, key in (
        ("properties", "verified.json", None),
        ("properties", "normalized.json", "eligible"),
    ):
        if store.exists(name, filename):
            payload = store.read_json(name, filename)
            items = payload[key] if key else payload
            return [PropertyRecord.model_validate(item) for item in items]
    raise StageError("missing_properties", "no normalized properties on disk", RunState.MANUAL_HOLD)


# --------------------------------------------------------------------------
# Stage: select
# --------------------------------------------------------------------------


@registry.register(
    "select",
    order=40,
    produces=RunState.SELECTING,
    failure_state=RunState.INSUFFICIENT_CANDIDATES,
    describe="Score candidates and choose the lineup plus alternates",
)
def stage_select(context: RunContext) -> dict[str, Any]:
    records = [
        record
        for record in (context.get("records") or _load_records(context.store))
        if record.verification and record.verification.verified
    ]
    template = context.template
    candidates = build_candidates(records, template)
    chosen = select_lineup(candidates, template)

    selected = [candidate for candidate in chosen if candidate.selected]
    quality = lineup_quality(selected)

    context.store.write_json(
        "properties",
        "candidates.json",
        [candidate.model_dump(mode="json") for candidate in candidates],
    )
    _persist_candidates(context.database, context.run_id, candidates)
    context.put("candidates", candidates)

    if len(selected) < template.result_count:
        raise StageError(
            "insufficient_candidates",
            f"selected only {len(selected)} of {template.result_count} properties",
            RunState.INSUFFICIENT_CANDIDATES,
            {"selected": len(selected)},
        )
    if quality < template.min_candidate_quality:
        # §2.2: publishing nothing beats publishing a weak episode.
        raise SkipEpisode(
            "lineup_below_quality_floor",
            f"lineup quality {quality} is below the template floor of {template.min_candidate_quality}",
            {"quality": quality, "floor": template.min_candidate_quality},
        )

    context.stage_logger("select").info(
        "selected",
        f"{len(selected)} properties, mean score {quality}",
        quality=quality,
        towns=[candidate.property.town for candidate in selected],
    )
    return {"selected": len(selected), "quality": quality}


def _persist_candidates(database: Database, run_id: str, candidates: list[Candidate]) -> None:
    with database.session() as session:
        for candidate in candidates:
            row = (
                session.query(RunCandidateRow)
                .filter_by(run_id=run_id, property_key=candidate.property_key)
                .one_or_none()
            )
            if row is None:
                row = RunCandidateRow(run_id=run_id, property_key=candidate.property_key)
                session.add(row)
            row.eligible = candidate.eligible
            row.selected = candidate.selected
            row.alternate = candidate.alternate
            row.rank = candidate.rank
            row.total_score = candidate.total_score
            row.component_scores_json = candidate.scores.model_dump()
            row.selection_reason = candidate.selection_reason
            row.exclusion_reason = candidate.exclusion_reason
            row.manual_lock = candidate.manual_lock


# --------------------------------------------------------------------------
# Stage: assets
# --------------------------------------------------------------------------


@registry.register(
    "assets",
    order=50,
    produces=RunState.ACQUIRING_ASSETS,
    failure_state=RunState.INSUFFICIENT_CANDIDATES,
    describe="Download, classify and choose the images for each property",
)
def stage_assets(context: RunContext) -> dict[str, Any]:
    candidates = context.get("candidates") or _load_candidates(context.store)
    lineup = [c for c in candidates if c.selected or c.alternate]
    logger = context.stage_logger("assets")

    downloader = AssetDownloader(
        context.store.dir("assets"),
        settings=context.settings.acquisition,
        client=context.get("http_client"),
    )
    all_assets: list[AssetRecord] = []
    per_property: dict[str, Any] = {}

    for candidate in lineup:
        record = candidate.property
        urls = list(record.canonical.image_urls)
        if record.verification and record.verification.observation:
            urls.extend(record.verification.observation.image_urls)

        credit = record.canonical.brokerage_name or ""
        fetched = downloader.fetch_many(
            urls,
            property_key=record.property_key,
            source_page=record.canonical.listing_url or record.canonical.source_url,
            credit_text=f"Listing photo courtesy of {credit}" if credit else "",
            limit=28,
        )
        fetched = deduplicate(fetched)
        classify_and_score(
            fetched, target=(context.settings.render.width, context.settings.render.height)
        )
        selected, warnings = select_for_property(fetched)

        all_assets.extend(fetched)
        per_property[record.property_key] = {
            "town": record.town,
            "downloaded": len(fetched),
            "selected": len(selected),
            "warnings": warnings,
            "coverage": coverage_report(selected),
        }
        if warnings:
            logger.warning(
                "asset_gaps",
                f"{record.town}: {'; '.join(warnings)}",
                property_key=record.property_key,
            )

    downloader.close()
    context.store.write_json(
        "assets",
        "manifest.json",
        {
            "assets": [asset.model_dump(mode="json") for asset in all_assets],
            "by_property": per_property,
        },
    )
    context.put("assets", all_assets)

    thin = [
        key
        for key, info in per_property.items()
        if info["selected"] < 6 and any(c.selected and c.property_key == key for c in lineup)
    ]
    if thin:
        # FR-047 / §14: swap in an alternate rather than repeat images.
        promoted = _promote_alternates(candidates, thin, per_property)
        context.store.write_json(
            "properties",
            "candidates.json",
            [candidate.model_dump(mode="json") for candidate in candidates],
        )
        context.put("candidates", candidates)
        logger.warning(
            "alternate_promoted",
            f"replaced {len(promoted)} properties with thin image sets",
            replaced=thin,
            promoted=promoted,
        )
        still_thin = [
            key for key in thin if any(c.selected and c.property_key == key for c in candidates)
        ]
        if still_thin:
            raise StageError(
                "insufficient_assets",
                f"{len(still_thin)} selected properties have too few usable images and no alternate",
                RunState.INSUFFICIENT_CANDIDATES,
                {"properties": still_thin},
            )
    return {"assets": len(all_assets), "properties": len(per_property)}


def _promote_alternates(
    candidates: list[Candidate], thin_keys: list[str], per_property: dict[str, Any]
) -> list[str]:
    alternates = [
        c
        for c in candidates
        if c.alternate and per_property.get(c.property_key, {}).get("selected", 0) >= 6
    ]
    promoted: list[str] = []
    for key in thin_keys:
        target = next((c for c in candidates if c.property_key == key and c.selected), None)
        if target is None or target.manual_lock or not alternates:
            continue
        replacement = alternates.pop(0)
        replacement.selected, replacement.alternate = True, False
        replacement.rank = target.rank
        replacement.selection_reason += " (promoted: replaced a property with too few images)"
        target.selected, target.alternate = False, False
        target.exclusion_reason = "insufficient usable images"
        target.rank = None
        promoted.append(replacement.property_key)
    return promoted


def _load_candidates(store: ArtifactStore) -> list[Candidate]:
    payload = store.read_json("properties", "candidates.json")
    return [Candidate.model_validate(item) for item in payload]


# --------------------------------------------------------------------------
# Stage: research
# --------------------------------------------------------------------------


@registry.register(
    "research",
    order=60,
    produces=RunState.RESEARCHING,
    failure_state=RunState.RESEARCH_FAILED,
    describe="Capture local context sources and extract evidence-backed claims",
)
def stage_research(context: RunContext) -> dict[str, Any]:
    candidates = context.get("candidates") or _load_candidates(context.store)
    lineup = [c for c in candidates if c.selected or c.alternate]
    logger = context.stage_logger("research")
    provider = build_provider(context.settings.provider)

    capture: SourceCapture = context.get("source_capture") or SourceCapture()
    claims: list[Claim] = []
    sources: list[SourceRecord] = []
    generations: list[dict[str, Any]] = []

    for candidate in lineup:
        record = candidate.property
        listing_source = SourceRecord(
            property_key=record.property_key,
            type="verification" if record.verification else "discovery",
            url=(record.verification.verification_url if record.verification else None)
            or record.canonical.listing_url
            or record.canonical.source_url,
            title=f"{record.canonical.raw_address or record.normalized_address} listing",
            publisher=(
                record.verification.verification_source
                if record.verification
                else record.canonical.source
            )
            or "",
            reliability_tier="A",
        )
        sources.append(listing_source)
        claims.extend(listing_claims(record, [listing_source.source_id]))

    queries = plan_for_lineup([c.property for c in lineup], context.template, max_per_property=3)
    context.store.write_json("research", "plan.json", [query.as_dict() for query in queries])

    for query in queries:
        captured = None
        if query.wiki_title:
            captured = capture.fetch_wikipedia(query.wiki_title, query.property_key)
        if captured is None:
            continue
        sources.append(captured.record)
        record = next(
            (c.property for c in lineup if c.property.property_key == query.property_key), None
        )
        if record is None:
            continue
        extracted, generation = extract_context_claims(provider, record, captured)
        claims.extend(extracted)
        if generation:
            generations.append(generation)
        logger.info(
            "research_source",
            f"{captured.record.title}: {len(extracted)} claims",
            property_key=query.property_key,
            tier=captured.record.reliability_tier,
        )

    capture.close()
    context.store.write_json(
        "research",
        "claims.json",
        {
            "claims": [claim.model_dump(mode="json") for claim in claims],
            "sources": [source.model_dump(mode="json") for source in sources],
            "generations": generations,
        },
    )
    context.put("claims", claims)
    context.put("sources", sources)
    return {"claims": len(claims), "sources": len(sources)}


# --------------------------------------------------------------------------
# Stage: evidence
# --------------------------------------------------------------------------


@registry.register(
    "evidence",
    order=70,
    produces=RunState.RESEARCHING,
    failure_state=RunState.RESEARCH_FAILED,
    describe="Assemble the versioned evidence bundle the writer works from",
)
def stage_evidence(context: RunContext) -> dict[str, Any]:
    candidates = context.get("candidates") or _load_candidates(context.store)
    claims = context.get("claims") or []
    sources = context.get("sources") or []
    assets = context.get("assets") or []

    if not claims or not sources:
        payload = context.store.read_json("research", "claims.json")
        claims = [Claim.model_validate(item) for item in payload["claims"]]
        sources = [SourceRecord.model_validate(item) for item in payload["sources"]]
    if not assets:
        payload = context.store.read_json("assets", "manifest.json")
        assets = [AssetRecord.model_validate(item) for item in payload["assets"]]

    lexicon = PronunciationLexicon.load(context.settings.pronunciation_path)
    bundle = build_bundle(
        run_id=context.run_id,
        template=context.template,
        candidates=candidates,
        claims=claims,
        sources=sources,
        assets=assets,
        lexicon=lexicon,
    )

    gaps = bundle_gaps(bundle, context.template)
    context.store.write_json("evidence", "bundle.json", bundle)
    context.store.write_json("evidence", "gaps.json", gaps)
    context.put("bundle", bundle)

    if gaps:
        raise StageError(
            "evidence_gaps",
            f"evidence bundle has {len(gaps)} blocking gaps",
            RunState.RESEARCH_FAILED,
            {"gaps": gaps[:10]},
        )
    return {
        "claims": len(bundle.claims),
        "sources": len(bundle.sources),
        "hash": bundle.content_hash(),
    }


# --------------------------------------------------------------------------
# Stage: script
# --------------------------------------------------------------------------


@registry.register(
    "script",
    order=80,
    produces=RunState.SCRIPTING,
    failure_state=RunState.SCRIPT_FAILED,
    describe="Write the script, validate every claim, and run the editorial gate",
)
def stage_script(context: RunContext) -> dict[str, Any]:
    bundle = context.get("bundle") or EvidenceBundle.model_validate(
        context.store.read_json("evidence", "bundle.json")
    )
    template = context.template
    provider = build_provider(context.settings.provider)
    logger = context.stage_logger("script")

    previous = context.get("previous_script")
    script = generate_script(bundle, template, provider, previous=previous)

    outcome = validate_script(script, bundle)
    editorial = editorial_evaluate(
        script, bundle, template.target_words, template.word_range, context.settings.gates
    )

    context.store.write_json("script", "script.json", script)
    context.store.write_text("script", "script.md", script_to_markdown(script, bundle))
    context.store.write_json(
        "script",
        "validation.json",
        {
            "factual_passed": outcome.passed,
            "editorial_passed": editorial.passed,
            "editorial_scores": editorial.as_dict(),
            "findings": [
                finding.model_dump(mode="json") for finding in outcome.findings + editorial.findings
            ],
            "unsupported": [claim.text for claim in outcome.unsupported],
        },
    )
    _persist_script(context.database, context.run_id, script, editorial.total)
    context.put("script", script)

    logger.info(
        "script_written",
        f"{script.word_count()} words, editorial {editorial.total}",
        words=script.word_count(),
        editorial=editorial.total,
        factual_passed=outcome.passed,
    )

    if not outcome.passed:
        raise StageError(
            "script_facts_unsupported",
            f"{len(outcome.unsupported)} script claims are not backed by evidence",
            RunState.SCRIPT_FAILED,
            {"examples": [claim.text for claim in outcome.unsupported[:5]]},
            artifact_path=str(context.store.path("script", "validation.json")),
        )
    if not editorial.passed:
        raise SkipEpisode(
            "editorial_gate",
            f"editorial score {editorial.total} did not clear the gate",
            {
                "score": editorial.total,
                "subscores": editorial.subscores,
                "problems": [f.message for f in editorial.findings if f.severity == "error"][:6],
            },
        )
    return {
        "words": script.word_count(),
        "editorial_score": editorial.total,
        "segments": len(script.segments),
    }


def _persist_script(
    database: Database, run_id: str, script: Script, editorial_score: float
) -> None:
    with database.session() as session:
        row = ScriptRow(
            run_id=run_id,
            version=script.version,
            status=script.status,
            total_words=script.word_count(),
            estimated_seconds=script.estimated_seconds(),
            evidence_hash=script.evidence_hash,
            evidence_map_json=script.citation_map,
            generator_json=script.generator | {"editorial_score": editorial_score},
        )
        session.add(row)


# --------------------------------------------------------------------------
# Stage: narrate
# --------------------------------------------------------------------------


@registry.register(
    "narrate",
    order=90,
    produces=RunState.AUDIO_READY,
    failure_state=RunState.RENDER_FAILED,
    describe="Synthesize narration segment by segment and build captions",
)
def stage_narrate(context: RunContext) -> dict[str, Any]:
    script = context.get("script") or Script.model_validate(
        context.store.read_json("script", "script.json")
    )
    lexicon = PronunciationLexicon.load(context.settings.pronunciation_path)
    engine = build_engine(context.settings.speech)
    narrator = Narrator(engine, lexicon, context.settings.speech)

    narrations, issues = narrator.synthesize_script(script.segments, context.store.dir("audio"))
    combined = concatenate_wavs(
        [narration.audio_path for narration in narrations],
        context.store.path("audio", "narration.wav"),
    )

    cues = build_cues(script.segments, narrations)
    caption_paths = write_captions(cues, context.store.dir("captions"))

    watchlist = narrator.watchlist_report(script.spoken_text())
    context.store.write_json(
        "audio",
        "narration.json",
        {
            "engine": engine.name,
            "segments": [narration.model_dump(mode="json") for narration in narrations],
            "issues": [issue.__dict__ for issue in issues],
            "pronunciation_watchlist": watchlist,
            "combined": str(combined),
        },
    )
    context.put("narrations", narrations)
    context.put("cues", cues)
    context.put("narration_path", combined)

    context.stage_logger("narrate").info(
        "narration_ready",
        f"{len(narrations)} segments, {sum(n.duration_seconds for n in narrations):.0f}s",
        engine=engine.name,
        watchlist=[item["term"] for item in watchlist],
    )
    return {
        "segments": len(narrations),
        "seconds": round(sum(n.duration_seconds for n in narrations), 1),
        "engine": engine.name,
        "captions": {key: str(path) for key, path in caption_paths.items()},
    }


# --------------------------------------------------------------------------
# Stage: render
# --------------------------------------------------------------------------


@registry.register(
    "render",
    order=100,
    produces=RunState.RENDERING,
    failure_state=RunState.RENDER_FAILED,
    describe="Build the scene plan, overlays, preview and 1080p master",
)
def stage_render(context: RunContext) -> dict[str, Any]:
    script = context.get("script") or Script.model_validate(
        context.store.read_json("script", "script.json")
    )
    bundle = context.get("bundle") or EvidenceBundle.model_validate(
        context.store.read_json("evidence", "bundle.json")
    )
    narrations = context.get("narrations") or [
        NarrationSegment.model_validate(item)
        for item in context.store.read_json("audio", "narration.json")["segments"]
    ]

    plan = build_scene_plan(script, narrations, bundle, context.template, context.settings.render)
    context.store.write_json("render", "scene-plan.json", plan)
    context.put("scene_plan", plan)

    _write_overlays(context, plan, bundle)

    thumbnails = write_variants(
        build_variants(bundle, context.template.name),
        context.store.dir("thumbnails"),
        context.template.design.palette,
    )
    default_thumbnail = choose_default(thumbnails)
    context.put("thumbnails", thumbnails)

    inputs = RenderInputs(
        assets={asset.asset_id: asset for asset in bundle.assets},
        narration_path=context.get("narration_path")
        or context.store.path("audio", "narration.wav"),
        music_path=_music_track(context),
    )

    outputs: dict[str, Any] = {
        "scene_count": len(plan.scenes),
        "duration": plan.total_duration,
        "thumbnails": [str(variant.svg_path) for variant in thumbnails if variant.svg_path],
        "default_thumbnail": default_thumbnail.name if default_thumbnail else None,
    }

    if context.dry_run:
        command, manifest = build_command(
            plan,
            inputs,
            context.store.path("render", "master.mp4"),
            context.settings.render,
            context.settings.mix,
        )
        context.store.write_json("render", "render-manifest.json", manifest.as_dict())
        outputs["dry_run"] = True
        return outputs

    for preview in (True, False):
        target = context.store.path("render", "preview.mp4" if preview else "master.mp4")
        command, manifest = build_command(
            plan, inputs, target, context.settings.render, context.settings.mix, preview=preview
        )
        try:
            stderr = run_render(command)
        except FFmpegUnavailable as exc:
            raise StageError(
                "ffmpeg_unavailable",
                str(exc),
                RunState.RENDER_FAILED,
                {"hint": "install ffmpeg and re-run 'mpvf doctor'"},
            ) from exc
        except RenderFailed as exc:
            log_path = context.store.write_text("logs", "ffmpeg-error.log", exc.stderr)
            raise StageError(
                "render_failed",
                str(exc),
                RunState.RENDER_FAILED,
                {"command": manifest.command},
                artifact_path=str(log_path),
            ) from exc

        context.store.write_text("logs", f"ffmpeg-{'preview' if preview else 'master'}.log", stderr)
        context.store.write_json(
            "render", f"{'preview' if preview else 'render'}-manifest.json", manifest.as_dict()
        )
        outputs["preview_path" if preview else "master_path"] = str(target)

    _persist_render(context, outputs)
    return outputs


def _write_overlays(context: RunContext, plan: ScenePlan, bundle: EvidenceBundle) -> None:
    """Render each scene's SVG overlay; rasterization is best-effort."""

    palette = context.template.design.palette
    frame = design.Frame(
        width=context.settings.render.width,
        height=context.settings.render.height,
        safe_margin_pct=context.settings.render.safe_margin_pct,
    )
    overlay_dir = context.store.dir("render")

    for scene in plan.scenes:
        document = None
        if scene.kind == "title":
            title = next((o.text for o in scene.overlays if o.kind == "chapter_card"), "")
            document = design.title_card(title, context.template.editorial_promise, frame, palette)
        elif scene.kind == "chapter_card":
            texts = {overlay.kind: overlay.text for overlay in scene.overlays}
            callouts = [o.text for o in scene.overlays if o.kind == "callout"]
            document = design.chapter_card(
                rank=int(texts.get("chapter_card", "#0").lstrip("#") or 0),
                town=texts.get("lower_third", ""),
                price=callouts[0] if callouts else "",
                facts=callouts[1] if len(callouts) > 1 else "",
                credit=texts.get("credit", ""),
                frame=frame,
                palette_name=palette,
            )
        elif scene.kind == "map":
            record = next(
                (p for p in bundle.properties if p.property_key == scene.property_key), None
            )
            document = design.locator_map(
                record.town if record else "",
                record.latitude if record else None,
                record.longitude if record else None,
                frame,
                palette,
            )
        elif scene.overlays:
            overlay = scene.overlays[0]
            if overlay.kind == "disclosure":
                document = design.disclosure_strip(overlay.text, frame, palette)
            elif overlay.kind == "credit":
                document = design.credit_line(overlay.text, frame, palette)
            else:
                document = design.callout(overlay.text, frame, palette)

        if document is None:
            continue
        svg_path = document.write(overlay_dir / f"overlay-{scene.scene_id}.svg")
        scene.svg_path = str(svg_path)
        try:
            design.rasterize(
                svg_path,
                overlay_dir / f"overlay-{scene.scene_id}.png",
                frame.width,
                frame.height,
            )
        except design.RasterizerUnavailable:
            context.stage_logger("render").warning(
                "rasterizer_missing",
                "no SVG rasterizer installed; overlays will not be burned in",
            )
            break


def _music_track(context: RunContext) -> Path | None:
    """Only approved local tracks may enter the pipeline (FR-150, FR-155)."""

    if not context.settings.mix.music_enabled:
        return None
    library = context.settings.shared_dir / "music"
    allowed = context.template.design.allowed_tracks
    if not library.exists():
        return None
    for path in sorted(library.glob("*")):
        if path.suffix.lower() not in {".mp3", ".wav", ".flac", ".m4a"}:
            continue
        if allowed and path.name not in allowed:
            continue
        if not (library / f"{path.stem}.license.json").exists():
            continue  # no asset record, no use
        return path
    return None


def _persist_render(context: RunContext, outputs: dict[str, Any]) -> None:
    with context.database.session() as session:
        session.add(
            RenderRow(
                run_id=context.run_id,
                profile="master",
                state="complete",
                preview_path=outputs.get("preview_path"),
                master_path=outputs.get("master_path"),
                duration_seconds=outputs.get("duration"),
                command_manifest_json={"scene_count": outputs.get("scene_count")},
            )
        )


# --------------------------------------------------------------------------
# Stage: qa
# --------------------------------------------------------------------------


@registry.register(
    "qa",
    order=110,
    produces=RunState.QA_PENDING,
    failure_state=RunState.QA_FAILED,
    describe="Run factual, editorial, visual, audio and technical checks",
)
def stage_qa(context: RunContext) -> dict[str, Any]:
    script = context.get("script") or Script.model_validate(
        context.store.read_json("script", "script.json")
    )
    bundle = context.get("bundle") or EvidenceBundle.model_validate(
        context.store.read_json("evidence", "bundle.json")
    )
    plan = context.get("scene_plan") or ScenePlan.model_validate(
        context.store.read_json("render", "scene-plan.json")
    )
    narration_payload = context.store.read_json("audio", "narration.json")
    narrations = context.get("narrations") or [
        NarrationSegment.model_validate(item) for item in narration_payload["segments"]
    ]
    cues: list[CaptionCue] = context.get("cues") or []
    if not cues:
        from mpvf.speech.captions import parse_srt

        srt_path = context.store.path("captions", "captions.srt")
        if srt_path.exists():
            cues = parse_srt(srt_path.read_text(encoding="utf-8"))

    master = context.store.path("render", "master.mp4")
    metadata = context.get("metadata")
    report = qa_checks.run_all(
        run_id=context.run_id,
        script=script,
        bundle=bundle,
        plan=plan,
        narrations=narrations,
        cues=cues,
        settings=context.settings.render,
        mix=context.settings.mix,
        gates=context.settings.gates,
        target_words=context.template.target_words,
        word_range=context.template.word_range,
        master_path=master if master.exists() else None,
        audio_path=context.store.path("audio", "narration.wav"),
        transcript=context.get("transcript"),
        package_files={
            "master.mp4": master if master.exists() else None,
            "captions.srt": context.store.path("captions", "captions.srt"),
            "script.md": context.store.path("script", "script.md"),
        },
        title=metadata.title if metadata else None,
    )

    context.store.write_json("render", "qa-report.json", report)
    context.store.write_json("render", "repair-plan.json", qa_checks.repair_plan(report))
    context.put("qa_report", report)

    context.stage_logger("qa").info(
        "qa_complete",
        f"{len(report.errors)} errors, {len(report.warnings)} warnings",
        passed=report.passed,
        scores=report.scores,
    )
    if not report.passed:
        raise StageError(
            "qa_failed",
            f"{len(report.errors)} QA errors block upload",
            RunState.QA_FAILED,
            {"errors": [finding.message for finding in report.errors[:8]]},
            artifact_path=str(context.store.path("render", "qa-report.json")),
        )
    return {"errors": 0, "warnings": len(report.warnings), "scores": report.scores}


# --------------------------------------------------------------------------
# Stage: publish
# --------------------------------------------------------------------------


@registry.register(
    "publish",
    order=120,
    produces=RunState.UPLOADED,
    failure_state=RunState.UPLOAD_FAILED,
    describe="Assemble the publication package and upload privately to YouTube",
)
def stage_publish(context: RunContext) -> dict[str, Any]:
    bundle = context.get("bundle") or EvidenceBundle.model_validate(
        context.store.read_json("evidence", "bundle.json")
    )
    script = context.get("script") or Script.model_validate(
        context.store.read_json("script", "script.json")
    )
    narrations = context.get("narrations") or [
        NarrationSegment.model_validate(item)
        for item in context.store.read_json("audio", "narration.json")["segments"]
    ]
    report = context.get("qa_report") or QAReport.model_validate(
        context.store.read_json("render", "qa-report.json")
    )

    schedule = context.mode == "scheduled" or context.mode == "full_auto"
    metadata = metadata_builder.build_metadata(
        bundle, context.template, script, narrations, schedule=schedule
    )
    context.put("metadata", metadata)

    # Publication-time recheck: never publish a stale status (FR-036, FR-037).
    stale = _recheck_listings(context, bundle)
    if stale:
        raise StageError(
            "listing_changed",
            f"{len(stale)} listings changed status or price before publication",
            RunState.MANUAL_HOLD,
            {"properties": stale},
        )

    master = context.store.path("render", "master.mp4")
    preview = context.store.path("render", "preview.mp4")
    thumbnails = [
        path for path in sorted(context.store.dir("thumbnails").glob("thumbnail-*.png"))
    ] or [path for path in sorted(context.store.dir("thumbnails").glob("thumbnail-*.svg"))]
    render_manifest = (
        context.store.read_json("render", "render-manifest.json")
        if context.store.exists("render", "render-manifest.json")
        else {}
    )

    package = build_package(
        store=context.store,
        bundle=bundle,
        script=script,
        script_markdown=script_to_markdown(script, bundle),
        metadata=metadata,
        qa_report=report,
        render_manifest=render_manifest,
        master_path=master if master.exists() else None,
        preview_path=preview if preview.exists() else None,
        caption_paths={
            "srt": context.store.path("captions", "captions.srt"),
            "vtt": context.store.path("captions", "captions.vtt"),
        },
        thumbnail_paths=thumbnails,
    )

    key = idempotency_key(context.run_id, master, metadata)
    existing = _existing_publication(context.database, key)
    if existing is not None:
        context.stage_logger("publish").info(
            "publish_idempotent",
            "this render was already published; not uploading again",
            video_id=existing.youtube_video_id,
        )
        return {
            "video_id": existing.youtube_video_id,
            "url": existing.url,
            "reused": True,
            "package_complete": package.complete,
        }

    if context.mode == "review" or context.dry_run:
        context.stage_logger("publish").info(
            "publish_skipped", f"mode is {context.mode}; package written but not uploaded"
        )
        return {
            "uploaded": False,
            "mode": context.mode,
            "package": str(package.directory),
            "missing": package.missing,
        }

    publisher = build_publisher(
        context.settings.secrets_dir, context.store.dir("publish"), enabled=True
    )
    result = publisher.upload(
        UploadRequest(
            video_path=master,
            metadata=metadata,
            thumbnail_path=thumbnails[0] if thumbnails else None,
            captions_path=context.store.path("captions", "captions.srt"),
            idempotency_key=key,
        )
    )
    result.run_id = context.run_id
    _persist_publication(context.database, context.run_id, key, metadata, result)
    context.store.write_json("publish", "publication.json", result)

    return {
        "uploaded": result.upload_state in {"uploaded", "scheduled"},
        "video_id": result.video_id,
        "url": result.url,
        "privacy": result.privacy,
        "publisher": publisher.name,
        "package_complete": package.complete,
    }


def _recheck_listings(context: RunContext, bundle: EvidenceBundle) -> list[str]:
    """Re-verify status and price within six hours of publication (FR-036)."""

    checked = bundle.checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    if utcnow() - checked < timedelta(hours=6):
        return []

    try:
        adapter = get_verification_adapter(
            context.get("verification_adapter", "brokerage"),
            context.settings,
            directory=context.get("fixture_dir"),
        )
    except KeyError:
        return []

    stale: list[str] = []
    allowed = context.template.hard_filters.status
    for candidate in bundle.candidates:
        if not candidate.selected:
            continue
        try:
            observation = adapter.resolve(candidate.property.canonical)
        except Exception:  # noqa: BLE001
            continue
        if observation is None:
            continue
        if observation.status not in allowed:
            stale.append(f"{candidate.property.town}: status is now {observation.status}")
        elif (
            observation.price
            and candidate.property.price
            and abs(observation.price - candidate.property.price) / candidate.property.price > 0.02
        ):
            stale.append(f"{candidate.property.town}: price moved to ${observation.price:,}")
    close = getattr(adapter, "close", None)
    if callable(close):
        close()
    return stale


def _existing_publication(database: Database, key: str) -> PublicationRow | None:
    with database.session() as session:
        return session.query(PublicationRow).filter_by(idempotency_key=key).one_or_none()


def _persist_publication(
    database: Database, run_id: str, key: str, metadata: Any, result: Any
) -> None:
    with database.session() as session:
        session.add(
            PublicationRow(
                run_id=run_id,
                idempotency_key=key,
                youtube_video_id=result.video_id,
                url=result.url,
                privacy=result.privacy,
                title=metadata.title,
                description=metadata.description,
                tags_json=metadata.tags,
                playlist=metadata.playlist,
                upload_state=result.upload_state,
                scheduled_for=result.scheduled_for,
                processing_state=result.processing_state,
                api_response_json=result.api_response,
            )
        )


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


class Runner:
    """Creates runs, executes stages, and owns every state transition."""

    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        settings.ensure_directories()
        self.database = database or Database(settings.database_url())
        self.database.create_all()
        self.templates = TemplateRegistry(settings.templates_dir)

    # -- run lifecycle ----------------------------------------------------
    def create_run(
        self,
        template: SearchTemplate,
        mode: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> tuple[str, ArtifactStore]:
        mode = mode or self.settings.publishing_mode
        with self.database.session() as session:
            row = RunRow(
                template_slug=template.slug,
                template_version=template.version,
                state=str(RunState.SCHEDULED),
                mode=mode,
                scheduled_for=scheduled_for,
                input_hash=template.config_hash(),
                created_at=utcnow(),
            )
            session.add(row)
            session.flush()
            run_id = row.id
            store = ArtifactStore.for_run(self.settings.runs_dir, run_id, template.slug)
            row.artifact_dir = str(store.root)
        return run_id, store

    def context_for(
        self, run_id: str, template: SearchTemplate, store: ArtifactStore, mode: str, dry_run: bool
    ) -> RunContext:
        configure_logging(json_path=store.dir("logs") / "run.jsonl")
        return RunContext(
            run_id=run_id,
            settings=self.settings,
            template=template,
            store=store,
            database=self.database,
            logger=RunLogger(run_id, self.database),
            mode=mode,
            dry_run=dry_run,
        )

    # -- execution --------------------------------------------------------
    def run(
        self,
        template_slug: str,
        from_stage: str | None = None,
        to_stage: str | None = None,
        run_id: str | None = None,
        mode: str | None = None,
        dry_run: bool = False,
        context_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        template = self.templates.get(template_slug)
        if run_id:
            store, existing_mode = self._resume_store(run_id)
            mode = mode or existing_mode
        else:
            run_id, store = self.create_run(template, mode)
        mode = mode or self.settings.publishing_mode

        context = self.context_for(run_id, template, store, mode, dry_run)
        for key, value in (context_overrides or {}).items():
            context.put(key, value)

        results: list[StageResult] = []
        summary: dict[str, Any] = {"run_id": run_id, "template": template.slug, "stages": results}

        for spec in registry.slice(from_stage, to_stage):
            try:
                result = self._execute_stage(context, spec)
                results.append(result)
            except SkipEpisode as skip:
                self._mark_skipped(run_id, skip)
                context.logger.bind(spec.name).warning(skip.code, skip.message, **skip.detail)
                summary["outcome"] = "skipped"
                summary["reason"] = skip.message
                summary["detail"] = skip.detail
                return summary
            except StageError as error:
                self._mark_failed(run_id, spec, error)
                context.logger.bind(spec.name).error(error.code, error.message, **error.detail)
                summary["outcome"] = "failed"
                summary["failed_stage"] = spec.name
                summary["reason"] = error.message
                summary["detail"] = error.as_dict()
                return summary

        self._advance(run_id, RunState.ARCHIVED, force=True)
        summary["outcome"] = "completed"
        summary["artifact_dir"] = str(store.root)
        return summary

    def _execute_stage(self, context: RunContext, spec: StageSpec) -> StageResult:
        started = time.perf_counter()
        stage_logger = context.logger.bind(spec.name)
        stage_logger.info("stage_start", f"starting {spec.name}")

        self._advance(context.run_id, spec.produces)
        self._stage_row(context.run_id, spec.name, state="running")

        try:
            payload = spec.run(context)
        except (StageError, SkipEpisode):
            raise
        except Exception as exc:  # noqa: BLE001 - convert to a typed stage failure
            self._stage_row(
                context.run_id,
                spec.name,
                state="failed",
                error_code=type(exc).__name__,
                error_detail=str(exc),
            )
            raise StageError(
                f"{spec.name}_unhandled",
                f"{type(exc).__name__}: {exc}",
                spec.failure_state,
                {"stage": spec.name},
            ) from exc

        duration = round(time.perf_counter() - started, 3)
        self._stage_row(
            context.run_id,
            spec.name,
            state="complete",
            duration=duration,
            input_hash=hash_inputs(context.template.config_hash(), spec.name),
        )
        stage_logger.info(
            "stage_complete", f"{spec.name} finished in {duration}s", duration_seconds=duration
        )
        return StageResult(
            name=spec.name,
            state=spec.produces,
            input_hash=context.template.config_hash(),
            output_path=str(context.store.root),
            cached=False,
            duration_seconds=duration,
            payload=payload,
        )

    # -- state helpers ----------------------------------------------------
    def _advance(self, run_id: str, target: RunState, force: bool = False) -> None:
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            current = RunState(row.state)
            if current == target:
                return
            if not force:
                try:
                    assert_transition(current, target)
                except Exception:
                    # A stage rerun re-enters a state we have already passed;
                    # that is legal and simply keeps the furthest state.
                    return
            row.state = str(target)
            if row.started_at is None:
                row.started_at = utcnow()
            if target in {RunState.ARCHIVED, RunState.PUBLISHED}:
                row.completed_at = utcnow()

    def _mark_failed(self, run_id: str, spec: StageSpec, error: StageError) -> None:
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            row.state = str(error.failure_state)
            row.failure_code = error.code
            row.failure_detail = error.message
            row.completed_at = utcnow()
        self._stage_row(
            run_id, spec.name, state="failed", error_code=error.code, error_detail=error.message
        )

    def _mark_skipped(self, run_id: str, skip: SkipEpisode) -> None:
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            row.state = str(RunState.SKIPPED)
            row.failure_code = skip.code
            row.failure_detail = skip.message
            row.completed_at = utcnow()

    def _stage_row(
        self,
        run_id: str,
        name: str,
        state: str,
        duration: float | None = None,
        input_hash: str = "",
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        with self.database.session() as session:
            row = session.query(StageRow).filter_by(run_id=run_id, name=name).one_or_none()
            if row is None:
                row = StageRow(run_id=run_id, name=name)
                session.add(row)
            row.state = state
            row.attempt = (row.attempt or 0) + (1 if state == "running" else 0)
            row.input_hash = input_hash or row.input_hash
            row.error_code = error_code
            row.error_detail = error_detail
            if state == "running":
                row.started_at = utcnow()
            if state in {"complete", "failed"}:
                row.completed_at = utcnow()
                row.duration_seconds = duration

    def _resume_store(self, run_id: str) -> tuple[ArtifactStore, str]:
        with self.database.session() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            return ArtifactStore(row.artifact_dir), row.mode

    # -- queries ----------------------------------------------------------
    def status(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = session.query(RunRow).order_by(RunRow.created_at.desc())
            rows = [session.get(RunRow, run_id)] if run_id else query.limit(25).all()
            output: list[dict[str, Any]] = []
            for row in rows:
                if row is None:
                    continue
                stages = session.query(StageRow).filter_by(run_id=row.id).all()
                output.append(
                    {
                        "run_id": row.id,
                        "template": row.template_slug,
                        "state": row.state,
                        "mode": row.mode,
                        "created_at": row.created_at,
                        "completed_at": row.completed_at,
                        "failure_code": row.failure_code,
                        "failure_detail": row.failure_detail,
                        "artifact_dir": row.artifact_dir,
                        "is_failure": is_failure(RunState(row.state)),
                        "stages": [
                            {
                                "name": stage.name,
                                "state": stage.state,
                                "duration": stage.duration_seconds,
                                "error": stage.error_code,
                            }
                            for stage in sorted(stages, key=lambda s: s.started_at or utcnow())
                        ],
                    }
                )
            return output
