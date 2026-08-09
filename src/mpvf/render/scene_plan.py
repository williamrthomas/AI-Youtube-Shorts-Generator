"""Scene planning (§7.14).

The plan is the deterministic bridge between narration and pixels: it decides
what is on screen, for how long, in what order, and it enforces the visual
rhythm rules before a single frame is encoded.
"""

from __future__ import annotations

from mpvf.config.settings import RenderSettings
from mpvf.config.templates import SearchTemplate
from mpvf.models.domain import (
    AssetRecord,
    CropFocus,
    EvidenceBundle,
    NarrationSegment,
    OnScreenText,
    Scene,
    SceneAsset,
    ScenePlan,
    Script,
    ScriptSegment,
)

RHYTHM_SECONDS = 8.0  # a visual change at least this often (FR-139)
CHAPTER_CARD_SECONDS = 2.6
MIN_ASSET_SECONDS = 2.5
MAX_ASSET_SECONDS = 9.0
ASSET_REPEAT_WINDOW = 20.0


def _crop_for(asset: AssetRecord, index: int) -> CropFocus:
    """Alternate the Ken Burns direction so movement does not feel mechanical."""

    if asset.locked:
        return asset.crop_focus
    forward = index % 2 == 0
    return CropFocus(
        x=asset.crop_focus.x,
        y=asset.crop_focus.y,
        zoom_start=1.0 if forward else 1.08,
        zoom_end=1.08 if forward else 1.0,
    )


def _assets_for_segment(
    segment: ScriptSegment, bundle: EvidenceBundle, used_at: dict[str, float], clock: float
) -> list[AssetRecord]:
    """Pick the images for one segment, honouring the repeat window (§13.3)."""

    if not segment.property_key:
        return []
    pool = bundle.assets_for(segment.property_key)
    hint = (segment.asset_plan[0] if segment.asset_plan else "").strip().lower()

    # An image already on screen inside the repeat window is not reused while
    # any unused image remains (§13.3).
    fresh = [
        asset for asset in pool if clock - used_at.get(asset.asset_id, -1e9) >= ASSET_REPEAT_WINDOW
    ]
    usable = fresh or pool

    def sort_key(asset: AssetRecord) -> tuple[float, float]:
        hint_bonus = -0.5 if hint and asset.category == hint else 0.0
        return (hint_bonus, -asset.quality_score)

    return sorted(usable, key=sort_key)


