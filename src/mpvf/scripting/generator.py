"""Script generation (§7.9).

The model writes prose from the evidence bundle; this module owns everything
that must not be left to the model: segment ordering, price/status injection
from structured fields (FR-104), the citation map (FR-106) and locked-segment
preservation (FR-092).
"""

from __future__ import annotations

import json
import random
from typing import Any, cast

from mpvf.config.templates import SearchTemplate
from mpvf.evidence.bundle import writer_view
from mpvf.generation.provider import (
    DeterministicProvider,
    FallbackProvider,
    GenerationProvider,
    Message,
)
from mpvf.models.domain import (
    Claim,
    EvidenceBundle,
    OnScreenText,
    OverlayKind,
    Script,
    ScriptSegment,
    SegmentType,
)
from mpvf.scripting import prompts
from mpvf.scripting.schemas import SCHEMA_VERSION, DraftSegment, ScriptDraft
from mpvf.speech.tts import estimate_seconds

_VALID_TYPES: set[str] = {
    "cold_open",
    "intro",
    "property",
    "transition",
    "comparison",
    "closing",
    "disclaimer",
}


def build_messages(
    bundle: EvidenceBundle, template: SearchTemplate
) -> tuple[list[Message], dict[str, Any]]:
    brief = writer_view(bundle, template)
    user = prompts.render(
        prompts.SCRIPT_USER,
        brief=json.dumps(brief, indent=2),
        result_count=template.result_count,
        max_segment_words=template.max_segment_words,
        min_words=template.word_range[0],
        max_words=template.word_range[1],
    )
    return (
        [
            Message(role="system", content=prompts.SCRIPT_SYSTEM.text),
            Message(role="user", content=user),
        ],
        brief,
    )


def generate_script(
    bundle: EvidenceBundle,
    template: SearchTemplate,
    provider: GenerationProvider,
    previous: Script | None = None,
    seed: int = 0,
) -> Script:
    """Produce a validated ``Script`` from the evidence bundle."""

    provider = _with_deterministic_fallback(provider, bundle, template, seed)
    messages, _brief = build_messages(bundle, template)
    draft, record = provider.generate_structured(
        "script_draft", messages, ScriptDraft, temperature=0.5
    )

    script = _assemble(draft, bundle, template)  # type: ignore[arg-type]
    script.generator = record.as_dict() | {
        "prompt_id": prompts.SCRIPT_SYSTEM.id,
        "prompt_version": prompts.SCRIPT_SYSTEM.version,
        "schema_version": SCHEMA_VERSION,
    }
    script.evidence_hash = bundle.content_hash()
    if previous is not None:
        script = preserve_locked_segments(previous, script)
        script.version = previous.version + 1
    return script


def _with_deterministic_fallback(
    provider: GenerationProvider,
    bundle: EvidenceBundle,
    template: SearchTemplate,
    seed: int,
) -> GenerationProvider:
    """Bind the evidence-driven fallback writer into any deterministic provider."""

    def handler(_messages: list[Message]) -> dict[str, Any]:
        return compose_draft(bundle, template, seed).model_dump()

    if isinstance(provider, DeterministicProvider):
        provider.register("script_draft", handler)
        return provider
    if isinstance(provider, FallbackProvider) and isinstance(
        provider.secondary, DeterministicProvider
    ):
        provider.secondary.register("script_draft", handler)
    return provider


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _property_order(bundle: EvidenceBundle) -> list[str]:
    """Countdown order: highest rank number first, rank 1 last."""

    ranked = [c for c in bundle.candidates if c.selected and c.rank is not None]
    ranked.sort(key=lambda c: c.rank or 0, reverse=True)
    return [c.property_key for c in ranked]


