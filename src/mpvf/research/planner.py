"""Research query planning (FR-060, FR-067).

Queries are generated from structured facts, not from vibes, and they are
ordered so the highest-value question runs first: what makes *this* location
feel the way it does. Source sprawl is a stated anti-goal (§2.6), so the plan
is capped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import PropertyRecord

QueryKind = Literal["property", "town", "water_body", "landmark", "architecture", "theme"]


@dataclass(frozen=True)
class ResearchQuery:
    kind: QueryKind
    text: str
    scope: Literal["property", "town", "region"]
    priority: int
    property_key: str | None = None
    wiki_title: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "scope": self.scope,
            "priority": self.priority,
            "property_key": self.property_key,
            "wiki_title": self.wiki_title,
        }


_STYLE_WORDS = (
    "federal",
    "greek revival",
    "italianate",
    "victorian",
    "queen anne",
    "colonial",
    "cape",
    "shingle style",
    "gambrel",
    "farmhouse",
    "mid-century",
    "post and beam",
    "saltbox",
    "second empire",
)

_LANDMARK_WORDS = (
    "lighthouse",
    "state park",
    "national park",
    "acadia",
    "harbor",
    "island",
    "ferry",
    "historic district",
    "trail",
    "preserve",
    "boatyard",
    "ski area",
)


def _detect(text: str, words: tuple[str, ...]) -> list[str]:
    blob = (text or "").lower()
    return [word for word in words if word in blob]


def plan_queries(
    record: PropertyRecord,
    template: SearchTemplate,
    max_queries: int = 5,
) -> list[ResearchQuery]:
    """Build a small, ordered research plan for one property."""

    canonical = record.canonical
    town = record.city or ""
    description = canonical.description_text or ""
    queries: list[ResearchQuery] = []

    address_line = canonical.raw_address or record.normalized_address
    if address_line and canonical.year_built:
        queries.append(
            ResearchQuery(
                kind="property",
                text=f'"{address_line}" history {canonical.year_built}',
                scope="property",
                priority=0,
                property_key=record.property_key,
            )
        )
    elif address_line:
        queries.append(
            ResearchQuery(
                kind="property",
                text=f'"{address_line}" historic register',
                scope="property",
                priority=1,
                property_key=record.property_key,
            )
        )

    water_body = canonical.waterfront.water_body
    if water_body:
        queries.append(
            ResearchQuery(
                kind="water_body",
                text=f"{water_body} Maine",
                scope="region",
                priority=1,
                property_key=record.property_key,
                wiki_title=f"{water_body}",
            )
        )

    if town:
        queries.append(
            ResearchQuery(
                kind="town",
                text=f"{town}, Maine",
                scope="town",
                priority=2,
                property_key=record.property_key,
                wiki_title=f"{town}, Maine",
            )
        )

    for style in _detect(description, _STYLE_WORDS)[:1]:
        queries.append(
            ResearchQuery(
                kind="architecture",
                text=f"{style} architecture Maine {town}".strip(),
                scope="region",
                priority=3,
                property_key=record.property_key,
                wiki_title=f"{style.title()} architecture",
            )
        )

    for landmark in _detect(description, _LANDMARK_WORDS)[:1]:
        queries.append(
            ResearchQuery(
                kind="landmark",
                text=f"{town} {landmark} Maine".strip(),
                scope="region",
                priority=3,
                property_key=record.property_key,
            )
        )

    if template.theme_rules.any_of:
        theme_hint = template.theme_rules.any_of[0].replace("_", " ")
        queries.append(
            ResearchQuery(
                kind="theme",
                text=f"{town} Maine {theme_hint}".strip(),
                scope="region",
                priority=4,
                property_key=record.property_key,
            )
        )

    ordered = sorted(queries, key=lambda query: query.priority)
    seen: set[str] = set()
    unique: list[ResearchQuery] = []
    for query in ordered:
        key = query.text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(query)
    return unique[:max_queries]


def plan_for_lineup(
    records: list[PropertyRecord],
    template: SearchTemplate,
    max_per_property: int = 4,
) -> list[ResearchQuery]:
    plan: list[ResearchQuery] = []
    for record in records:
        plan.extend(plan_queries(record, template, max_queries=max_per_property))
    return plan
