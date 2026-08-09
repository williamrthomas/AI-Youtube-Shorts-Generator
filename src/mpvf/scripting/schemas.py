"""Script generation schemas (FR-083).

These are the shapes the model is constrained to. They are intentionally
narrow: the model chooses words and ordering; prices, statuses and credits are
injected from structured fields at finalization (FR-104).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SCHEMA_VERSION = "script-v1"


class DraftOnScreenText(BaseModel):
    kind: str = Field(
        default="callout",
        description="chapter_card, callout, credit, disclosure or lower_third",
    )
    text: str = Field(description="Five words or fewer for a callout")


class DraftSegment(BaseModel):
    type: str = Field(
        description="cold_open, intro, property, transition, comparison, closing or disclaimer"
    )
    property_key: str | None = Field(
        default=None, description="Required when type is 'property'; copy exactly from the brief"
    )
    spoken_text: str = Field(description="The narration for this segment, plain prose")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="claim_id values from the brief supporting every fact stated here",
    )
    on_screen_text: list[DraftOnScreenText] = Field(default_factory=list)
    asset_hint: str = Field(
        default="",
        description="Which image category should be on screen: exterior, water_view, kitchen...",
    )
    pronunciation_notes: list[str] = Field(default_factory=list)


class ScriptDraft(BaseModel):
    """The full model response for one episode."""

    segments: list[DraftSegment] = Field(min_length=3)
    notes: str = Field(
        default="", description="Anything the writer could not support with evidence"
    )


class SegmentRevision(BaseModel):
    """Response shape for a targeted rewrite of one segment (FR-092)."""

    spoken_text: str
    evidence_refs: list[str] = Field(default_factory=list)
    change_note: str = ""


class TitleCandidates(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=5)
    reasoning: str = ""


class DescriptionDraft(BaseModel):
    hook: str = Field(description="Two sentences, specific, no hype")
    call_to_action: str = ""
    tags: list[str] = Field(default_factory=list, max_length=25)


class AtomicClaim(BaseModel):
    """One factual assertion pulled back out of the finished script (FR-100)."""

    text: str
    kind: str = Field(description="numeric, status, attribution, proximity, historical or other")
    segment_index: int = 0


class AtomicClaims(BaseModel):
    claims: list[AtomicClaim] = Field(default_factory=list)


class EditorialCritique(BaseModel):
    """Second-pass editorial read (FR-105)."""

    repetition: list[str] = Field(default_factory=list)
    empty_hype: list[str] = Field(default_factory=list)
    awkward_transitions: list[str] = Field(default_factory=list)
    missing_comparison: bool = False
    suggested_fixes: list[str] = Field(default_factory=list)
