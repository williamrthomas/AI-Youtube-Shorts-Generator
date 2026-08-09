"""SQLAlchemy 2.x models and session management (§10).

SQLite in WAL mode is the version-1 store. The database also provides the job
lock and stage status, which is why no separate queue product is needed at one
episode per day (§9.3).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from mpvf.models.domain import new_id, utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _pk(prefix: str):
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


class SearchTemplateRow(Base):
    __tablename__ = "search_templates"

    id: Mapped[str] = _pk("tpl")
    slug: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    supersedes_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (UniqueConstraint("slug", "version", name="uq_template_slug_version"),)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = _pk("run")
    template_id: Mapped[str | None] = mapped_column(
        ForeignKey("search_templates.id"), nullable=True
    )
    template_slug: Mapped[str] = mapped_column(String(80), index=True, default="")
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(40), index=True, default="scheduled")
    mode: Mapped[str] = mapped_column(String(30), default="private_upload")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(32), default="")
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_dir: Mapped[str] = mapped_column(String(500), default="")
    lock_token: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    stages: Mapped[list[StageRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list[EventRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class StageRow(Base):
    """Per-stage status, enabling rerun of a single stage (§2.3, FR-198)."""

    __tablename__ = "run_stages"

    id: Mapped[str] = _pk("stg")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(40), index=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str] = mapped_column(String(32), default="")
    output_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[RunRow] = relationship(back_populates="stages")

    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_stage_run_name"),)


class PropertyRow(Base):
    __tablename__ = "properties"

    id: Mapped[str] = _pk("prop")
    property_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    normalized_address: Mapped[str] = mapped_column(String(300), index=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(4), default="ME")
    postal_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    county: Mapped[str | None] = mapped_column(String(60), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_featured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_featured_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_featured_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ListingRow(Base):
    __tablename__ = "listings"

    id: Mapped[str] = _pk("lst")
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    mls_number: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    canonical_status: Mapped[str] = mapped_column(String(30), default="unknown")
    canonical_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    property_type: Mapped[str] = mapped_column(String(30), default="other")
    brokerage_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    first_listed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ListingSnapshotRow(Base):
    __tablename__ = "listing_snapshots"

    id: Mapped[str] = _pk("snp")
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_name: Mapped[str] = mapped_column(String(60), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds: Mapped[float | None] = mapped_column(Float, nullable=True)
    baths: Mapped[float | None] = mapped_column(Float, nullable=True)
    square_feet: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acres: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hoa_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    waterfront_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    facts_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_path: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RunCandidateRow(Base):
    __tablename__ = "run_candidates"

    id: Mapped[str] = _pk("rc")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    property_key: Mapped[str] = mapped_column(String(120), index=True)
    listing_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    alternate: Mapped[bool] = mapped_column(Boolean, default=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    component_scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_lock: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("run_id", "property_key", name="uq_candidate_run_prop"),)


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[str] = _pk("src")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    property_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    type: Mapped[str] = mapped_column(String(30), default="research")
    url: Mapped[str] = mapped_column(String(900))
    title: Mapped[str] = mapped_column(String(400), default="")
    publisher: Mapped[str] = mapped_column(String(200), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reliability_tier: Mapped[str] = mapped_column(String(2), default="C")


class ClaimRow(Base):
    __tablename__ = "claims"

    id: Mapped[str] = _pk("clm")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    property_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_ids_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    claim_type: Mapped[str] = mapped_column(String(30), default="listing_fact")
    normalized_text: Mapped[str] = mapped_column(Text)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    eligible_for_script: Mapped[bool] = mapped_column(Boolean, default=True)
    conflict_state: Mapped[str] = mapped_column(String(20), default="none")


class AssetRow(Base):
    __tablename__ = "assets"

    id: Mapped[str] = _pk("ast")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    property_key: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default="listing_image")
    category: Mapped[str] = mapped_column(String(30), default="unknown")
    source_url: Mapped[str] = mapped_column(String(900), default="")
    local_path: Mapped[str] = mapped_column(String(500), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True, default="")
    perceptual_hash: Mapped[str] = mapped_column(String(32), index=True, default="")
    credit_text: Mapped[str] = mapped_column(String(400), default="")
    license_text: Mapped[str] = mapped_column(String(400), default="")
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    crop_focus_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScriptRow(Base):
    __tablename__ = "scripts"

    id: Mapped[str] = _pk("scr")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    total_words: Mapped[int] = mapped_column(Integer, default=0)
    estimated_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_hash: Mapped[str] = mapped_column(String(32), default="")
    evidence_map_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generator_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScriptSegmentRow(Base):
    __tablename__ = "script_segments"

    id: Mapped[str] = _pk("seg")
    script_id: Mapped[str] = mapped_column(ForeignKey("scripts.id"), index=True)
    type: Mapped[str] = mapped_column(String(30))
    property_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    spoken_text: Mapped[str] = mapped_column(Text, default="")
    on_screen_text_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    asset_plan_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_refs_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_state: Mapped[str] = mapped_column(String(20), default="pending")


class RenderRow(Base):
    __tablename__ = "renders"

    id: Mapped[str] = _pk("rnd")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile: Mapped[str] = mapped_column(String(30), default="master")
    state: Mapped[str] = mapped_column(String(20), default="pending")
    preview_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    master_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    codec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    command_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(32), default="")
    qa_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicationRow(Base):
    __tablename__ = "publications"

    id: Mapped[str] = _pk("pub")
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    privacy: Mapped[str] = mapped_column(String(20), default="private")
    title: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    playlist: Mapped[str] = mapped_column(String(200), default="")
    thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    upload_state: Mapped[str] = mapped_column(String(20), default="pending")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_state: Mapped[str] = mapped_column(String(30), default="unknown")
    api_response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EventRow(Base):
    """Append-only operational event stream (§10.1)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True, nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    severity: Mapped[str] = mapped_column(String(10), default="info")
    code: Mapped[str] = mapped_column(String(60), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    run: Mapped[RunRow | None] = relationship(back_populates="events")


class Database:
    """Thin wrapper owning the engine and session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            _enable_sqlite_pragmas(self.engine)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _enable_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
