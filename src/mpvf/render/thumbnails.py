"""Thumbnail generation and legibility checks (§7.16)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mpvf.media.imaging import PIL_AVAILABLE, probe_image
from mpvf.models.domain import AssetRecord, EvidenceBundle
from mpvf.render.design import DISPLAY_SERIF, PALETTES, UI_SANS, Frame, SvgDocument, _panel, _text

THUMBNAIL_SIZE = (1280, 720)
MAX_WORDS = 5


@dataclass
class ThumbnailVariant:
    name: str
    headline: str
    price_text: str
    asset: AssetRecord | None
    svg_path: Path | None = None
    image_path: Path | None = None
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def _headline_words(text: str) -> int:
    return len([word for word in text.split() if word.strip()])


def overlay_svg(
    headline: str,
    price_text: str,
    identifier: str = "MAINE",
    palette_name: str = "coastal",
) -> SvgDocument:
    """Overlay art for one variant: hero image behind, five words maximum."""

    frame = Frame(width=THUMBNAIL_SIZE[0], height=THUMBNAIL_SIZE[1], safe_margin_pct=0.04)
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    doc.add(
        f'<linearGradient id="scrim" x1="0" y1="1" x2="0" y2="0">'
        f'<stop offset="0%" stop-color="{palette.ink}" stop-opacity="0.92"/>'
        f'<stop offset="60%" stop-color="{palette.ink}" stop-opacity="0.15"/></linearGradient>'
    )
    doc.add(f'<rect width="{frame.width}" height="{frame.height}" fill="url(#scrim)"/>')

    doc.add(_panel(frame.safe_x, frame.safe_y, 168, 54, palette.secondary, opacity=0.95, radius=4))
    doc.add(
        _text(
            identifier,
            frame.safe_x + 22,
            frame.safe_y + 39,
            30,
            palette.paper,
            UI_SANS,
            "700",
            letter_spacing="4",
        )
    )

    baseline = frame.height - frame.safe_y - 118
    doc.add(_text(headline, frame.safe_x, baseline, 82, palette.paper, DISPLAY_SERIF, "700"))
    doc.add(_text(price_text, frame.safe_x, baseline + 74, 62, palette.accent, UI_SANS, "700"))
    return doc


def build_variants(bundle: EvidenceBundle, template_name: str) -> list[ThumbnailVariant]:
    """Three distinct variants from real lineup material (FR-160 .. FR-163)."""

    selected = sorted([c for c in bundle.candidates if c.selected], key=lambda c: c.rank or 0)
    if not selected:
        return []

    top = selected[0].property
    heroes = [asset for asset in bundle.assets_for(top.property_key) if asset.selected]
    hero = next((asset for asset in heroes if asset.hero), heroes[0] if heroes else None)

    prices = [c.property.price for c in selected if c.property.price]
    ceiling = max(prices) if prices else None
    floor = min(prices) if prices else None

    variants = [
        ThumbnailVariant(
            name="a",
            headline=f"{len(selected)} Maine Homes",
            price_text=f"Under ${ceiling:,}" if ceiling else "",
            asset=hero,
        ),
        ThumbnailVariant(
            name="b",
            headline=top.town,
            price_text=f"${top.price:,}" if top.price else "",
            asset=hero,
        ),
        ThumbnailVariant(
            name="c",
            headline="Maine Waterfront",
            price_text=f"From ${floor:,}" if floor else "",
            asset=_alternate_hero(bundle, selected) or hero,
        ),
    ]

    for variant in variants:
        if _headline_words(variant.headline) > MAX_WORDS:
            variant.notes.append("headline exceeds five words")
        # FR-163: the image must come from a property actually featured.
        if variant.asset is None:
            variant.notes.append("no hero image available")
        elif variant.asset.property_key not in {c.property_key for c in selected}:
            variant.notes.append("hero image is not from a featured property")
    return variants


def _alternate_hero(bundle: EvidenceBundle, selected) -> AssetRecord | None:
    for candidate in selected[1:]:
        assets = [
            asset
            for asset in bundle.assets_for(candidate.property_key)
            if asset.category in {"water_view", "aerial", "exterior"}
        ]
        if assets:
            return max(assets, key=lambda a: a.quality_score)
    return None


def legibility_report(variant: ThumbnailVariant) -> dict[str, object]:
    """Small-size legibility and safe-zone check (FR-162, FR-164)."""

    notes: list[str] = list(variant.notes)
    # At 10% scale a 1280x720 thumbnail is 128x72; text below ~34px in the
    # full-size render becomes unreadable there.
    price_size = 62
    headline_size = 82
    scaled_price = price_size * 0.10
    if scaled_price < 4.0:
        notes.append("price text too small to read at 10% scale")
    if len(variant.headline) > 26:
        notes.append("headline too long for a mobile thumbnail")

    frame = Frame(width=THUMBNAIL_SIZE[0], height=THUMBNAIL_SIZE[1], safe_margin_pct=0.04)
    headline_width = int(headline_size * 0.55 * len(variant.headline))
    if not frame.inside_safe_area(
        frame.safe_x, frame.height - frame.safe_y - 200, headline_width, 200
    ):
        notes.append("headline extends outside the thumbnail safe area")

    resolution_ok = True
    if variant.asset and variant.asset.local_path and PIL_AVAILABLE:
        probe = probe_image(variant.asset.local_path)
        resolution_ok = probe.valid and probe.width >= THUMBNAIL_SIZE[0]
        if not resolution_ok:
            notes.append("hero image is below 1280px wide")

    return {
        "variant": variant.name,
        "headline_words": _headline_words(variant.headline),
        "notes": notes,
        "passes": not notes,
        "resolution_ok": resolution_ok,
    }


def write_variants(
    variants: list[ThumbnailVariant],
    directory: Path | str,
    palette_name: str = "coastal",
) -> list[ThumbnailVariant]:
    """Write SVG overlays; rasterization happens in the render stage."""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for variant in variants:
        document = overlay_svg(variant.headline, variant.price_text, palette_name=palette_name)
        variant.svg_path = document.write(directory / f"thumbnail-{variant.name}.svg")
    return variants


def choose_default(variants: list[ThumbnailVariant]) -> ThumbnailVariant | None:
    """Heuristic default when the editor does not pick one (FR-165)."""

    scored = [
        (variant, len(legibility_report(variant)["notes"]), -len(variant.price_text))  # type: ignore[arg-type]
        for variant in variants
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[1], item[2]))
    return scored[0][0]