def _assemble(draft: ScriptDraft, bundle: EvidenceBundle, template: SearchTemplate) -> Script:
    order = _property_order(bundle)
    rank_by_key = {
        c.property_key: c.rank for c in bundle.candidates if c.selected and c.rank is not None
    }
    valid_claim_ids = {claim.claim_id for claim in bundle.claims if claim.eligible_for_script}

    segments: list[ScriptSegment] = []
    property_index = 0
    for draft_segment in draft.segments:
        segment_type = draft_segment.type if draft_segment.type in _VALID_TYPES else "transition"
        property_key: str | None = None
        rank: int | None = None

        if segment_type == "property":
            property_key = draft_segment.property_key
            if property_key not in rank_by_key:
                # The model named an unknown property: fall back to episode order.
                property_key = order[property_index] if property_index < len(order) else None
            if property_key is None:
                continue
            rank = rank_by_key.get(property_key)
            property_index += 1

        spoken = _inject_structured_facts(draft_segment.spoken_text, property_key, bundle)
        segment = ScriptSegment(
            type=segment_type,  # type: ignore[arg-type]
            order=len(segments),
            property_key=property_key,
            rank=rank,
            spoken_text=spoken.strip(),
            evidence_refs=[ref for ref in draft_segment.evidence_refs if ref in valid_claim_ids],
            on_screen_text=_on_screen(draft_segment, property_key, bundle),
            asset_plan=[draft_segment.asset_hint] if draft_segment.asset_hint else [],
            pronunciation_notes=draft_segment.pronunciation_notes,
            estimated_seconds=estimate_seconds(spoken),
        )
        segments.append(segment)

    segments.append(_disclaimer_segment(len(segments), bundle, template))

    script = Script(
        run_id=bundle.run_id,
        template_slug=template.slug,
        segments=segments,
        citation_map={
            segment.segment_id: segment.evidence_refs
            for segment in segments
            if segment.evidence_refs
        },
    )
    return script


def _inject_structured_facts(text: str, property_key: str | None, bundle: EvidenceBundle) -> str:
    """FR-104: prices and statuses come from fields, not from model prose.

    Any dollar amount the model wrote inside a property segment is replaced with
    the verified asking price, so a hallucinated digit cannot reach narration.
    """

    if not property_key:
        return text
    price_claim = next(
        (
            c
            for c in bundle.eligible_claims_for(property_key)
            if c.type == "price" and c.unit == "usd"
        ),
        None,
    )
    if price_claim is None or not isinstance(price_claim.value, (int, float)):
        return text

    import re

    verified = f"${int(price_claim.value):,}"
    # Shorthand amounts ("$1.2 million") are matched whole; plain amounts allow
    # cents only as exactly two digits, so a sentence-ending period is never
    # swallowed into the number.
    pattern = (
        r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:million|billion|k|m)\b"
        r"|\$\s?\d[\d,]*(?:\.\d{2})?(?!\d)"
    )
    return re.sub(pattern, verified, text, flags=re.IGNORECASE)


_OVERLAY_KINDS: frozenset[str] = frozenset(
    {"chapter_card", "callout", "credit", "disclosure", "lower_third"}
)


def _overlay_kind(value: str) -> OverlayKind:
    """A model-supplied overlay kind, narrowed to the ones the renderer draws."""

    return cast(OverlayKind, value) if value in _OVERLAY_KINDS else "callout"


def _on_screen(
    draft_segment: DraftSegment, property_key: str | None, bundle: EvidenceBundle
) -> list[OnScreenText]:
    overlays = [
        OnScreenText(kind=_overlay_kind(item.kind), text=item.text)
        for item in draft_segment.on_screen_text
    ]
    if property_key:
        credit = bundle.credit_for(property_key)
        if credit and (credit.brokerage_name or credit.agent_name):
            overlays.append(OnScreenText(kind="credit", text=credit.spoken(), duration=4.0))
    return overlays


def _disclaimer_segment(
    order: int, bundle: EvidenceBundle, template: SearchTemplate
) -> ScriptSegment:
    """The required on-screen/spoken disclosure (§8.4, FR-084)."""

    stamp = bundle.checked_at.strftime("%B %d, %Y")
    text = (
        f"Prices and availability were checked on {stamp}. "
        "Listings change quickly, so confirm the details with the listing agent before you act on anything here."
    )
    return ScriptSegment(
        type="disclaimer",
        order=order,
        spoken_text=text,
        on_screen_text=[
            OnScreenText(
                kind="disclosure", text=f"Prices and availability checked {stamp}", duration=5.0
            )
        ],
        estimated_seconds=estimate_seconds(text),
    )


