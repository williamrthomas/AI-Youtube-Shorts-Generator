"""Pydantic domain models used at module boundaries (§22).

These are the shapes that flow between adapters, normalization, selection,
research, scripting, speech and render. Anything crossing a module boundary is
validated here so a bad upstream value fails loudly at the seam.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from mpvf.config.templates import ListingStatus, PropertyType


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Short, sortable-ish, prefixed identifier."""

    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


class SourceHealth(BaseModel):
    name: str
    available: bool
    detail: str = ""
    checked_at: datetime = Field(default_factory=utcnow)
    challenge_detected: bool = False


class DiscoveryQuery(BaseModel):
    """A single discovery instruction handed to a source adapter."""

    template_slug: str
    search_urls: list[str]
    max_pages: int = 4
    max_cards_per_page: int = 60
    state: str = "ME"


class RawListingCard(BaseModel):
    """Unparsed listing card captured from a search results page (FR-011)."""

    source: str
    source_url: str
    html: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=utcnow)


class RawListingPage(BaseModel):
    source: str
    url: str
    html: str = ""
    screenshot_path: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=utcnow)


class WaterfrontFacts(BaseModel):
    owned_frontage_feet: float | None = None
    water_body: str | None = None
    frontage_type: Literal["ocean", "lake", "river", "pond", "bay", "none", "unknown"] = "unknown"
    deeded_access: bool | None = None
    water_view: bool | None = None


class ListingObservation(BaseModel):
    """One source's observation of one listing at one point in time (FR-022)."""

    observation_id: str = Field(default_factory=lambda: new_id("obs"))
    source: str
    source_url: str
    listing_url: str | None = None
    source_property_id: str | None = None
    mls_number: str | None = None

    raw_address: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    price: int | None = None
    previous_price: int | None = None
    price_change_at: datetime | None = None
    status: ListingStatus = "unknown"
    property_type: PropertyType = "other"

    beds: float | None = None
    baths: float | None = None
    square_feet: int | None = None
    acres: float | None = None
    year_built: int | None = None
    hoa_fee: float | None = None
    seasonal: bool | None = None
    waterfront: WaterfrontFacts = Field(default_factory=WaterfrontFacts)

    brokerage_name: str | None = None
    agent_name: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    description_text: str | None = None
    listed_at: datetime | None = None
    source_updated_at: datetime | None = None
    captured_at: datetime = Field(default_factory=utcnow)
    facts: dict[str, Any] = Field(default_factory=dict)

    def usable_image_count(self) -> int:
        return len({url for url in self.image_urls if url})


class FieldComparison(BaseModel):
    """Discovery vs verification agreement for a single field (FR-033)."""

    field: str
    discovery_value: Any = None
    verification_value: Any = None
    agrees: bool = True
    material: bool = False
    confidence: float = 1.0
    note: str = ""


class VerificationResult(BaseModel):
    property_key: str
    verified: bool
    verification_source: str | None = None
    verification_url: str | None = None
    observation: ListingObservation | None = None
    comparisons: list[FieldComparison] = Field(default_factory=list)
    confidence: float = 0.0
    blocking_conflicts: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_conflicts)


class PropertyRecord(BaseModel):
    """Canonical property with its merged, verified facts."""

    property_id: str = Field(default_factory=lambda: new_id("prop"))
    property_key: str
    normalized_address: str
    city: str | None = None
    state: str = "ME"
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    county: str | None = None

    observations: list[ListingObservation] = Field(default_factory=list)
    canonical: ListingObservation
    verification: VerificationResult | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)

    @property
    def price(self) -> int | None:
        return self.canonical.price

    @property
    def town(self) -> str:
        return self.city or "Maine"


class ScoreBreakdown(BaseModel):
    theme_fit: float = 0.0
    visual_strength: float = 0.0
    story_value: float = 0.0
    evidence_quality: float = 0.0
    freshness: float = 0.0
    geographic_diversity: float = 0.0
    asset_completeness: float = 0.0

    def total(self) -> float:
        return round(sum(self.model_dump().values()), 2)


class Candidate(BaseModel):
    """A property being considered for an episode (FR-040/041)."""

    candidate_id: str = Field(default_factory=lambda: new_id("cand"))
    property: PropertyRecord
    eligible: bool = True
    selected: bool = False
    alternate: bool = False
    rank: int | None = None
    scores: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    total_score: float = 0.0
    theme_signals: list[str] = Field(default_factory=list)
    selection_reason: str = ""
    exclusion_reason: str | None = None
    manual_lock: bool = False

    @property
    def property_key(self) -> str:
        return self.property.property_key


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

ReliabilityTier = Literal["A", "B", "C", "D"]
ClaimType = Literal[
    "listing_fact",
    "price",
    "status",
    "location",
    "measurement",
    "history",
    "context",
    "attribution",
    "proximity",
]