def _distribute(duration: float, count: int) -> list[float]:
    """Split segment duration into per-image slices inside the allowed range."""

    if count <= 0:
        return []
    even = duration / count
    if even < MIN_ASSET_SECONDS:
        count = max(1, int(duration // MIN_ASSET_SECONDS))
        even = duration / count
    even = min(even, MAX_ASSET_SECONDS)
    slices = [even] * count
    drift = duration - sum(slices)
    if slices:
        slices[-1] = max(MIN_ASSET_SECONDS * 0.6, slices[-1] + drift)
    return [round(value, 3) for value in slices]


def build_scene_plan(
    script: Script,
    narrations: list[NarrationSegment],
    bundle: EvidenceBundle,
    template: SearchTemplate,
    settings: RenderSettings | None = None,
    gap_seconds: float = 0.35,
) -> ScenePlan:
    """Turn a validated script plus narration timings into a scene plan."""

    settings = settings or RenderSettings()
    by_id = {narration.segment_id: narration for narration in narrations}
    plan = ScenePlan(
        run_id=script.run_id,
        fps=settings.fps,
        width=settings.width,
        height=settings.height,
    )

    clock = 0.0
    used_at: dict[str, float] = {}
    map_shown: set[str] = set()

    for segment in script.segments:
        narration = by_id.get(segment.segment_id)
        duration = narration.duration_seconds if narration else segment.estimated_seconds
        if duration <= 0:
            continue

        if segment.type == "cold_open":
            scene = _title_scene(segment, clock, duration, bundle, template)
            plan.scenes.append(scene)
            _record_usage(scene, used_at)
            clock += duration + gap_seconds
            continue

        if segment.type in {"intro", "transition", "comparison", "closing", "disclaimer"}:
            plan.scenes.extend(_non_property_scenes(segment, clock, duration, bundle, template))
            clock += duration + gap_seconds
            continue

        # Property chapter: card, optional locator map, then the photo montage.
        card_duration = min(CHAPTER_CARD_SECONDS, max(2.0, duration * 0.18))
        card = _chapter_card(segment, clock, card_duration, bundle)
        plan.scenes.append(card)
        _record_usage(card, used_at)
        cursor = clock + card_duration
        remaining = duration - card_duration

        show_map = segment.property_key not in map_shown
        if show_map and remaining > 6:
            map_duration = min(3.0, remaining * 0.2)
            plan.scenes.append(
                Scene(
                    kind="map",
                    segment_id=segment.segment_id,
                    property_key=segment.property_key,
                    start=round(cursor, 3),
                    duration=round(map_duration, 3),
                    overlays=[OnScreenText(kind="lower_third", text=_town_of(segment, bundle))],
                    notes="locator map",
                )
            )
            map_shown.add(segment.property_key or "")
            cursor += map_duration
            remaining -= map_duration

        assets = _assets_for_segment(segment, bundle, used_at, cursor)
        if not assets:
            plan.scenes.append(
                _fact_card(segment, cursor, remaining, bundle, note="no usable images")
            )
            clock += duration + gap_seconds
            continue

        wanted = max(1, int(remaining // RHYTHM_SECONDS) + 1)
        chosen = assets[: min(wanted, len(assets))]
        # FR-142: if variety is thin, break the montage with a factual card.
        if len(chosen) * MAX_ASSET_SECONDS < remaining:
            fact_duration = min(3.5, remaining - len(chosen) * MIN_ASSET_SECONDS)
            plan.scenes.append(_fact_card(segment, cursor, fact_duration, bundle))
            cursor += fact_duration
            remaining -= fact_duration

        for index, (asset, slice_seconds) in enumerate(
            zip(chosen, _distribute(remaining, len(chosen)), strict=False)
        ):
            plan.scenes.append(
                Scene(
                    kind="photo",
                    segment_id=segment.segment_id,
                    property_key=segment.property_key,
                    start=round(cursor, 3),
                    duration=round(slice_seconds, 3),
                    assets=[
                        SceneAsset(
                            asset_id=asset.asset_id,
                            start=0.0,
                            duration=round(slice_seconds, 3),
                            crop_focus=_crop_for(asset, index),
                            transition="crossfade" if index else "cut",
                        )
                    ],
                    overlays=_overlays_for(segment, index),
                )
            )
            used_at[asset.asset_id] = cursor
            cursor += slice_seconds

        clock += duration + gap_seconds

    plan.total_duration = round(
        max((scene.start + scene.duration for scene in plan.scenes), default=0.0), 3
    )
    return plan


def _record_usage(scene: Scene, used_at: dict[str, float]) -> None:
    """Remember when each asset was last on screen, cards included."""

    for asset in scene.assets:
        used_at[asset.asset_id] = scene.start


def _town_of(segment: ScriptSegment, bundle: EvidenceBundle) -> str:
    record = next((p for p in bundle.properties if p.property_key == segment.property_key), None)
    return record.town if record else ""


def _title_scene(
    segment: ScriptSegment,
    start: float,
    duration: float,
    bundle: EvidenceBundle,
    template: SearchTemplate,
) -> Scene:
    hero = next((asset for asset in bundle.assets if asset.hero and asset.selected), None)
    assets = (
        [SceneAsset(asset_id=hero.asset_id, start=0.0, duration=duration, transition="cut")]
        if hero
        else []
    )
    return Scene(
        kind="title",
        segment_id=segment.segment_id,
        start=round(start, 3),
        duration=round(duration, 3),
        assets=assets,
        overlays=[OnScreenText(kind="chapter_card", text=template.name, duration=duration)],
    )


def _non_property_scenes(
    segment: ScriptSegment,
    start: float,
    duration: float,
    bundle: EvidenceBundle,
    template: SearchTemplate,
) -> list[Scene]:
    if segment.type == "disclaimer":
        return [
            Scene(
                kind="closing",
                segment_id=segment.segment_id,
                start=round(start, 3),
                duration=round(duration, 3),
                overlays=segment.on_screen_text
                or [OnScreenText(kind="disclosure", text="Verify details with the listing agent")],
            )
        ]

    kind = "comparison" if segment.type in {"comparison", "closing"} else "fact_card"
    overlays = list(segment.on_screen_text)
    if segment.type == "intro":
        overlays.append(
            OnScreenText(
                kind="disclosure",
                text=f"Prices and availability checked {bundle.checked_at.strftime('%b %d, %Y')}",
                duration=min(5.0, duration),
            )
        )
    return [
        Scene(
            kind=kind,  # type: ignore[arg-type]
            segment_id=segment.segment_id,
            start=round(start, 3),
            duration=round(duration, 3),
            overlays=overlays,
        )
    ]


def _chapter_card(
    segment: ScriptSegment, start: float, duration: float, bundle: EvidenceBundle
) -> Scene:
    """FR-135: rank, town, price, key facts and a small source line."""

    record = next((p for p in bundle.properties if p.property_key == segment.property_key), None)
    credit = bundle.credit_for(segment.property_key or "")
    price = f"${record.price:,}" if record and record.price else "Price on request"
    facts: list[str] = []
    if record:
        canonical = record.canonical
        if canonical.beds:
            facts.append(f"{int(canonical.beds)} bd")
        if canonical.baths:
            facts.append(f"{canonical.baths:g} ba")
        if canonical.square_feet:
            facts.append(f"{canonical.square_feet:,} sq ft")
        if canonical.acres:
            facts.append(f"{canonical.acres:g} ac")

    overlays = [
        OnScreenText(kind="chapter_card", text=f"#{segment.rank}", duration=duration),
        OnScreenText(kind="lower_third", text=record.town if record else "", duration=duration),
        OnScreenText(kind="callout", text=price, duration=duration),
        OnScreenText(kind="callout", text=" · ".join(facts), duration=duration),
    ]
    if credit and (credit.brokerage_name or credit.agent_name):
        overlays.append(OnScreenText(kind="credit", text=credit.spoken(), duration=duration))
    hero = next(
        (asset for asset in bundle.assets_for(segment.property_key or "") if asset.hero), None
    )
    return Scene(
        kind="chapter_card",
        segment_id=segment.segment_id,
        property_key=segment.property_key,
        start=round(start, 3),
        duration=round(duration, 3),
        assets=(
            [SceneAsset(asset_id=hero.asset_id, start=0.0, duration=duration, transition="cut")]
            if hero
            else []
        ),
        overlays=overlays,
    )


def _fact_card(
    segment: ScriptSegment,
    start: float,
    duration: float,
    bundle: EvidenceBundle,
    note: str = "",
) -> Scene:
    claims = bundle.eligible_claims_for(segment.property_key or "")
    highlights = [
        claim.text
        for claim in claims
        if claim.type in {"measurement", "listing_fact"} and claim.value is not None
    ][:3]
    return Scene(
        kind="fact_card",
        segment_id=segment.segment_id,
        property_key=segment.property_key,
        start=round(start, 3),
        duration=round(max(1.5, duration), 3),
        overlays=[
            OnScreenText(kind="callout", text=text, duration=duration) for text in highlights
        ],
        notes=note,
    )


def _overlays_for(segment: ScriptSegment, index: int) -> list[OnScreenText]:
    """Kinetic text is selective, not every spoken word (FR-123)."""

    if index != 0:
        return []
    return [item for item in segment.on_screen_text if item.kind in {"callout", "credit"}][:1]


def validate_plan(plan: ScenePlan, gates_max_static: float = 12.0) -> list[str]:
    """Structural problems that would surface as visual QA failures (§13.3)."""

    problems: list[str] = []
    last_end = 0.0
    for scene in sorted(plan.scenes, key=lambda s: s.start):
        if scene.start < last_end - 0.05:
            problems.append(f"scene {scene.scene_id} overlaps the previous scene")
        if scene.duration <= 0:
            problems.append(f"scene {scene.scene_id} has non-positive duration")
        if scene.kind == "chapter_card" and scene.duration < 2.0:
            problems.append(f"chapter card {scene.scene_id} is shorter than two seconds")
        if scene.kind == "photo" and scene.duration > gates_max_static:
            problems.append(f"scene {scene.scene_id} holds one image for {scene.duration:.1f}s")
        last_end = scene.start + scene.duration

    seen: dict[str, float] = {}
    for scene in sorted(plan.scenes, key=lambda s: s.start):
        for asset in scene.assets:
            previous = seen.get(asset.asset_id)
            if previous is not None and scene.start - previous < ASSET_REPEAT_WINDOW:
                problems.append(
                    f"asset {asset.asset_id} repeats after {scene.start - previous:.1f}s"
                )
            seen[asset.asset_id] = scene.start
    return problems