def preserve_locked_segments(previous: Script, regenerated: Script) -> Script:
    """Regeneration must never overwrite operator-locked text (FR-092)."""

    locked = {
        segment.property_key or f"__{segment.type}_{segment.order}": segment
        for segment in previous.segments
        if segment.locked
    }
    if not locked:
        return regenerated

    for index, segment in enumerate(regenerated.segments):
        key = segment.property_key or f"__{segment.type}_{segment.order}"
        original = locked.get(key)
        if original is not None:
            preserved = original.model_copy(deep=True)
            preserved.order = segment.order
            regenerated.segments[index] = preserved
    return regenerated


# --------------------------------------------------------------------------
# Deterministic composition (no model required)
# --------------------------------------------------------------------------

_OPENERS = (
    "Start in {town}.",
    "Next, {town}.",
    "Then there is this one, in {town}.",
    "{town} gives us number {rank}.",
    "Number {rank} sits in {town}.",
    "Our last stop is {town}.",
)

_TRANSITIONS = (
    "That is a hard act to follow, but the next one tries.",
    "The next property trades that view for something else entirely.",
    "Different town, different argument.",
    "Now we move up the coast.",
)


def _comparison_sentence(record, previous, index: int) -> str | None:
    """A qualitative comparison to the previous property.

    Deliberately free of new numbers: every figure a script states has to come
    from the evidence bundle, and a computed difference is not in it. Saying
    "less money, more land" is both true and checkable from the claims already
    present.
    """

    if previous is None:
        return None
    bits: list[str] = []
    if record.price and previous.price:
        bits.append("less money" if record.price < previous.price else "more money")
    if record.canonical.acres and previous.canonical.acres:
        bits.append(
            "more land" if record.canonical.acres > previous.canonical.acres else "less land"
        )
    elif record.canonical.square_feet and previous.canonical.square_feet:
        bits.append(
            "more house"
            if record.canonical.square_feet > previous.canonical.square_feet
            else "less house"
        )
    if not bits:
        return None

    frames = (
        f"Against the {previous.town} house that is {' and '.join(bits)}.",
        f"Compared with {previous.town}, you trade into {' and '.join(bits)}.",
        f"This one is {' and '.join(bits)} than the {previous.town} listing.",
        f"In exchange for leaving {previous.town} you get {' and '.join(bits)}.",
    )
    return frames[index % len(frames)]


def _claim_text(
    claims: list[Claim], claim_type: str, unit: str | None = None
) -> tuple[str, str] | None:
    for claim in claims:
        if claim.type == claim_type and (unit is None or claim.unit == unit):
            return claim.text, claim.claim_id
    return None