class SourceRecord(BaseModel):
    source_id: str = Field(default_factory=lambda: new_id("src"))
    property_key: str | None = None
    type: Literal["discovery", "verification", "research", "media", "reference"] = "research"
    url: str
    title: str = ""
    publisher: str = ""
    retrieved_at: datetime = Field(default_factory=utcnow)
    content_hash: str = ""
    local_path: str | None = None
    reliability_tier: ReliabilityTier = "C"
    excerpt: str = ""


class Claim(BaseModel):
    """An atomic fact eligible (or not) for narration (§7.8)."""

    claim_id: str = Field(default_factory=lambda: new_id("clm"))
    property_key: str | None = None
    text: str
    type: ClaimType = "listing_fact"
    attribute: str | None = Field(
        default=None,
        description=(
            "Which property attribute this asserts (asking_price, beds, frontage_feet...). "
            "Two claims conflict only when they assert the same attribute."
        ),
    )
    value: Any = None
    unit: str | None = None
    sources: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    eligible_for_script: bool = True
    conflict_state: Literal["none", "conflicting", "resolved", "unresolved"] = "none"
    scope: Literal["property", "town", "region"] = "property"
    note: str = ""

    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.property_key}|{self.text}".encode()).hexdigest()[:16]


class PronunciationEntry(BaseModel):
    term: str
    phonetic: str
    note: str = ""
    watchlist: bool = False


class BrokerCredit(BaseModel):
    property_key: str
    brokerage_name: str | None = None
    agent_name: str | None = None
    verification_url: str | None = None

    def spoken(self) -> str:
        if self.agent_name and self.brokerage_name:
            return f"Listed by {self.agent_name} of {self.brokerage_name}"
        if self.brokerage_name:
            return f"Listed by {self.brokerage_name}"
        if self.agent_name:
            return f"Listed by {self.agent_name}"
        return "Listing brokerage credited in the description"


