"""Image classification and hero ranking (FR-053, FR-054, FR-055).

Classification runs a cheap deterministic heuristic first (filename, alt text,
position, aspect, color signature). A vision model, when configured, only
*refines* the heuristic — it never becomes a hard dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mpvf.media.imaging import (
    ImageProbe,
    brightness_and_contrast,
    color_signature,
    fits_frame,
    probe_image,
)
from mpvf.models.domain import AssetCategory, AssetRecord

_KEYWORDS: dict[AssetCategory, tuple[str, ...]] = {
    "aerial": ("aerial", "drone", "birdseye", "overhead", "sky"),
    "water_view": (
        "water",
        "ocean",
        "shore",
        "beach",
        "dock",
        "harbor",
        "lake",
        "cove",
        "view",
        "sunset",
    ),
    "kitchen": ("kitchen", "pantry", "island", "range"),
    "living_room": ("living", "great-room", "greatroom", "family", "hearth", "fireplace", "den"),
    "bedroom": ("bed", "primary", "master", "guest-room"),
    "bathroom": ("bath", "shower", "vanity", "powder"),
    "floor_plan": ("floorplan", "floor-plan", "plan", "layout", "blueprint"),
    "map": ("map", "plat", "parcel", "survey", "lotline"),
    "agent_logo": ("logo", "agent", "headshot", "brand", "watermark", "realtor"),
    "land": ("lot", "land", "acreage", "field", "woods", "trail"),
    "detail": ("detail", "molding", "mantel", "stair", "door", "window", "fixture", "built-in"),
    "exterior": ("exterior", "front", "facade", "house", "curb", "porch", "deck", "yard"),
}

# Order matters: the first matching category wins.
_PRIORITY: tuple[AssetCategory, ...] = (
    "floor_plan",
    "map",
    "agent_logo",
    "aerial",
    "water_view",
    "kitchen",
    "bathroom",
    "bedroom",
    "living_room",
    "detail",
    "land",
    "exterior",
)

_INTERIOR = {"kitchen", "living_room", "bedroom", "bathroom"}
_UNUSABLE = {"agent_logo", "floor_plan"}


@dataclass
class ClassificationContext:
    """Signals available without opening a model."""

    index: int
    total: int
    alt_text: str = ""
    caption: str = ""


def classify_asset(
    asset: AssetRecord,
    context: ClassificationContext | None = None,
) -> AssetCategory:
    """Assign a category from filename, caption and image statistics."""

    haystack = " ".join(
        part.lower()
        for part in (
            asset.source_url,
            asset.local_path,
            context.alt_text if context else "",
            context.caption if context else "",
        )
        if part
    )
    for category in _PRIORITY:
        if any(keyword in haystack for keyword in _KEYWORDS[category]):
            return category

    probe = probe_image(asset.local_path) if asset.local_path else ImageProbe(valid=False)
    if not probe.valid:
        return "unknown"

    red, green, blue = color_signature(asset.local_path)
    brightness, _contrast = brightness_and_contrast(asset.local_path)

    # A bright, blue-dominant, wide frame is almost always the setting shot.
    if blue > red + 0.06 and blue > green + 0.02 and brightness > 0.45 and probe.aspect > 1.4:
        return "water_view"
    # Listing galleries lead with the exterior.
    if context and context.index == 0:
        return "exterior"
    if green > red + 0.05 and green > blue + 0.05:
        return "land"
    return "unknown"


def quality_score(asset: AssetRecord, category: AssetCategory, target: tuple[int, int]) -> float:
    """0..1 suitability score used for ranking and hero choice (FR-054)."""

    probe = probe_image(asset.local_path) if asset.local_path else ImageProbe(valid=False)
    if not probe.valid:
        return 0.0
    if category in _UNUSABLE:
        return 0.05

    resolution = min(1.0, (probe.width * probe.height) / (1920 * 1080))
    frame_fit = 1.0 if fits_frame(probe, *target) else 0.35
    orientation = 1.0 if probe.is_landscape else 0.55
    brightness, contrast = brightness_and_contrast(asset.local_path)
    exposure = 1.0 - min(1.0, abs(brightness - 0.52) * 2.2)
    interest = min(1.0, contrast + 0.25)

    weights = (0.30, 0.20, 0.15, 0.20, 0.15)
    raw = (
        resolution * weights[0]
        + frame_fit * weights[1]
        + orientation * weights[2]
        + exposure * weights[3]
        + interest * weights[4]
    )
    return round(min(1.0, raw), 3)


_HERO_BONUS: dict[AssetCategory, float] = {
    "exterior": 0.22,
    "aerial": 0.20,
    "water_view": 0.18,
    "living_room": 0.06,
    "kitchen": 0.05,
}


def classify_and_score(
    assets: list[AssetRecord],
    target: tuple[int, int] = (1920, 1080),
    contexts: dict[str, ClassificationContext] | None = None,
) -> list[AssetRecord]:
    """Classify, score and mark hero suitability in place."""

    contexts = contexts or {}
    total = len(assets)
    for index, asset in enumerate(assets):
        context = contexts.get(asset.asset_id) or ClassificationContext(index=index, total=total)
        asset.category = classify_asset(asset, context)
        asset.quality_score = quality_score(asset, asset.category, target)
    return assets


def select_for_property(
    assets: list[AssetRecord],
    minimum: int = 8,
    maximum: int = 14,
) -> tuple[list[AssetRecord], list[str]]:
    """Choose the per-property image set (FR-055).

    Returns ``(selected, warnings)``. The set aims for a defining hero, a
    setting/view frame, two or more interiors and at least one detail.
    """

    warnings: list[str] = []
    usable = [
        asset
        for asset in assets
        if not asset.excluded and asset.category not in _UNUSABLE and asset.quality_score > 0.1
    ]
    locked = [asset for asset in usable if asset.locked]
    pool = sorted(
        (asset for asset in usable if not asset.locked),
        key=lambda a: a.quality_score + _HERO_BONUS.get(a.category, 0.0),
        reverse=True,
    )

    selected: list[AssetRecord] = list(locked)

    def take(predicate, count: int = 1) -> int:
        taken = 0
        for asset in list(pool):
            if taken >= count:
                break
            if asset in selected:
                continue
            if predicate(asset):
                selected.append(asset)
                pool.remove(asset)
                taken += 1
        return taken

    hero_taken = take(lambda a: a.category in {"exterior", "aerial"})
    if not hero_taken:
        hero_taken = take(lambda a: a.category == "water_view")
    if not hero_taken:
        warnings.append("no exterior, aerial or view image to use as a hero")

    if not take(lambda a: a.category in {"water_view", "aerial", "land"}):
        warnings.append("no setting or view image")

    interiors = take(lambda a: a.category in _INTERIOR, count=3)
    if interiors < 2:
        warnings.append(f"only {interiors} interior images")

    if not take(lambda a: a.category == "detail"):
        warnings.append("no distinctive detail image")

    while pool and len(selected) < maximum:
        selected.append(pool.pop(0))

    ordered = _order_for_narration(selected)[:maximum]
    for asset in assets:
        asset.selected = asset in ordered
        asset.hero = bool(ordered) and asset is ordered[0]

    if len(ordered) < minimum:
        warnings.append(f"only {len(ordered)} usable images, wanted at least {minimum}")
    return ordered, warnings


_NARRATION_ORDER: dict[AssetCategory, int] = {
    "exterior": 0,
    "aerial": 1,
    "water_view": 2,
    "living_room": 3,
    "kitchen": 4,
    "bedroom": 5,
    "bathroom": 6,
    "detail": 7,
    "land": 8,
    "map": 9,
    "unknown": 10,
    "floor_plan": 11,
    "agent_logo": 12,
}


def _order_for_narration(assets: list[AssetRecord]) -> list[AssetRecord]:
    """Setting, exterior, principal interior, details, closing hero (FR-140)."""

    ordered = sorted(
        assets,
        key=lambda a: (_NARRATION_ORDER.get(a.category, 10), -a.quality_score),
    )
    if len(ordered) > 3:
        # Close on the strongest view/exterior frame rather than a bathroom.
        closer = max(
            ordered[1:],
            key=lambda a: a.quality_score + _HERO_BONUS.get(a.category, 0.0),
        )
        ordered.remove(closer)
        ordered.append(closer)
    return ordered


def coverage_report(assets: list[AssetRecord]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for asset in assets:
        counts[asset.category] = counts.get(asset.category, 0) + 1
    return {
        "total": len(assets),
        "by_category": counts,
        "interiors": sum(counts.get(name, 0) for name in _INTERIOR),
        "distinct_categories": len(counts),
    }