def compose_draft(bundle: EvidenceBundle, template: SearchTemplate, seed: int = 0) -> ScriptDraft:
    """Build a complete, evidence-only draft without a language model.

    This is the degraded path, and it is honest about being plain: it states
    facts in a fixed order rather than pretending to have a voice it doesn't.
    """

    rng = random.Random(seed or 7)
    order = _property_order(bundle)
    stamp = bundle.checked_at.strftime("%B %d")
    segments: list[DraftSegment] = []

    hero_key = order[-1] if order else None
    hero_town = ""
    if hero_key:
        hero = next((p for p in bundle.properties if p.property_key == hero_key), None)
        hero_town = hero.town if hero else ""

    segments.append(
        DraftSegment(
            type="cold_open",
            spoken_text=(
                f"One of these five Maine houses is in {hero_town}, and it is the reason this list exists."
                if hero_town
                else "Five Maine houses, checked this morning, ranked by what you actually get."
            ),
            asset_hint="water_view",
            on_screen_text=[],
        )
    )
    segments.append(
        DraftSegment(
            type="intro",
            spoken_text=(
                f"{template.editorial_promise or template.name}. "
                f"Every price and status here was checked on {stamp}, and each listing is credited to the "
                "brokerage that brought it to market."
            ),
            asset_hint="exterior",
        )
    )

    previous_record = None
    for index, property_key in enumerate(order):
        record = next((p for p in bundle.properties if p.property_key == property_key), None)
        if record is None:
            continue
        candidate = next((c for c in bundle.candidates if c.property_key == property_key), None)
        rank = candidate.rank if candidate else len(order) - index
        claims = bundle.eligible_claims_for(property_key)
        used: list[str] = []
        sentences: list[str] = []

        opener = _OPENERS[index % len(_OPENERS)].format(town=record.town, rank=rank)
        sentences.append(opener)

        for claim_type, unit in (
            ("price", "usd"),
            ("listing_fact", "beds"),
            ("measurement", "sqft"),
            ("measurement", "feet"),
            ("measurement", "acres"),
            ("listing_fact", "year"),
        ):
            found = _claim_text(claims, claim_type, unit)
            if found:
                sentences.append(found[0])
                used.append(found[1])

        context = next(
            (c for c in claims if c.type in {"history", "context"} and c.scope == "property"), None
        ) or next((c for c in claims if c.type == "context"), None)
        if context:
            prefix = "" if context.scope == "property" else f"In {record.town}, "
            sentences.append(f"{prefix}{context.text[0].lower() + context.text[1:]}")
            used.append(context.claim_id)

        # A material limitation, stated plainly rather than glossed (FR-086).
        limitation = next(
            (
                claim
                for claim in claims
                if claim.attribute in {"seasonal", "hoa_fee", "property_type"}
                and claim.claim_id not in used
            ),
            None,
        )
        if limitation is not None:
            sentences.append(limitation.text)
            used.append(limitation.claim_id)
        else:
            for claim in claims:
                if (
                    claim.type == "listing_fact"
                    and claim.value is True
                    and claim.claim_id not in used
                ):
                    sentences.append(claim.text)
                    used.append(claim.claim_id)
                    break

        comparison = _comparison_sentence(record, previous_record, index)
        if comparison:
            sentences.append(comparison)
        previous_record = record

        credit = bundle.credit_for(property_key)
        if credit:
            sentences.append(f"{credit.spoken()}.")
            attribution = _claim_text(claims, "attribution")
            if attribution:
                used.append(attribution[1])

        segments.append(
            DraftSegment(
                type="property",
                property_key=property_key,
                spoken_text=" ".join(sentences),
                evidence_refs=used,
                asset_hint="exterior" if index % 2 == 0 else "water_view",
                on_screen_text=[],
            )
        )
        if index < len(order) - 1:
            segments.append(
                DraftSegment(
                    type="transition",
                    spoken_text=rng.choice(_TRANSITIONS),
                    asset_hint="map",
                )
            )

    if len(order) >= 2:
        first = next((p for p in bundle.properties if p.property_key == order[-1]), None)
        second = next((p for p in bundle.properties if p.property_key == order[-2]), None)
        if first and second:
            cheaper = first if (first.price or 0) < (second.price or 0) else second
            dearer = second if cheaper is first else first
            segments.append(
                DraftSegment(
                    type="closing",
                    spoken_text=(
                        f"So which one would you actually take: the {first.town} house, or the one in "
                        f"{second.town}? The {cheaper.town} listing is cheaper than the {dearer.town} one, "
                        "and that difference buys a very different life. They are not the same bet, "
                        "and the comments usually split."
                    ),
                    asset_hint="water_view",
                )
            )
    return ScriptDraft(segments=segments, notes="composed deterministically from evidence")


def script_to_markdown(script: Script, bundle: EvidenceBundle) -> str:
    """Human-readable script for review (§18 ``script.md``)."""

    lines = [f"# Episode script — {script.template_slug} (v{script.version})", ""]
    lines.append(
        f"Words: {script.word_count()} · Estimated runtime: {script.estimated_seconds():.0f}s"
    )
    lines.append("")
    for segment in script.segments:
        heading = segment.type.replace("_", " ").title()
        if segment.property_key:
            record = next(
                (p for p in bundle.properties if p.property_key == segment.property_key), None
            )
            if record:
                price = f"${record.price:,}" if record.price else "price unknown"
                heading = f"#{segment.rank} — {record.town} ({price})"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(segment.spoken_text)
        if segment.evidence_refs:
            lines.append("")
            lines.append(f"*Evidence:* {', '.join(segment.evidence_refs)}")
        lines.append("")
    return "\n".join(lines)


def segment_types(script: Script) -> list[SegmentType]:
    return [segment.type for segment in script.segments]
