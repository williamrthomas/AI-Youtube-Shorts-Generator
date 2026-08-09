"""Fallback-template behavior (§5.3, §14, FR-006).

The rule being protected: when today's pool cannot fill a lineup, the system
may try the configured fallback *with that template's own criteria*. It must
never relax the primary's essential rules to force an episode out, and a
non-pool failure must never trigger a fallback at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mpvf.config.templates import SearchTemplate, TemplateRegistry
from mpvf.pipeline.runner import POOL_EXHAUSTION_CODES, Runner, _pool_exhausted
from mpvf.research import sources as sources_module


def _write_template(settings, slug: str, **overrides) -> SearchTemplate:
    payload = {
        "version": 1,
        "slug": slug,
        "name": f"Template {slug}",
        "editorial_promise": f"{slug} promise",
        "result_count": 5,
        "alternates_count": 1,
        "target_words": 500,
        "word_range": [220, 900],
        "min_candidate_quality": 20,
        "discovery": {"adapter": "fixture", "search_urls": ["fixture://x"], "max_pages": 1},
        "hard_filters": {
            "status": ["active"],
            "property_types": ["single_family", "condo", "seasonal_residence"],
            "price_max": 999999,
            "state": "ME",
            "min_usable_images": 8,
        },
        "theme_rules": {
            "any_of": [
                "owned_ocean_frontage",
                "ocean_view",
                "deeded_beach_access",
                "island_location",
                "verified_coastal_village",
            ]
        },
        "publishing": {"playlist": "P", "privacy": "private"},
        **overrides,
    }
    (Path(settings.templates_dir) / f"{slug}.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )
    return SearchTemplate.model_validate(payload)


class _OfflineCapture(sources_module.SourceCapture):
    def fetch_page(self, url, property_key=None):
        return None

    def fetch_wikipedia(self, title, property_key=None):
        return None

    def fetch_commons_media(self, search, limit=5):
        return []


@pytest.fixture
def overrides(fixture_dir, image_client):
    return {
        "fixture_dir": fixture_dir / "listings",
        "verification_adapter": "fixture",
        "http_client": image_client,
        "source_capture": _OfflineCapture(),
    }


class TestPoolExhaustionClassification:
    @pytest.mark.parametrize("code", sorted(POOL_EXHAUSTION_CODES))
    def test_pool_codes_trigger(self, code):
        assert _pool_exhausted({"outcome": "skipped", "code": code})
        assert _pool_exhausted({"outcome": "failed", "code": code})

    @pytest.mark.parametrize(
        "code", ["editorial_gate", "script_facts_unsupported", "render_failed", "qa_failed"]
    )
    def test_quality_and_technical_failures_do_not_trigger(self, code):
        """A weak script from a healthy pool is not a pool problem."""

        assert not _pool_exhausted({"outcome": "skipped", "code": code})

    def test_a_completed_run_never_triggers(self):
        assert not _pool_exhausted({"outcome": "completed", "code": None})


class TestFallbackChainValidation:
    def test_reports_a_missing_target(self, settings):
        _write_template(settings, "primary", fallback_template="ghost")
        problems = TemplateRegistry(settings.templates_dir).chain_problems("primary")
        assert any("does not exist" in problem for problem in problems)

    def test_reports_a_loop(self, settings):
        _write_template(settings, "alpha", fallback_template="beta")
        _write_template(settings, "beta", fallback_template="alpha")
        problems = TemplateRegistry(settings.templates_dir).chain_problems("alpha")
        assert any("loops back" in problem for problem in problems)

    def test_a_clean_chain_has_no_problems(self, settings):
        _write_template(settings, "alpha", fallback_template="beta")
        _write_template(settings, "beta")
        assert TemplateRegistry(settings.templates_dir).chain_problems("alpha") == []

    def test_chain_is_enumerated_in_order(self, settings):
        _write_template(settings, "alpha", fallback_template="beta")
        _write_template(settings, "beta", fallback_template="gamma")
        _write_template(settings, "gamma")
        chain = TemplateRegistry(settings.templates_dir).fallback_chain("alpha")
        assert chain == ["alpha", "beta", "gamma"]

    def test_chain_enumeration_survives_a_loop(self, settings):
        _write_template(settings, "alpha", fallback_template="beta")
        _write_template(settings, "beta", fallback_template="alpha")
        assert TemplateRegistry(settings.templates_dir).fallback_chain("alpha") == [
            "alpha",
            "beta",
        ]


class TestFallbackExecution:
    def test_an_exhausted_primary_runs_its_fallback(self, settings, overrides):
        # The primary demands a quality no lineup can reach; the fallback is sane.
        _write_template(
            settings, "primary", min_candidate_quality=99, fallback_template="secondary"
        )
        _write_template(settings, "secondary", min_candidate_quality=20)

        summary = Runner(settings).run_episode(
            "primary", to_stage="select", context_overrides=overrides
        )

        assert summary["templates_tried"] == ["primary", "secondary"]
        assert summary["used_fallback"] is True
        assert summary["outcome"] == "completed"
        assert summary["template"] == "secondary"
        assert summary["attempts"][0]["code"] == "lineup_below_quality_floor"

    def test_the_fallback_uses_its_own_criteria_not_a_relaxed_primary(self, settings, overrides):
        """The primary's ceiling must not be loosened to force an episode."""

        _write_template(
            settings,
            "primary",
            fallback_template="secondary",
            hard_filters={
                "status": ["active"],
                "property_types": ["single_family"],
                "price_max": 1,  # nothing can qualify
                "state": "ME",
                "min_usable_images": 8,
            },
        )
        _write_template(settings, "secondary")

        runner = Runner(settings)
        summary = runner.run_episode("primary", to_stage="select", context_overrides=overrides)

        assert summary["outcome"] == "completed"
        assert summary["template"] == "secondary"
        # The primary run is recorded as its own failed run; it was not retried
        # with weakened filters.
        primary = summary["attempts"][0]
        assert primary["outcome"] == "failed"
        assert primary["code"] == "insufficient_candidates"
        assert primary["run_id"] != summary["run_id"]

    def test_no_fallback_configured_leaves_the_skip_standing(self, settings, overrides):
        _write_template(settings, "solo", min_candidate_quality=99)
        summary = Runner(settings).run_episode(
            "solo", to_stage="select", context_overrides=overrides
        )
        assert summary["outcome"] == "skipped"
        assert summary["used_fallback"] is False
        assert summary["templates_tried"] == ["solo"]

    def test_use_fallback_false_disables_it(self, settings, overrides):
        _write_template(
            settings, "primary", min_candidate_quality=99, fallback_template="secondary"
        )
        _write_template(settings, "secondary")
        summary = Runner(settings).run_episode(
            "primary", use_fallback=False, to_stage="select", context_overrides=overrides
        )
        assert summary["templates_tried"] == ["primary"]
        assert summary["outcome"] == "skipped"

    def test_a_cycle_terminates(self, settings, overrides):
        _write_template(settings, "alpha", min_candidate_quality=99, fallback_template="beta")
        _write_template(settings, "beta", min_candidate_quality=99, fallback_template="alpha")

        summary = Runner(settings).run_episode(
            "alpha", to_stage="select", context_overrides=overrides
        )
        assert summary["templates_tried"] == ["alpha", "beta"]
        assert summary["outcome"] == "skipped"

    @pytest.mark.parametrize(("depth", "expected"), [(1, ["a1", "a2"]), (2, ["a1", "a2", "a3"])])
    def test_depth_bounds_the_number_of_fallbacks(self, settings, overrides, depth, expected):
        """max_fallback_depth counts fallbacks attempted after the primary."""

        _write_template(settings, "a1", min_candidate_quality=99, fallback_template="a2")
        _write_template(settings, "a2", min_candidate_quality=99, fallback_template="a3")
        _write_template(settings, "a3", min_candidate_quality=99, fallback_template="a4")
        _write_template(settings, "a4", min_candidate_quality=20)

        summary = Runner(settings).run_episode(
            "a1", max_fallback_depth=depth, to_stage="select", context_overrides=overrides
        )
        assert summary["templates_tried"] == expected
        assert summary["outcome"] == "skipped"

    def test_a_missing_fallback_target_is_reported_not_crashed(self, settings, overrides):
        """The summary still describes the run that happened."""

        _write_template(settings, "primary", min_candidate_quality=99, fallback_template="ghost")
        summary = Runner(settings).run_episode(
            "primary", to_stage="select", context_overrides=overrides
        )
        # The real attempt is the base, so run_id and artifacts remain reachable.
        assert summary["outcome"] == "skipped"
        assert summary["template"] == "primary"
        assert summary["run_id"]
        assert Path(summary["artifact_dir"]).exists()
        # The chain problem is attached rather than hidden.
        assert summary["fallback_problem"]["code"] == "unknown_fallback_template"
        assert "ghost" in summary["fallback_problem"]["reason"]

    def test_a_cycle_reports_the_problem_and_keeps_the_last_real_result(self, settings, overrides):
        _write_template(settings, "alpha", min_candidate_quality=99, fallback_template="beta")
        _write_template(settings, "beta", min_candidate_quality=99, fallback_template="alpha")

        summary = Runner(settings).run_episode(
            "alpha", to_stage="select", context_overrides=overrides
        )
        assert summary["outcome"] == "skipped"
        assert summary["template"] == "beta"
        assert summary["run_id"]
        assert summary["fallback_problem"]["code"] == "fallback_cycle"

    def test_a_non_pool_failure_does_not_fall_back(self, settings, monkeypatch):
        """A script failure means the writing was bad, not the listings."""

        _write_template(settings, "primary", fallback_template="secondary")
        _write_template(settings, "secondary")
        runner = Runner(settings)

        attempted: list[str] = []

        def fake_run(slug, **_kwargs):
            attempted.append(slug)
            return {
                "run_id": "run_stub",
                "template": slug,
                "outcome": "failed",
                "code": "script_facts_unsupported",
                "reason": "prose asserted something unevidenced",
            }

        monkeypatch.setattr(runner, "run", fake_run)
        summary = runner.run_episode("primary")

        assert attempted == ["primary"]
        assert summary["used_fallback"] is False
        assert summary["code"] == "script_facts_unsupported"

    def test_each_attempt_is_its_own_run_record(self, settings, overrides):
        _write_template(
            settings, "primary", min_candidate_quality=99, fallback_template="secondary"
        )
        _write_template(settings, "secondary")

        runner = Runner(settings)
        summary = runner.run_episode("primary", to_stage="select", context_overrides=overrides)

        run_ids = {attempt["run_id"] for attempt in summary["attempts"] if "run_id" in attempt}
        assert len(run_ids) == 2
        statuses = {item["run_id"]: item for item in runner.status()}
        assert statuses[summary["attempts"][0]["run_id"]]["state"] == "skipped"
        assert run_ids <= set(statuses)

    def test_run_summary_always_carries_a_code(self, settings, overrides):
        """The fallback decision depends on it; a missing code is a silent bug."""

        _write_template(settings, "solo", min_candidate_quality=99)
        summary = Runner(settings).run("solo", to_stage="select", context_overrides=overrides)
        assert summary["code"] == "lineup_below_quality_floor"
        assert summary["stage"] == "select"
        assert Path(summary["artifact_dir"]).exists()


class TestFallbackReporting:
    def test_the_operator_can_see_what_was_tried(self, settings, overrides):
        _write_template(
            settings, "primary", min_candidate_quality=99, fallback_template="secondary"
        )
        _write_template(settings, "secondary")
        summary = Runner(settings).run_episode(
            "primary", to_stage="select", context_overrides=overrides
        )
        assert json.dumps(summary, default=str)
        assert len(summary["attempts"]) == 2
        assert summary["attempts"][0]["reason"]
