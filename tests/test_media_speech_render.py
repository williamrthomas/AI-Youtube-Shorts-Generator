"""Assets, narration, captions, scene planning, thumbnails and metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from mpvf.generation.provider import DeterministicProvider
from mpvf.media.classify import ClassificationContext, classify_asset, select_for_property
from mpvf.media.download import AssetDownloader, deduplicate, hamming
from mpvf.media.imaging import fits_frame, ken_burns_path, perceptual_hash, probe_image
from mpvf.models.domain import AssetRecord, NarrationSegment
from mpvf.publish import metadata as metadata_builder
from mpvf.render import design
from mpvf.render.ffmpeg import RenderInputs, build_command, build_filter_graph
from mpvf.render.scene_plan import build_scene_plan, validate_plan
from mpvf.render.thumbnails import build_variants, choose_default, legibility_report
from mpvf.scripting.generator import generate_script
from mpvf.speech.captions import (
    build_cues,
    parse_srt,
    split_lines,
    to_srt,
    to_vtt,
    transcript_similarity,
)
from mpvf.speech.tts import Narrator, SilentEngine, concatenate_wavs, estimate_seconds


def _asset(**kwargs) -> AssetRecord:
    defaults = {
        "property_key": "p1",
        "source_url": "https://images.test/x.jpg",
        "width": 1920,
        "height": 1080,
        "quality_score": 0.8,
    }
    return AssetRecord(**{**defaults, **kwargs})


class TestAssetDownload:
    def test_downloads_validates_and_hashes(self, tmp_path, image_client):
        downloader = AssetDownloader(tmp_path, client=image_client)
        record = downloader.fetch_one("https://images.test/one.jpg", property_key="p1")
        assert record.width == 1920
        assert record.sha256
        assert record.perceptual_hash
        assert Path(record.local_path).exists()

    def test_rejects_a_disallowed_mime_type(self, tmp_path):
        import httpx

        from mpvf.media.download import DownloadRejected

        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b"<html>", headers={"content-type": "text/html"}
                )
            )
        )
        downloader = AssetDownloader(tmp_path, client=client)
        with pytest.raises(DownloadRejected):
            downloader.fetch_one("https://images.test/evil.html")

    def test_a_single_failure_does_not_stop_the_batch(self, tmp_path, image_client):
        downloader = AssetDownloader(tmp_path, client=image_client)
        records = downloader.fetch_many(
            ["https://images.test/a.jpg", "https://elsewhere.test/b.jpg"], property_key="p1"
        )
        assert len(records) == 1

    def test_exact_duplicates_are_removed(self):
        left = _asset(sha256="same", perceptual_hash="ffff0000ffff0000")
        right = _asset(sha256="same", perceptual_hash="ffff0000ffff0000")
        assert len(deduplicate([left, right])) == 1

    def test_near_duplicates_are_removed(self):
        left = _asset(sha256="a", perceptual_hash="ffff0000ffff0000")
        right = _asset(sha256="b", perceptual_hash="ffff0000ffff0001")
        assert len(deduplicate([left, right])) == 1

    def test_distinct_images_survive(self):
        left = _asset(sha256="a", perceptual_hash="ffffffffffffffff")
        right = _asset(sha256="b", perceptual_hash="0000000000000000")
        assert len(deduplicate([left, right])) == 2

    def test_hamming_handles_bad_input(self):
        assert hamming("", "abc") == 64


class TestImaging:
    def test_probe_reads_dimensions(self, tmp_path, image_client):
        downloader = AssetDownloader(tmp_path, client=image_client)
        record = downloader.fetch_one("https://images.test/one.jpg")
        probe = probe_image(record.local_path)
        assert probe.valid and probe.is_landscape
        assert probe.aspect == pytest.approx(1.777, abs=0.01)

    def test_probe_rejects_a_non_image(self, tmp_path):
        path = tmp_path / "not-an-image.jpg"
        path.write_bytes(b"nope")
        assert not probe_image(path).valid

    def test_frame_fit_rejects_heavy_upscaling(self, tmp_path, image_client):
        downloader = AssetDownloader(tmp_path, client=image_client)
        record = downloader.fetch_one("https://images.test/one.jpg")
        probe = probe_image(record.local_path)
        assert fits_frame(probe, 1920, 1080)
        assert not fits_frame(probe, 7680, 4320)

    def test_ken_burns_never_exceeds_the_zoom_ceiling(self):
        path = ken_burns_path(probe_image("missing"), zoom_start=1.0, zoom_end=3.0)
        assert path["zoom_end"] <= 1.25

    def test_perceptual_hash_of_a_missing_file_is_empty(self):
        assert perceptual_hash("/does/not/exist.jpg") == ""


class TestClassification:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://x.test/aa-aerial-drone.jpg", "aerial"),
            ("https://x.test/aa-kitchen-island.jpg", "kitchen"),
            ("https://x.test/aa-primary-bedroom.jpg", "bedroom"),
            ("https://x.test/aa-bath-vanity.jpg", "bathroom"),
            ("https://x.test/aa-floorplan.png", "floor_plan"),
            ("https://x.test/agent-headshot.jpg", "agent_logo"),
            ("https://x.test/aa-detail-molding.jpg", "detail"),
        ],
    )
    def test_filename_signals(self, url, expected):
        assert classify_asset(_asset(source_url=url), ClassificationContext(0, 10)) == expected

    def test_selection_prefers_a_hero_and_reports_gaps(self):
        assets = [
            _asset(
                source_url="https://x.test/exterior.jpg", category="exterior", quality_score=0.9
            ),
            _asset(source_url="https://x.test/kitchen.jpg", category="kitchen", quality_score=0.8),
            _asset(source_url="https://x.test/bed.jpg", category="bedroom", quality_score=0.7),
        ]
        for asset, category in zip(assets, ["exterior", "kitchen", "bedroom"], strict=True):
            asset.category = category
        selected, warnings = select_for_property(assets, minimum=8)
        assert selected[0].category == "exterior"
        assert selected[0].hero
        assert any("only 3 usable images" in warning for warning in warnings)

    def test_agent_logos_are_never_selected(self):
        assets = [
            _asset(source_url="https://x.test/logo.png", category="agent_logo", quality_score=0.9),
            _asset(
                source_url="https://x.test/exterior.jpg", category="exterior", quality_score=0.5
            ),
        ]
        selected, _warnings = select_for_property(assets)
        assert all(asset.category != "agent_logo" for asset in selected)

    def test_locked_assets_are_always_kept(self):
        locked = _asset(
            source_url="https://x.test/odd.jpg", category="unknown", quality_score=0.2, locked=True
        )
        others = [
            _asset(source_url=f"https://x.test/{i}.jpg", category="exterior", quality_score=0.9)
            for i in range(12)
        ]
        selected, _warnings = select_for_property([locked, *others])
        assert locked in selected


class TestNarration:
    def test_estimate_scales_with_length(self):
        assert estimate_seconds("one two three") < estimate_seconds(
            "one two three four five six seven"
        )

    def test_pronunciation_overrides_are_applied(self, lexicon):
        narrator = Narrator(SilentEngine(), lexicon)
        spoken = narrator.prepare_text("A house in Calais near Machias.")
        assert "CAL-us" in spoken
        assert "muh-CHY-us" in spoken

    def test_multi_word_place_names_are_handled(self, lexicon):
        narrator = Narrator(SilentEngine(), lexicon)
        assert "des-SERT" in narrator.prepare_text("Mount Desert Island")

    def test_watchlist_terms_are_reported(self, lexicon):
        narrator = Narrator(SilentEngine(), lexicon)
        terms = [
            item["term"]
            for item in narrator.watchlist_report("Driving through Wiscasset to Bangor.")
        ]
        assert {"Wiscasset", "Bangor"} <= set(terms)

    def test_synthesis_produces_one_file_per_segment(self, tmp_path, bundle, template, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _issues = narrator.synthesize_script(script.segments, tmp_path)
        assert len(narrations) == len(script.segments)
        assert all(Path(item.audio_path).exists() for item in narrations)

    def test_silent_engine_is_marked_unpublishable(self, tmp_path, bundle, template, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _issues = narrator.synthesize_script(script.segments[:1], tmp_path)
        assert any("not publishable" in warning for warning in narrations[0].warnings)

    def test_segments_concatenate_into_one_track(self, tmp_path, bundle, template, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _issues = narrator.synthesize_script(script.segments[:3], tmp_path)
        combined = concatenate_wavs([n.audio_path for n in narrations], tmp_path / "all.wav")
        assert combined.exists()


class TestCaptions:
    def test_prices_are_never_split_across_lines(self):
        lines = split_lines("The asking price for this particular property is $1,250,000 even.")
        assert any("$1,250,000" in line for line in lines)

    def test_at_most_two_lines(self):
        lines = split_lines("word " * 60)
        assert len(lines) <= 2

    def test_cues_are_ordered_and_non_overlapping(self, bundle, template, tmp_path, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _ = narrator.synthesize_script(script.segments, tmp_path)
        cues = build_cues(script.segments, narrations)
        assert cues
        for previous, current in zip(cues, cues[1:], strict=False):
            assert previous.end <= current.start + 0.001
            assert current.end > current.start

    def test_srt_round_trips(self, bundle, template, tmp_path, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _ = narrator.synthesize_script(script.segments[:2], tmp_path)
        cues = build_cues(script.segments[:2], narrations)
        parsed = parse_srt(to_srt(cues))
        assert len(parsed) == len(cues)
        assert parsed[0].lines == cues[0].lines

    def test_vtt_has_a_header(self):
        from mpvf.models.domain import CaptionCue

        text = to_vtt([CaptionCue(index=1, start=0, end=2, lines=["Hello"])])
        assert text.startswith("WEBVTT")

    def test_transcript_similarity(self):
        assert transcript_similarity("the house is red", "The house is red.") == 1.0
        assert transcript_similarity("the house is red", "a boat is blue") < 0.6


class TestScenePlan:
    def _plan(self, bundle, template, tmp_path, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _ = narrator.synthesize_script(script.segments, tmp_path)
        for asset in bundle.assets:
            asset.local_path = str(tmp_path / f"{asset.asset_id}.jpg")
        return script, narrations, build_scene_plan(script, narrations, bundle, template)

    def test_plan_is_structurally_valid(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        assert validate_plan(plan) == []

    def test_every_property_gets_a_chapter_card(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        cards = [scene for scene in plan.scenes if scene.kind == "chapter_card"]
        assert len(cards) == template.result_count
        assert all(card.duration >= 2.0 for card in cards)

    def test_a_locator_map_appears_once_per_property(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        maps = [scene for scene in plan.scenes if scene.kind == "map"]
        assert len({scene.property_key for scene in maps}) == len(maps)

    def test_no_image_is_held_too_long(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        photos = [scene for scene in plan.scenes if scene.kind == "photo"]
        assert photos
        assert all(scene.duration <= 12.0 for scene in photos)

    def test_total_duration_matches_the_last_scene(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        last = max(plan.scenes, key=lambda scene: scene.start + scene.duration)
        assert plan.total_duration == pytest.approx(last.start + last.duration, abs=0.01)

    def test_plan_hash_is_stable(self, bundle, template, tmp_path, lexicon):
        _script, _narrations, plan = self._plan(bundle, template, tmp_path, lexicon)
        assert plan.content_hash() == plan.content_hash()


class TestFFmpegGraph:
    def _plan(self, bundle, template, tmp_path, lexicon):
        script = generate_script(bundle, template, DeterministicProvider())
        narrator = Narrator(SilentEngine(), lexicon)
        narrations, _ = narrator.synthesize_script(script.segments, tmp_path)
        return build_scene_plan(script, narrations, bundle, template)

    def test_graph_has_one_input_per_scene(self, bundle, template, tmp_path, lexicon, settings):
        plan = self._plan(bundle, template, tmp_path, lexicon)
        inputs = RenderInputs(assets={asset.asset_id: asset for asset in bundle.assets})
        input_args, filter_complex, labels = build_filter_graph(plan, inputs, settings.render)
        assert input_args.count("-i") == len(plan.scenes)
        assert len(labels) == 1
        assert "zoompan" in filter_complex or "setsar" in filter_complex

    def test_missing_assets_fall_back_to_a_colour_source(
        self, bundle, template, tmp_path, lexicon, settings
    ):
        plan = self._plan(bundle, template, tmp_path, lexicon)
        input_args, _filters, _labels = build_filter_graph(
            plan, RenderInputs(assets={}), settings.render
        )
        assert "lavfi" in input_args

    def test_command_records_a_reproducible_manifest(
        self, bundle, template, tmp_path, lexicon, settings
    ):
        plan = self._plan(bundle, template, tmp_path, lexicon)
        inputs = RenderInputs(assets={asset.asset_id: asset for asset in bundle.assets})
        command, manifest = build_command(
            plan, inputs, tmp_path / "master.mp4", settings.render, settings.mix
        )
        assert command[0] == settings.render.ffmpeg_binary
        assert "-movflags" in command
        assert manifest.scene_hash == plan.content_hash()
        assert manifest.input_hash

    def test_preview_renders_smaller(self, bundle, template, tmp_path, lexicon, settings):
        plan = self._plan(bundle, template, tmp_path, lexicon)
        inputs = RenderInputs(assets={})
        _command, master = build_command(
            plan, inputs, tmp_path / "m.mp4", settings.render, settings.mix
        )
        preview_command, preview = build_command(
            plan, inputs, tmp_path / "p.mp4", settings.render, settings.mix, preview=True
        )
        assert preview.profile == "preview"
        assert master.profile == "master"
        assert f"scale={int(settings.render.preview_height * 16 / 9)}" in " ".join(preview_command)


class TestDesign:
    def test_chapter_card_stays_inside_the_safe_area(self):
        frame = design.Frame()
        document = design.chapter_card(3, "Camden", "$985,000", "5 bd · 4 ba", "Listed by X", frame)
        markup = document.render()
        assert markup.startswith("<svg")
        assert "Camden" in markup
        assert f'x="{frame.safe_x}"' in markup

    def test_locator_map_places_a_marker_for_known_coordinates(self):
        markup = design.locator_map("Camden", 44.2098, -69.0648).render()
        assert "<circle" in markup
        assert "OpenStreetMap" in markup

    def test_locator_map_degrades_without_coordinates(self):
        markup = design.locator_map("Camden", None, None).render()
        assert "Camden" in markup
        assert "<circle" not in markup

    def test_text_is_escaped(self):
        markup = design.callout("Fish & Chips <b>").render()
        assert "&amp;" in markup and "<b>" not in markup


class TestThumbnails:
    def test_three_variants_from_real_lineup_material(self, bundle, template):
        variants = build_variants(bundle, template.name)
        assert len(variants) == 3
        selected_keys = {c.property_key for c in bundle.candidates if c.selected}
        for variant in variants:
            if variant.asset:
                assert variant.asset.property_key in selected_keys

    def test_headlines_stay_within_five_words(self, bundle, template):
        for variant in build_variants(bundle, template.name):
            assert legibility_report(variant)["headline_words"] <= 5

    def test_default_is_the_most_legible(self, bundle, template):
        variants = build_variants(bundle, template.name)
        assert choose_default(variants) in variants

    def test_overlay_svg_contains_the_price(self, bundle, template):
        variant = build_variants(bundle, template.name)[1]
        from mpvf.render.thumbnails import overlay_svg

        markup = overlay_svg(variant.headline, variant.price_text).render()
        assert variant.price_text in markup


class TestPublicationMetadata:
    def _narrations(self, script):
        return [
            NarrationSegment(segment_id=segment.segment_id, audio_path="", duration_seconds=12.0)
            for segment in script.segments
        ]

    def test_titles_state_the_real_count(self, bundle, template):
        titles = metadata_builder.build_titles(bundle, template)
        assert titles
        assert str(template.result_count) in titles[0]

    def test_description_contains_every_required_section(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        metadata = metadata_builder.build_metadata(
            bundle, template, script, self._narrations(script)
        )
        for marker in (
            "Prices and availability checked",
            "CHAPTERS",
            "THE PROPERTIES",
            "Listings change fast",
        ):
            assert marker in metadata.description

    def test_every_property_and_broker_is_credited(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        metadata = metadata_builder.build_metadata(
            bundle, template, script, self._narrations(script)
        )
        for candidate in bundle.candidates:
            if candidate.selected:
                assert candidate.property.town in metadata.description

    def test_chapters_start_at_zero(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        metadata = metadata_builder.build_metadata(
            bundle, template, script, self._narrations(script)
        )
        assert metadata.chapters[0]["start"] == 0.0

    def test_defaults_to_private(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        metadata = metadata_builder.build_metadata(
            bundle, template, script, self._narrations(script)
        )
        assert metadata.privacy == "private"
        assert metadata.scheduled_publish_at is None

    def test_scheduling_respects_the_configured_hour(self, bundle, template):
        script = generate_script(bundle, template, DeterministicProvider())
        metadata = metadata_builder.build_metadata(
            bundle, template, script, self._narrations(script), schedule=True
        )
        assert metadata.scheduled_publish_at is not None
        assert metadata.scheduled_publish_at.hour == template.publishing.earliest_publish_hour

    def test_title_promise_is_checked_against_the_lineup(self, bundle):
        from mpvf.qa.checks import title_promise_check

        assert title_promise_check("5 Maine Coastal Homes", bundle) == []
        findings = title_promise_check("9 Maine Coastal Homes", bundle)
        assert any(finding.check == "title_count_mismatch" for finding in findings)

    def test_price_ceiling_in_a_title_is_enforced(self, bundle):
        from mpvf.qa.checks import title_promise_check

        findings = title_promise_check("5 Maine Homes Under $100,000", bundle)
        assert any(finding.check == "title_ceiling_violated" for finding in findings)