class EvidenceBundle(BaseModel):
    """Everything the writer is allowed to know (§7.8)."""

    bundle_version: int = 1
    run_id: str
    template_slug: str
    template_version: int
    generated_at: datetime = Field(default_factory=utcnow)
    checked_at: datetime = Field(default_factory=utcnow)

    properties: list[PropertyRecord] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    assets: list[AssetRecord] = Field(default_factory=list)
    pronunciations: list[PronunciationEntry] = Field(default_factory=list)
    broker_credits: list[BrokerCredit] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)
    uncertain_claims: list[str] = Field(default_factory=list)

    def claims_for(self, property_key: str) -> list[Claim]:
        return [c for c in self.claims if c.property_key == property_key]

    def eligible_claims_for(self, property_key: str) -> list[Claim]:
        return [c for c in self.claims_for(property_key) if c.eligible_for_script]

    def assets_for(self, property_key: str) -> list[AssetRecord]:
        return [a for a in self.assets if a.property_key == property_key and a.selected]

    def credit_for(self, property_key: str) -> BrokerCredit | None:
        for credit in self.broker_credits:
            if credit.property_key == property_key:
                return credit
        return None

    def content_hash(self) -> str:
        payload = self.model_dump_json(exclude={"generated_at", "checked_at"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------

AssetCategory = Literal[
    "exterior",
    "aerial",
    "water_view",
    "kitchen",
    "living_room",
    "bedroom",
    "bathroom",
    "detail",
    "land",
    "map",
    "floor_plan",
    "agent_logo",
    "unknown",
]


class CropFocus(BaseModel):
    x: float = 0.5
    y: float = 0.5
    zoom_start: float = 1.0
    zoom_end: float = 1.08


class AssetRecord(BaseModel):
    asset_id: str = Field(default_factory=lambda: new_id("ast"))
    property_key: str | None = None
    source_id: str | None = None
    kind: Literal["listing_image", "context_image", "map", "graphic", "music", "sfx"] = (
        "listing_image"
    )
    category: AssetCategory = "unknown"
    source_url: str = ""
    source_page: str = ""
    local_path: str = ""
    width: int = 0
    height: int = 0
    mime_type: str = ""
    sha256: str = ""
    perceptual_hash: str = ""
    credit_text: str = ""
    license_text: str = ""
    quality_score: float = 0.0
    selected: bool = False
    hero: bool = False
    locked: bool = False
    excluded: bool = False
    crop_focus: CropFocus = Field(default_factory=CropFocus)
    retrieved_at: datetime = Field(default_factory=utcnow)

    def megapixels(self) -> float:
        return round((self.width * self.height) / 1_000_000, 2)


EvidenceBundle.model_rebuild()


# --------------------------------------------------------------------------
# Script
# --------------------------------------------------------------------------

SegmentType = Literal[
    "cold_open",
    "intro",
    "property",
    "transition",
    "comparison",
    "closing",
    "disclaimer",
]


class OnScreenText(BaseModel):
    kind: Literal["chapter_card", "callout", "credit", "disclosure", "lower_third"] = "callout"
    text: str
    start_offset: float = 0.0
    duration: float = 3.0


class ScriptSegment(BaseModel):
    segment_id: str = Field(default_factory=lambda: new_id("seg"))
    type: SegmentType
    order: int
    property_key: str | None = None
    rank: int | None = None
    spoken_text: str
    evidence_refs: list[str] = Field(default_factory=list)
    on_screen_text: list[OnScreenText] = Field(default_factory=list)
    asset_plan: list[str] = Field(default_factory=list)
    pronunciation_notes: list[str] = Field(default_factory=list)
    estimated_seconds: float = 0.0
    locked: bool = False
    validation_state: Literal["pending", "passed", "failed"] = "pending"
    validation_notes: list[str] = Field(default_factory=list)

    def word_count(self) -> int:
        return len(self.spoken_text.split())


class Script(BaseModel):
    script_id: str = Field(default_factory=lambda: new_id("scr"))
    run_id: str
    version: int = 1
    template_slug: str
    evidence_hash: str = ""
    segments: list[ScriptSegment] = Field(default_factory=list)
    citation_map: dict[str, list[str]] = Field(default_factory=dict)
    generator: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "validated", "rejected", "approved"] = "draft"

    def word_count(self) -> int:
        return sum(segment.word_count() for segment in self.segments)

    def estimated_seconds(self) -> float:
        return round(sum(segment.estimated_seconds for segment in self.segments), 1)

    def property_segments(self) -> list[ScriptSegment]:
        return [s for s in self.segments if s.type == "property"]

    def spoken_text(self) -> str:
        return "\n\n".join(segment.spoken_text for segment in self.segments)


# --------------------------------------------------------------------------
# Speech / captions / render / QA
# --------------------------------------------------------------------------


class NarrationSegment(BaseModel):
    segment_id: str
    audio_path: str
    duration_seconds: float
    sample_rate: int = 24000
    words: int = 0
    engine: str = "kokoro"
    voice: str = ""
    warnings: list[str] = Field(default_factory=list)


class CaptionCue(BaseModel):
    index: int
    start: float
    end: float
    lines: list[str]

    def text(self) -> str:
        return " ".join(self.lines)


class SceneAsset(BaseModel):
    asset_id: str
    start: float
    duration: float
    crop_focus: CropFocus = Field(default_factory=CropFocus)
    transition: Literal["cut", "crossfade", "dip"] = "crossfade"


class Scene(BaseModel):
    scene_id: str = Field(default_factory=lambda: new_id("scn"))
    kind: Literal["title", "chapter_card", "photo", "map", "fact_card", "comparison", "closing"] = (
        "photo"
    )
    segment_id: str | None = None
    property_key: str | None = None
    start: float
    duration: float
    assets: list[SceneAsset] = Field(default_factory=list)
    overlays: list[OnScreenText] = Field(default_factory=list)
    svg_path: str | None = None
    notes: str = ""


class ScenePlan(BaseModel):
    run_id: str
    version: int = 1
    fps: int = 30
    width: int = 1920
    height: int = 1080
    scenes: list[Scene] = Field(default_factory=list)
    total_duration: float = 0.0
    audio_path: str | None = None
    music_track: str | None = None

    def content_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()[:16]


class QAFinding(BaseModel):
    check: str
    category: Literal["factual", "editorial", "visual", "audio", "technical"]
    severity: Literal["info", "warning", "error"] = "error"
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    repairable: bool = False


class QAReport(BaseModel):
    run_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    findings: list[QAFinding] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    passed: bool = False

    @property
    def errors(self) -> list[QAFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[QAFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    def finalize(self) -> QAReport:
        self.passed = not self.errors
        return self


class PublicationMetadata(BaseModel):
    title: str
    title_alternates: list[str] = Field(default_factory=list)
    description: str
    tags: list[str] = Field(default_factory=list)
    playlist: str = ""
    category_id: str = "19"
    privacy: Literal["private", "unlisted", "public"] = "private"
    made_for_kids: bool = False
    embeddable: bool = True
    language: str = "en"
    scheduled_publish_at: datetime | None = None
    chapters: list[dict[str, Any]] = Field(default_factory=list)


class PublicationResult(BaseModel):
    run_id: str
    video_id: str | None = None
    url: str | None = None
    privacy: str = "private"
    upload_state: Literal["pending", "uploaded", "scheduled", "published", "failed"] = "pending"
    uploaded_at: datetime | None = None
    scheduled_for: datetime | None = None
    processing_state: str = "unknown"
    api_response: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
