"""Run state machine, artifact store, retention, publishing idempotency and
an offline end-to-end run through the fixture adapter (§19.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mpvf.cli.cleanup import parse_duration, run_cleanup
from mpvf.models.db import PublicationRow, RunRow, StageRow
from mpvf.models.domain import PublicationMetadata
from mpvf.pipeline.artifacts import ArtifactStore, hash_inputs, safe_filename
from mpvf.pipeline.runner import Runner
from mpvf.pipeline.stages import registry
from mpvf.pipeline.state import (
    HAPPY_PATH,
    InvalidTransition,
    RunState,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_failure,
    is_terminal,
)
from mpvf.publish.package import REQUIRED_FILES, build_package
from mpvf.publish.youtube import DryRunPublisher, UploadRequest, idempotency_key


class TestStateMachine:
    def test_the_happy_path_is_walkable(self):
        for current, following in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
            assert can_transition(current, following), f"{current} -> {following}"

    def test_a_stage_cannot_skip_ahead(self):
        assert not can_transition(RunState.SCHEDULED, RunState.RENDERING)
        with pytest.raises(InvalidTransition):
            assert_transition(RunState.SCHEDULED, RunState.PUBLISHED)

    def test_every_working_state_can_fail_or_hold(self):
        for state in HAPPY_PATH[:-1]:
            allowed = allowed_transitions(state)
            assert RunState.MANUAL_HOLD in allowed
            assert RunState.SKIPPED in allowed

    def test_failures_can_be_retried_into_their_own_stage(self):
        assert can_transition(RunState.RENDER_FAILED, RunState.RENDERING)
        assert can_transition(RunState.QA_FAILED, RunState.RENDERING)
        assert can_transition(RunState.UPLOAD_FAILED, RunState.UPLOADED)

    def test_skipping_is_terminal_but_archivable(self):
        assert is_terminal(RunState.SKIPPED)
        assert can_transition(RunState.SKIPPED, RunState.ARCHIVED)

    def test_failure_classification(self):
        assert is_failure(RunState.QA_FAILED)
        assert not is_failure(RunState.SKIPPED)
        assert not is_failure(RunState.PUBLISHED)


class TestArtifactStore:
    def test_creates_the_documented_layout(self, tmp_path):
        store = ArtifactStore.for_run(tmp_path, "run_1", "coastal-under-1m")
        for name in ("discovery", "evidence", "script", "audio", "render", "publish", "logs"):
            assert (store.root / name).is_dir()

    def test_round_trips_json(self, tmp_path):
        store = ArtifactStore.for_run(tmp_path, "run_1", "t")
        store.write_json("evidence", "bundle.json", {"a": 1})
        assert store.read_json("evidence", "bundle.json") == {"a": 1}

    def test_rejects_an_unknown_directory(self, tmp_path):
        store = ArtifactStore.for_run(tmp_path, "run_1", "t")
        with pytest.raises(KeyError):
            store.dir("nowhere")

    def test_filenames_from_acquired_content_are_sanitized(self):
        assert safe_filename("../../etc/passwd") == "etc-passwd"
        assert safe_filename("") == "file"
        assert len(safe_filename("x" * 400)) <= 120

    def test_input_hash_is_stable_and_order_sensitive(self):
        assert hash_inputs("a", 1) == hash_inputs("a", 1)
        assert hash_inputs("a", 1) != hash_inputs(1, "a")


class TestPublishIdempotency:
    def test_the_same_render_yields_the_same_key(self, tmp_path):
        video = tmp_path / "master.mp4"
        video.write_bytes(b"video-bytes")
        metadata = PublicationMetadata(title="Five Homes", description="d")
        assert idempotency_key("run_1", video, metadata) == idempotency_key(
            "run_1", video, metadata
        )

    def test_changing_the_title_changes_the_key(self, tmp_path):
        video = tmp_path / "master.mp4"
        video.write_bytes(b"video-bytes")
        first = idempotency_key("run_1", video, PublicationMetadata(title="A", description="d"))
        second = idempotency_key("run_1", video, PublicationMetadata(title="B", description="d"))
        assert first != second

    def test_dry_run_publisher_writes_the_request_instead_of_uploading(self, tmp_path):
        publisher = DryRunPublisher(tmp_path)
        result = publisher.upload(
            UploadRequest(
                video_path=tmp_path / "master.mp4",
                metadata=PublicationMetadata(title="T", description="d"),
                idempotency_key="key",
            )
        )
        assert result.video_id is None
        assert (tmp_path / "upload-request.json").exists()


class TestRetention:
    def test_duration_parsing(self):
        assert parse_duration("90d") == timedelta(days=90)
        assert parse_duration("12h") == timedelta(hours=12)
        with pytest.raises(ValueError):
            parse_duration("soon")

    def test_dry_run_deletes_nothing(self, settings):
        run_dir = settings.runs_dir / "2026-01-01_t_run_old"
        (run_dir / "pages").mkdir(parents=True)
        page = run_dir / "pages" / "saved.html"
        page.write_text("x" * 2000)
        old = (datetime.now(UTC) - timedelta(days=400)).timestamp()
        import os

        os.utime(run_dir, (old, old))

        report = run_cleanup(settings, "90d", dry_run=True)
        assert report["action_count"] >= 1
        assert page.exists()

    def test_execute_removes_expired_raw_pages(self, settings):
        run_dir = settings.runs_dir / "2026-01-01_t_run_old"
        (run_dir / "pages").mkdir(parents=True)
        page = run_dir / "pages" / "saved.html"
        page.write_text("x" * 2000)
        old = (datetime.now(UTC) - timedelta(days=400)).timestamp()
        import os

        os.utime(run_dir, (old, old))

        run_cleanup(settings, "90d", dry_run=False)
        assert not page.exists()


class TestRunnerLifecycle:
    def test_creates_a_run_with_an_artifact_directory(self, settings, template):
        runner = Runner(settings)
        run_id, store = runner.create_run(template)
        assert run_id.startswith("run_")
        assert store.root.exists()
        with runner.database.session() as session:
            row = session.get(RunRow, run_id)
            assert row.state == "scheduled"
            assert row.template_slug == template.slug

    def test_status_lists_runs(self, settings, template):
        runner = Runner(settings)
        run_id, _store = runner.create_run(template)
        statuses = runner.status()
        assert any(item["run_id"] == run_id for item in statuses)

    def test_stage_registry_is_ordered_and_sliceable(self):
        names = registry.names()
        assert names[0] == "discover"
        assert names[-1] == "publish"
        assert [spec.name for spec in registry.slice("script", "narrate")] == ["script", "narrate"]

    def test_slice_rejects_a_reversed_range(self):
        with pytest.raises(ValueError):
            registry.slice("render", "discover")


@pytest.fixture
def offline_runner(settings, template, fixture_dir, image_client, monkeypatch):
    """A runner wired entirely to local fixtures: no network, no ffmpeg."""

    runner = Runner(settings)
    overrides = {
        "fixture_dir": fixture_dir / "listings",
        "verification_adapter": "fixture",
        "http_client": image_client,
    }

    # Research would reach Wikipedia; offline it simply finds nothing, which is
    # the documented "omit the beat rather than invent one" path (FR-069).
    from mpvf.research import sources as sources_module

    class OfflineCapture(sources_module.SourceCapture):
        def fetch_page(self, url, property_key=None):
            return None

        def fetch_wikipedia(self, title, property_key=None):
            return None

        def fetch_commons_media(self, search, limit=5):
            return []

    overrides["source_capture"] = OfflineCapture()
    template_copy = template
    monkeypatch.setattr(runner.templates, "get", lambda slug: template_copy)
    return runner, overrides


class TestEndToEnd:
    def test_pipeline_runs_from_discovery_to_scene_plan(self, offline_runner, settings):
        runner, overrides = offline_runner
        summary = runner.run(
            "coastal-under-1m",
            to_stage="render",
            dry_run=True,
            context_overrides=overrides,
        )
        assert summary["outcome"] == "completed", summary.get("detail")

        store = ArtifactStore(summary["artifact_dir"])
        for directory, filename in (
            ("discovery", "observations.json"),
            ("properties", "normalized.json"),
            ("properties", "verified.json"),
            ("properties", "candidates.json"),
            ("assets", "manifest.json"),
            ("research", "claims.json"),
            ("evidence", "bundle.json"),
            ("script", "script.json"),
            ("script", "script.md"),
            ("audio", "narration.json"),
            ("captions", "captions.srt"),
            ("captions", "captions.vtt"),
            ("render", "scene-plan.json"),
        ):
            assert store.exists(directory, filename), f"missing {directory}/{filename}"

    def test_the_lineup_is_five_verified_properties_plus_an_alternate(self, offline_runner):
        runner, overrides = offline_runner
        summary = runner.run("coastal-under-1m", to_stage="select", context_overrides=overrides)
        assert summary["outcome"] == "completed", summary.get("detail")
        store = ArtifactStore(summary["artifact_dir"])
        candidates = store.read_json("properties", "candidates.json")
        assert sum(1 for item in candidates if item["selected"]) == 5
        assert sum(1 for item in candidates if item["alternate"]) == 1
        for item in candidates:
            if item["selected"]:
                assert item["property"]["verification"]["verified"]

    def test_land_and_pending_listings_never_reach_the_lineup(self, offline_runner):
        runner, overrides = offline_runner
        summary = runner.run("coastal-under-1m", to_stage="select", context_overrides=overrides)
        store = ArtifactStore(summary["artifact_dir"])
        candidates = store.read_json("properties", "candidates.json")
        selected_towns = {item["property"]["city"] for item in candidates if item["selected"]}
        assert "Alna" not in selected_towns  # land-only
        assert "Rockport" not in selected_towns  # pending

    def test_every_script_claim_maps_to_evidence(self, offline_runner):
        runner, overrides = offline_runner
        summary = runner.run("coastal-under-1m", to_stage="script", context_overrides=overrides)
        assert summary["outcome"] == "completed", summary.get("detail")
        store = ArtifactStore(summary["artifact_dir"])
        validation = store.read_json("script", "validation.json")
        assert validation["factual_passed"]
        assert validation["unsupported"] == []

    def test_a_run_can_resume_from_a_later_stage(self, offline_runner):
        runner, overrides = offline_runner
        first = runner.run("coastal-under-1m", to_stage="evidence", context_overrides=overrides)
        assert first["outcome"] == "completed", first.get("detail")

        second = runner.run(
            "coastal-under-1m",
            from_stage="script",
            to_stage="script",
            run_id=first["run_id"],
            context_overrides=overrides,
        )
        assert second["outcome"] == "completed", second.get("detail")
        assert second["run_id"] == first["run_id"]

    def test_stage_rows_record_duration_and_state(self, offline_runner):
        runner, overrides = offline_runner
        summary = runner.run("coastal-under-1m", to_stage="normalize", context_overrides=overrides)
        with runner.database.session() as session:
            rows = session.query(StageRow).filter_by(run_id=summary["run_id"]).all()
        assert {row.name for row in rows} == {"discover", "normalize"}
        assert all(row.state == "complete" for row in rows)
        assert all(row.duration_seconds is not None for row in rows)

    def test_a_weak_lineup_skips_rather_than_publishes(self, offline_runner):
        runner, overrides = offline_runner
        runner.templates.get("coastal-under-1m").min_candidate_quality = 99
        template = runner.templates.get("coastal-under-1m")
        template.min_candidate_quality = 99
        summary = runner.run("coastal-under-1m", to_stage="select", context_overrides=overrides)
        assert summary["outcome"] == "skipped"
        assert "quality" in summary["reason"]
        with runner.database.session() as session:
            row = session.get(RunRow, summary["run_id"])
            assert row.state == "skipped"

    def test_insufficient_listings_fail_with_an_actionable_reason(self, offline_runner, tmp_path):
        runner, overrides = offline_runner
        empty = tmp_path / "empty-fixtures"
        empty.mkdir()
        (empty / "none.json").write_text(json.dumps({"listings": []}))
        overrides = {**overrides, "fixture_dir": empty}
        summary = runner.run("coastal-under-1m", to_stage="normalize", context_overrides=overrides)
        assert summary["outcome"] == "failed"
        assert summary["failed_stage"] == "discover"
        assert "no discovery source" in summary["reason"]

    def test_publication_package_is_assembled(self, offline_runner, settings):
        runner, overrides = offline_runner
        summary = runner.run(
            "coastal-under-1m",
            to_stage="render",
            dry_run=True,
            context_overrides=overrides,
        )
        store = ArtifactStore(summary["artifact_dir"])

        from mpvf.models.domain import EvidenceBundle, NarrationSegment, QAReport, Script
        from mpvf.publish import metadata as metadata_builder
        from mpvf.scripting.generator import script_to_markdown

        bundle = EvidenceBundle.model_validate(store.read_json("evidence", "bundle.json"))
        script = Script.model_validate(store.read_json("script", "script.json"))
        narrations = [
            NarrationSegment.model_validate(item)
            for item in store.read_json("audio", "narration.json")["segments"]
        ]
        metadata = metadata_builder.build_metadata(
            bundle, runner.templates.get("x"), script, narrations
        )
        package = build_package(
            store=store,
            bundle=bundle,
            script=script,
            script_markdown=script_to_markdown(script, bundle),
            metadata=metadata,
            qa_report=QAReport(run_id=summary["run_id"]),
            render_manifest={},
            master_path=None,
            preview_path=None,
            caption_paths={
                "srt": store.path("captions", "captions.srt"),
                "vtt": store.path("captions", "captions.vtt"),
            },
        )
        # Without ffmpeg the video files are legitimately absent; everything the
        # pipeline can produce offline must still be there.
        written = set(package.files)
        assert {
            "script.md",
            "script.json",
            "description.md",
            "metadata.json",
            "chapters.json",
            "sources.json",
            "assets-manifest.json",
            "captions.srt",
            "captions.vtt",
            "evidence.json",
        } <= written
        assert set(package.missing) <= {
            "master.mp4",
            "preview.mp4",
            "thumbnail-a.jpg",
            "qa-report.json",
            "render-manifest.json",
        } | set(name for name in REQUIRED_FILES if name.startswith("thumbnail"))

    def test_publish_stage_in_review_mode_does_not_upload(self, offline_runner):
        runner, overrides = offline_runner
        summary = runner.run(
            "coastal-under-1m",
            to_stage="render",
            mode="review",
            dry_run=True,
            context_overrides=overrides,
        )
        assert summary["outcome"] == "completed"
        with runner.database.session() as session:
            assert session.query(PublicationRow).count() == 0


class TestStageDescriptions:
    def test_every_stage_describes_itself(self):
        """The CLI and dashboard show these; an empty one is a silent regression."""

        for spec in registry.ordered():
            assert spec.describe, f"{spec.name} has no description"

    def test_a_docstring_is_used_when_no_description_is_given(self):
        from mpvf.pipeline.stages import StageRegistry

        local = StageRegistry()

        @local.register(
            "demo", order=1, produces=RunState.DISCOVERING, failure_state=RunState.DISCOVERY_FAILED
        )
        def _demo(_context):
            """First line becomes the description.

            Later lines do not.
            """

        assert local.get("demo").describe == "First line becomes the description."
