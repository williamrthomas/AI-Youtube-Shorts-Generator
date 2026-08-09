"""Initial MPVF schema.

Creates every table in §10 of the specification: templates, runs, stages,
properties, listings, snapshots, candidates, sources, claims, assets, scripts,
script segments, renders, publications and the append-only event stream.

Revision ID: 0001_initial
Revises:
Created: 2026-08-09 16:43:40.149147
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "properties",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=False),
        sa.Column("normalized_address", sa.String(length=300), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=4), nullable=False),
        sa.Column("postal_code", sa.String(length=12), nullable=True),
        sa.Column("county", sa.String(length=60), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_featured_at", sa.DateTime(), nullable=True),
        sa.Column("last_featured_price", sa.Integer(), nullable=True),
        sa.Column("last_featured_run_id", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_properties_normalized_address"), ["normalized_address"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_properties_property_key"), ["property_key"], unique=True
        )

    op.create_table(
        "search_templates",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", "version", name="uq_template_slug_version"),
    )
    with op.batch_alter_table("search_templates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_search_templates_slug"), ["slug"], unique=False)

    op.create_table(
        "listings",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("property_id", sa.String(length=40), nullable=False),
        sa.Column("mls_number", sa.String(length=40), nullable=True),
        sa.Column("canonical_status", sa.String(length=30), nullable=False),
        sa.Column("canonical_price", sa.Integer(), nullable=True),
        sa.Column("property_type", sa.String(length=30), nullable=False),
        sa.Column("brokerage_name", sa.String(length=200), nullable=True),
        sa.Column("agent_name", sa.String(length=200), nullable=True),
        sa.Column("canonical_url", sa.String(length=700), nullable=True),
        sa.Column("first_listed_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_listings_mls_number"), ["mls_number"], unique=False)
        batch_op.create_index(batch_op.f("ix_listings_property_id"), ["property_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("template_id", sa.String(length=40), nullable=True),
        sa.Column("template_slug", sa.String(length=80), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("input_hash", sa.String(length=32), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("failure_code", sa.String(length=60), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("artifact_dir", sa.String(length=500), nullable=False),
        sa.Column("lock_token", sa.String(length=60), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["search_templates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_state"), ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_template_slug"), ["template_slug"], unique=False)

    op.create_table(
        "assets",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=True),
        sa.Column("source_id", sa.String(length=40), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("source_url", sa.String(length=900), nullable=False),
        sa.Column("local_path", sa.String(length=500), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=32), nullable=False),
        sa.Column("credit_text", sa.String(length=400), nullable=False),
        sa.Column("license_text", sa.String(length=400), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("crop_focus_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_assets_perceptual_hash"), ["perceptual_hash"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_assets_property_key"), ["property_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_assets_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_assets_sha256"), ["sha256"], unique=False)

    op.create_table(
        "claims",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=True),
        sa.Column("source_ids_json", sa.JSON(), nullable=False),
        sa.Column("claim_type", sa.String(length=30), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("eligible_for_script", sa.Boolean(), nullable=False),
        sa.Column("conflict_state", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_claims_run_id"), ["run_id"], unique=False)

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_run_id"), ["run_id"], unique=False)

    op.create_table(
        "listing_snapshots",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("listing_id", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=40), nullable=True),
        sa.Column("source_name", sa.String(length=60), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("beds", sa.Float(), nullable=True),
        sa.Column("baths", sa.Float(), nullable=True),
        sa.Column("square_feet", sa.Integer(), nullable=True),
        sa.Column("acres", sa.Float(), nullable=True),
        sa.Column("year_built", sa.Integer(), nullable=True),
        sa.Column("hoa_fee", sa.Float(), nullable=True),
        sa.Column("waterfront_json", sa.JSON(), nullable=False),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("raw_path", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("listing_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_listing_snapshots_captured_at"), ["captured_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_listing_snapshots_listing_id"), ["listing_id"], unique=False
        )

    op.create_table(
        "publications",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("youtube_video_id", sa.String(length=40), nullable=True),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.Column("privacy", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("playlist", sa.String(length=200), nullable=False),
        sa.Column("thumbnail_path", sa.String(length=500), nullable=True),
        sa.Column("upload_state", sa.String(length=20), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("processing_state", sa.String(length=30), nullable=False),
        sa.Column("api_response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_publications_idempotency_key"), ["idempotency_key"], unique=True
        )
        batch_op.create_index(batch_op.f("ix_publications_run_id"), ["run_id"], unique=False)

    op.create_table(
        "renders",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile", sa.String(length=30), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("preview_path", sa.String(length=500), nullable=True),
        sa.Column("master_path", sa.String(length=500), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("codec_json", sa.JSON(), nullable=False),
        sa.Column("command_manifest_json", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=32), nullable=False),
        sa.Column("qa_report_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("renders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_renders_run_id"), ["run_id"], unique=False)

    op.create_table(
        "run_candidates",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=False),
        sa.Column("listing_id", sa.String(length=40), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("alternate", sa.Boolean(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("component_scores_json", sa.JSON(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("manual_lock", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "property_key", name="uq_candidate_run_prop"),
    )
    with op.batch_alter_table("run_candidates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_run_candidates_property_key"), ["property_key"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_run_candidates_run_id"), ["run_id"], unique=False)

    op.create_table(
        "run_stages",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=32), nullable=False),
        sa.Column("output_path", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "name", name="uq_stage_run_name"),
    )
    with op.batch_alter_table("run_stages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_stages_name"), ["name"], unique=False)
        batch_op.create_index(batch_op.f("ix_run_stages_run_id"), ["run_id"], unique=False)

    op.create_table(
        "scripts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_words", sa.Integer(), nullable=False),
        sa.Column("estimated_seconds", sa.Float(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=32), nullable=False),
        sa.Column("evidence_map_json", sa.JSON(), nullable=False),
        sa.Column("generator_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scripts_run_id"), ["run_id"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("url", sa.String(length=900), nullable=False),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("local_path", sa.String(length=500), nullable=True),
        sa.Column("reliability_tier", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sources_run_id"), ["run_id"], unique=False)

    op.create_table(
        "script_segments",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("script_id", sa.String(length=40), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("property_key", sa.String(length=120), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("spoken_text", sa.Text(), nullable=False),
        sa.Column("on_screen_text_json", sa.JSON(), nullable=False),
        sa.Column("asset_plan_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("validation_state", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["script_id"],
            ["scripts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("script_segments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_script_segments_script_id"), ["script_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("script_segments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_script_segments_script_id"))

    op.drop_table("script_segments")
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sources_run_id"))

    op.drop_table("sources")
    with op.batch_alter_table("scripts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scripts_run_id"))

    op.drop_table("scripts")
    with op.batch_alter_table("run_stages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_stages_run_id"))
        batch_op.drop_index(batch_op.f("ix_run_stages_name"))

    op.drop_table("run_stages")
    with op.batch_alter_table("run_candidates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_candidates_run_id"))
        batch_op.drop_index(batch_op.f("ix_run_candidates_property_key"))

    op.drop_table("run_candidates")
    with op.batch_alter_table("renders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_renders_run_id"))

    op.drop_table("renders")
    with op.batch_alter_table("publications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_publications_run_id"))
        batch_op.drop_index(batch_op.f("ix_publications_idempotency_key"))

    op.drop_table("publications")
    with op.batch_alter_table("listing_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_listing_snapshots_listing_id"))
        batch_op.drop_index(batch_op.f("ix_listing_snapshots_captured_at"))

    op.drop_table("listing_snapshots")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_run_id"))
        batch_op.drop_index(batch_op.f("ix_events_created_at"))

    op.drop_table("events")
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_claims_run_id"))

    op.drop_table("claims")
    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_assets_sha256"))
        batch_op.drop_index(batch_op.f("ix_assets_run_id"))
        batch_op.drop_index(batch_op.f("ix_assets_property_key"))
        batch_op.drop_index(batch_op.f("ix_assets_perceptual_hash"))

    op.drop_table("assets")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_template_slug"))
        batch_op.drop_index(batch_op.f("ix_runs_state"))

    op.drop_table("runs")
    with op.batch_alter_table("listings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_listings_property_id"))
        batch_op.drop_index(batch_op.f("ix_listings_mls_number"))

    op.drop_table("listings")
    with op.batch_alter_table("search_templates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_search_templates_slug"))

    op.drop_table("search_templates")
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_properties_property_key"))
        batch_op.drop_index(batch_op.f("ix_properties_normalized_address"))

    op.drop_table("properties")
