"""YouTube metadata generation (§7.17, §18).

Titles must describe what the video actually shows; descriptions carry the
check timestamp, chapters, every listing link, broker credits and research
sources. All of it is built from the evidence bundle, not from prose.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import (
    EvidenceBundle,
    NarrationSegment,
    PublicationMetadata,
    Script,
)

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS = 25


def _selected(bundle: EvidenceBundle):
    return sorted(
        [c for c in bundle.candidates if c.selected],
        key=lambda c: c.rank or 0,
        reverse=True,
    )


def build_titles(bundle: EvidenceBundle, template: SearchTemplate) -> list[str]:
    """Three accurate title candidates (FR-170)."""

    selected = _selected(bundle)
    count = len(selected)
    prices = [c.property.price for c in selected if c.property.price]
    towns = [c.property.town for c in selected if c.property.town]
    ceiling = _round_ceiling(max(prices)) if prices else None

    titles: list[str] = []
    for pattern in template.title_patterns:
        try:
            titles.append(
                pattern.format(
                    count=count,
                    ceiling=f"${ceiling:,}" if ceiling else "",
                    town=towns[0] if towns else "Maine",
                    name=template.name,
                )
            )
        except (KeyError, IndexError):
            continue

    if ceiling:
        titles.append(f"{count} Maine Homes for Sale Under ${ceiling:,}")
    if towns:
        titles.append(f"{count} Maine Homes We'd Actually Buy — {towns[-1]} to {towns[0]}")
    titles.append(template.name)

    seen: set[str] = set()
    unique: list[str] = []
    for title in titles:
        cleaned = re.sub(r"\s+", " ", title).strip()[:MAX_TITLE_CHARS]
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            unique.append(cleaned)
    return unique[:3]


def _round_ceiling(price: int) -> int:
    """Round a real maximum up to a title-friendly number without lying."""

    for step in (50_000, 100_000, 250_000, 500_000, 1_000_000):
        candidate = ((price // step) + 1) * step
        if candidate - price <= step * 0.8:
            return candidate
    return price


def build_chapters(
    script: Script,
    narrations: list[NarrationSegment],
    bundle: EvidenceBundle,
    gap_seconds: float = 0.35,
) -> list[dict[str, Any]]:
    """Timestamped chapters derived from actual narration lengths."""

    by_id = {narration.segment_id: narration for narration in narrations}
    chapters: list[dict[str, Any]] = []
    clock = 0.0
    for segment in script.segments:
        narration = by_id.get(segment.segment_id)
        duration = narration.duration_seconds if narration else segment.estimated_seconds
        label: str | None = None
        if segment.type == "cold_open" and not chapters:
            label = "Intro"
        elif segment.type == "property" and segment.property_key:
            record = next(
                (p for p in bundle.properties if p.property_key == segment.property_key), None
            )
            if record:
                price = f"${record.price:,}" if record.price else ""
                label = f"#{segment.rank} {record.town} {price}".strip()
        elif segment.type in {"comparison", "closing"}:
            label = "Which one would you take?"
        if label:
            chapters.append({"start": round(clock, 2), "title": label[:100]})
        clock += duration + gap_seconds
    # YouTube requires the first chapter to start at 0.
    if chapters and chapters[0]["start"] > 0:
        chapters[0]["start"] = 0.0
    return chapters


def format_timestamp(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_description(
    bundle: EvidenceBundle,
    template: SearchTemplate,
    chapters: list[dict[str, Any]],
    hook: str | None = None,
    call_to_action: str = "",
) -> str:
    """The §18 description structure, in order."""

    selected = _selected(bundle)
    stamp = bundle.checked_at.strftime("%B %d, %Y at %I:%M %p %Z").strip()
    lines: list[str] = []

    if hook:
        lines += [hook.strip(), ""]
    else:
        towns = [c.property.town for c in selected if c.property.town]
        lead = towns[0] if towns else "the Maine coast"
        lines += [
            f"{template.editorial_promise or template.name}. "
            f"We start in {lead} and finish with the one that made the list worth doing.",
            "",
        ]

    lines += [f"Prices and availability checked {stamp}.", ""]

    if chapters:
        lines.append("CHAPTERS")
        for chapter in chapters:
            lines.append(f"{format_timestamp(chapter['start'])} {chapter['title']}")
        lines.append("")

    lines.append("THE PROPERTIES")
    for candidate in selected:
        record = candidate.property
        credit = bundle.credit_for(record.property_key)
        price = f"${record.price:,}" if record.price else "Price on request"
        address = record.canonical.raw_address or record.normalized_address.title()
        lines.append(f"#{candidate.rank} — {address}, {record.town}, ME — {price}")
        url = (
            record.verification.verification_url
            if record.verification and record.verification.verification_url
            else record.canonical.listing_url
        )
        if url:
            lines.append(f"Listing: {url}")
        if credit and (credit.brokerage_name or credit.agent_name):
            lines.append(f"{credit.spoken()}")
        lines.append("")

    research = [
        source
        for source in bundle.sources
        if source.type == "research" and source.reliability_tier in {"A", "B", "C"}
    ]
    if research:
        lines.append("SOURCES AND FURTHER READING")
        for source in research[:12]:
            title = source.title or source.publisher or source.url
            lines.append(f"{title} — {source.url}")
        lines.append("")

    media_credits = [asset.credit_text for asset in bundle.assets if asset.credit_text]
    if media_credits:
        lines.append("IMAGE CREDITS")
        for credit_text in sorted(set(media_credits))[:12]:
            lines.append(credit_text)
        lines.append("")

    lines.append(
        call_to_action
        or "If you want more Maine listings pulled apart like this, subscribe — one episode a day."
    )
    lines.append("")
    lines.append(
        "Listings change fast. Prices, status and details can move after upload; confirm anything "
        "that matters with the listing professional before you act on it. This is not advice to buy."
    )
    return "\n".join(lines)[:MAX_DESCRIPTION_CHARS]


def build_tags(bundle: EvidenceBundle, template: SearchTemplate) -> list[str]:
    tags = ["Maine real estate", "Maine homes for sale", "Maine"]
    for candidate in _selected(bundle):
        town = candidate.property.town
        if town and town != "Maine":
            tags.append(f"{town} Maine")
        for signal in candidate.theme_signals[:2]:
            tags.append(signal.replace("_", " "))
    tags.append(template.name.lower())
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        cleaned = re.sub(r"\s+", " ", tag).strip().lower()
        if cleaned and cleaned not in seen and len(cleaned) <= 40:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique[:MAX_TAGS]


def next_publish_time(
    template: SearchTemplate,
    now: datetime | None = None,
    minimum_lead_hours: float = 2.0,
) -> datetime:
    """The next slot at the template's configured publish hour (FR-175)."""

    now = now or datetime.now(UTC)
    target = now.replace(
        hour=template.publishing.earliest_publish_hour, minute=0, second=0, microsecond=0
    )
    if target - now < timedelta(hours=minimum_lead_hours):
        target += timedelta(days=1)
    return target


def build_metadata(
    bundle: EvidenceBundle,
    template: SearchTemplate,
    script: Script,
    narrations: list[NarrationSegment],
    hook: str | None = None,
    call_to_action: str = "",
    schedule: bool = False,
    now: datetime | None = None,
) -> PublicationMetadata:
    titles = build_titles(bundle, template)
    chapters = build_chapters(script, narrations, bundle)
    return PublicationMetadata(
        title=titles[0] if titles else template.name,
        title_alternates=titles[1:],
        description=build_description(bundle, template, chapters, hook, call_to_action),
        tags=build_tags(bundle, template),
        playlist=template.publishing.playlist,
        category_id=template.publishing.category_id,
        privacy=template.publishing.privacy,
        made_for_kids=template.publishing.made_for_kids,
        language=template.publishing.language,
        scheduled_publish_at=next_publish_time(template, now) if schedule else None,
        chapters=chapters,
    )
