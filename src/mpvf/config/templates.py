"""Search-template model and loader (§5).

Templates are user-editable YAML living outside application code (§22). They
are versioned; every run records the template version that produced it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

PropertyType = Literal[
    "single_family",
    "condo",
    "townhouse",
    "seasonal_residence",
    "multi_family",
    "farm",
    "land",
    "other",
]

ListingStatus = Literal[
    "active",
    "pending",
    "contingent",
    "active_under_contract",
    "sold",
    "off_market",
    "unknown",
]


class DiscoverySpec(BaseModel):
    adapter: str = "zillow"
    search_urls: list[str] = Field(default_factory=list)
    max_pages: int = 4
    max_cards_per_page: int = 60
    fallback_adapters: list[str] = Field(default_factory=list)


class HardFilters(BaseModel):
    status: list[ListingStatus] = Field(default_factory=lambda: ["active"])
    property_types: list[PropertyType] = Field(
        default_factory=lambda: ["single_family", "condo", "seasonal_residence"]
    )
    price_min: int | None = None
    price_max: int | None = None
    state: str = "ME"
    min_usable_images: int = 8
    min_year_built: int | None = None
    max_year_built: int | None = None
    min_acres: float | None = None
    towns: list[str] = Field(default_factory=list)
    counties: list[str] = Field(default_factory=list)


class ThemeRules(BaseModel):
    """Rule-based theme fit evaluated before any model assistance (FR-042)."""

    any_of: list[str] = Field(default_factory=list)
    all_of: list[str] = Field(default_factory=list)
    none_of: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    theme_fit: int = 25
    visual_strength: int = 25
    story_value: int = 15
    evidence_quality: int = 15
    freshness: int = 10
    geographic_diversity: int = 5
    asset_completeness: int = 5

    def total(self) -> int:
        return sum(self.model_dump().values())


class Exclusions(BaseModel):
    statuses: list[ListingStatus] = Field(
        default_factory=lambda: ["pending", "contingent", "sold", "off_market"]
    )
    featured_within_days: int = 120
    reprise_price_change_pct: float = 10.0
    land_only: bool = True
    auction: bool = True
    towns: list[str] = Field(default_factory=list)


class PublishingSpec(BaseModel):
    playlist: str = ""
    privacy: Literal["private", "unlisted", "public"] = "private"
    earliest_publish_hour: int = 17
    category_id: str = "19"  # Travel & Events
    made_for_kids: bool = False
    language: str = "en"


class DesignSpec(BaseModel):
    palette: str = "coastal"
    map_style: str = "chart"
    music_family: str = "warm_acoustic"
    music_energy: Literal["low", "medium", "high"] = "medium"
    allowed_tracks: list[str] = Field(default_factory=list)


class SearchTemplate(BaseModel):
    """A versioned editorial format (§5.1)."""

    version: int = 1
    slug: str
    name: str
    enabled: bool = True
    editorial_promise: str = ""
    schedule: str | None = None
    result_count: int = 5
    alternates_count: int = 1
    target_runtime_seconds: int = 420
    target_words: int = 980
    word_range: tuple[int, int] = (850, 1100)
    max_segment_words: int = 140
    max_history_beats: int = 2
    fallback_template: str | None = None
    min_candidate_quality: int = 60
    require_geographic_diversity: bool = True
    title_patterns: list[str] = Field(default_factory=list)
    tone: str = "informed Maine property enthusiast: observant, concise, honest"
    recurring_segments: list[str] = Field(default_factory=list)

    discovery: DiscoverySpec = Field(default_factory=DiscoverySpec)
    hard_filters: HardFilters = Field(default_factory=HardFilters)
    theme_rules: ThemeRules = Field(default_factory=ThemeRules)
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    exclusions: Exclusions = Field(default_factory=Exclusions)
    publishing: PublishingSpec = Field(default_factory=PublishingSpec)
    design: DesignSpec = Field(default_factory=DesignSpec)

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        if not value or not all(ch.isalnum() or ch in "-_" for ch in value):
            raise ValueError("slug must be alphanumeric with dashes or underscores")
        return value

    @field_validator("word_range")
    @classmethod
    def _word_range_ordered(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if low > high:
            raise ValueError("word_range must be (min, max)")
        return value

    def config_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def validation_report(self) -> list[str]:
        """Return human-readable problems that do not break parsing."""

        problems: list[str] = []
        if self.scoring.total() != 100:
            problems.append(f"scoring weights total {self.scoring.total()}, expected 100")
        if not self.discovery.search_urls:
            problems.append("no discovery search_urls configured")
        if self.result_count < 3:
            problems.append("result_count below 3 makes a weak episode")
        if not self.theme_rules.any_of and not self.theme_rules.all_of:
            problems.append("no theme rules: every listing will pass theme fit")
        if self.hard_filters.state != "ME":
            problems.append("hard_filters.state is not ME; MPVF targets Maine")
        if self.target_words < self.word_range[0] or self.target_words > self.word_range[1]:
            problems.append("target_words outside word_range")
        return problems


class TemplateRegistry:
    """Loads templates from a directory of YAML files."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(p for p in self.directory.iterdir() if p.suffix in {".yaml", ".yml"})

    def load_all(self, include_disabled: bool = True) -> list[SearchTemplate]:
        templates: list[SearchTemplate] = []
        for path in self._paths():
            payload: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            payload.setdefault("slug", path.stem)
            template = SearchTemplate.model_validate(payload)
            if include_disabled or template.enabled:
                templates.append(template)
        return templates

    def get(self, slug: str) -> SearchTemplate:
        for template in self.load_all():
            if template.slug == slug:
                return template
        raise KeyError(f"unknown template: {slug}")

    def try_get(self, slug: str) -> SearchTemplate | None:
        try:
            return self.get(slug)
        except KeyError:
            return None
