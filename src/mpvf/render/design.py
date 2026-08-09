"""The SVG design system (§8.3, FR-132).

Clean New England editorial design: natural palette, one display serif and one
legible sans, restrained chart-paper texture. Overlays are generated as SVG
from deterministic templates and rasterized at render time, so layout,
typography and safe margins are code, not per-episode improvisation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from html import escape
from pathlib import Path


@dataclass(frozen=True)
class Palette:
    name: str
    ink: str
    paper: str
    accent: str
    secondary: str
    muted: str
    shadow: str = "#00000066"


PALETTES: dict[str, Palette] = {
    "coastal": Palette(
        name="coastal",
        ink="#0F2A43",  # deep navy
        paper="#F6F1E4",  # warm cream
        accent="#7FA99B",  # sea glass
        secondary="#A6402F",  # weathered red
        muted="#6E7B85",  # granite
    ),
    "mountain": Palette(
        name="mountain",
        ink="#1B2A22",
        paper="#F3EFE6",
        accent="#5C7A66",
        secondary="#B4643C",
        muted="#77807A",
    ),
    "historic": Palette(
        name="historic",
        ink="#2B2118",
        paper="#F5EEDF",
        accent="#8A6A3C",
        secondary="#7B2E28",
        muted="#7A7266",
    ),
}

DISPLAY_SERIF = "Georgia, 'Iowan Old Style', 'Times New Roman', serif"
UI_SANS = "'Inter', 'Helvetica Neue', Arial, sans-serif"


@dataclass
class Frame:
    width: int = 1920
    height: int = 1080
    safe_margin_pct: float = 0.05

    @property
    def safe_x(self) -> int:
        return int(self.width * self.safe_margin_pct)

    @property
    def safe_y(self) -> int:
        return int(self.height * self.safe_margin_pct)

    @property
    def safe_width(self) -> int:
        return self.width - 2 * self.safe_x

    def inside_safe_area(self, x: int, y: int, width: int, height: int) -> bool:
        return (
            x >= self.safe_x
            and y >= self.safe_y
            and x + width <= self.width - self.safe_x
            and y + height <= self.height - self.safe_y
        )


@dataclass
class SvgDocument:
    frame: Frame
    palette: Palette
    elements: list[str] = field(default_factory=list)

    def add(self, markup: str) -> None:
        self.elements.append(markup)

    def render(self) -> str:
        body = "\n  ".join(self.elements)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.frame.width}" '
            f'height="{self.frame.height}" viewBox="0 0 {self.frame.width} {self.frame.height}">\n'
            f"  {body}\n</svg>\n"
        )

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


def _text(
    content: str,
    x: int,
    y: int,
    size: int,
    fill: str,
    family: str = UI_SANS,
    weight: str = "500",
    anchor: str = "start",
    letter_spacing: str = "0",
) -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
        f'letter-spacing="{letter_spacing}">{escape(content)}</text>'
    )


def _panel(
    x: int, y: int, width: int, height: int, fill: str, opacity: float = 0.92, radius: int = 6
) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )


def title_card(
    title: str,
    subtitle: str,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    doc.add(
        f'<rect width="{frame.width}" height="{frame.height}" fill="{palette.ink}" fill-opacity="0.55"/>'
    )
    doc.add(_chart_texture(frame, palette))
    baseline = frame.height // 2
    doc.add(_text(title, frame.safe_x, baseline, 92, palette.paper, DISPLAY_SERIF, "600"))
    doc.add(_text(subtitle, frame.safe_x, baseline + 62, 34, palette.accent, UI_SANS, "500"))
    doc.add(
        f'<rect x="{frame.safe_x}" y="{baseline + 92}" width="220" height="5" fill="{palette.secondary}"/>'
    )
    return doc


def chapter_card(
    rank: int,
    town: str,
    price: str,
    facts: str,
    credit: str,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    """FR-135: consistent rank/town/price card with a small source line."""

    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    panel_height = 300
    panel_y = frame.height - frame.safe_y - panel_height
    doc.add(_panel(frame.safe_x, panel_y, 980, panel_height, palette.paper))
    doc.add(
        f'<rect x="{frame.safe_x}" y="{panel_y}" width="12" height="{panel_height}" fill="{palette.secondary}"/>'
    )

    text_x = frame.safe_x + 48
    doc.add(_text(f"#{rank}", text_x, panel_y + 84, 64, palette.secondary, DISPLAY_SERIF, "700"))
    doc.add(_text(town, text_x + 110, panel_y + 84, 58, palette.ink, DISPLAY_SERIF, "600"))
    doc.add(_text(price, text_x, panel_y + 156, 46, palette.ink, UI_SANS, "600"))
    doc.add(_text(facts, text_x, panel_y + 206, 30, palette.muted, UI_SANS, "500"))
    if credit:
        doc.add(_text(credit, text_x, panel_y + 258, 22, palette.muted, UI_SANS, "400"))
    return doc


def lower_third(
    text: str,
    detail: str = "",
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    height = 96 if detail else 68
    y = frame.height - frame.safe_y - height
    width = min(frame.safe_width, 240 + 22 * max(len(text), len(detail)))
    doc.add(_panel(frame.safe_x, y, width, height, palette.ink, opacity=0.86))
    doc.add(_text(text, frame.safe_x + 28, y + 46, 34, palette.paper, UI_SANS, "600"))
    if detail:
        doc.add(_text(detail, frame.safe_x + 28, y + 80, 24, palette.accent, UI_SANS, "400"))
    return doc


def callout(
    text: str,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    """An editorial callout, not a social-media sticker (§8.3)."""

    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    width = min(frame.safe_width, 120 + 20 * len(text))
    x = frame.width - frame.safe_x - width
    y = frame.safe_y
    doc.add(_panel(x, y, width, 78, palette.paper, opacity=0.94))
    doc.add(f'<rect x="{x}" y="{y}" width="6" height="78" fill="{palette.accent}"/>')
    doc.add(_text(text, x + 28, y + 50, 32, palette.ink, UI_SANS, "600"))
    return doc


def disclosure_strip(
    text: str,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    """The required "checked on" disclosure (§8.4)."""

    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)
    y = frame.safe_y
    doc.add(_panel(frame.safe_x, y, 640, 52, palette.ink, opacity=0.72))
    doc.add(_text(text, frame.safe_x + 22, y + 35, 24, palette.paper, UI_SANS, "500"))
    return doc


def credit_line(
    text: str,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    """Small, unobtrusive brokerage credit (§8.4)."""

    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)
    y = frame.height - frame.safe_y - 34
    doc.add(_text(text, frame.safe_x, y + 22, 22, palette.paper, UI_SANS, "400"))
    return doc


def locator_map(
    town: str,
    latitude: float | None,
    longitude: float | None,
    frame: Frame | None = None,
    palette_name: str = "coastal",
) -> SvgDocument:
    """A schematic Maine locator (FR-136, FR-137).

    This is the fallback drawing used when no vector-tile source is configured;
    the map layer is replaceable by design.
    """

    frame = frame or Frame()
    palette = PALETTES.get(palette_name, PALETTES["coastal"])
    doc = SvgDocument(frame=frame, palette=palette)

    doc.add(f'<rect width="{frame.width}" height="{frame.height}" fill="{palette.paper}"/>')
    doc.add(_chart_texture(frame, palette, opacity=0.18))

    # Maine bounding box, roughly.
    lat_min, lat_max = 42.9, 47.5
    lon_min, lon_max = -71.1, -66.9
    box_w, box_h = 620, 860
    box_x = (frame.width - box_w) // 2
    box_y = (frame.height - box_h) // 2

    doc.add(
        f'<rect x="{box_x}" y="{box_y}" width="{box_w}" height="{box_h}" rx="14" '
        f'fill="{palette.accent}" fill-opacity="0.20" stroke="{palette.ink}" stroke-width="3"/>'
    )
    doc.add(
        _text(
            "MAINE", box_x + 24, box_y + 48, 30, palette.muted, UI_SANS, "600", letter_spacing="6"
        )
    )

    if latitude is not None and longitude is not None:
        x = box_x + int((longitude - lon_min) / (lon_max - lon_min) * box_w)
        y = box_y + int((lat_max - latitude) / (lat_max - lat_min) * box_h)
        x = max(box_x + 8, min(box_x + box_w - 8, x))
        y = max(box_y + 8, min(box_y + box_h - 8, y))
        doc.add(
            f'<circle cx="{x}" cy="{y}" r="26" fill="{palette.secondary}" fill-opacity="0.25"/>'
        )
        doc.add(f'<circle cx="{x}" cy="{y}" r="11" fill="{palette.secondary}"/>')
        doc.add(_text(town, x + 26, y + 8, 32, palette.ink, DISPLAY_SERIF, "600"))
    else:
        doc.add(
            _text(
                town,
                frame.width // 2,
                box_y + box_h // 2,
                44,
                palette.ink,
                DISPLAY_SERIF,
                "600",
                anchor="middle",
            )
        )
    doc.add(
        _text(
            "Map: schematic locator · OpenStreetMap contributors",
            frame.safe_x,
            frame.height - frame.safe_y,
            20,
            palette.muted,
        )
    )
    return doc


def _chart_texture(frame: Frame, palette: Palette, opacity: float = 0.10) -> str:
    """Nautical-chart depth lines: restrained texture, never a pattern pack."""

    lines: list[str] = []
    for index in range(1, 7):
        offset = index * 130
        lines.append(
            f'<path d="M -50 {offset} Q {frame.width // 3} {offset - 60}, '
            f'{frame.width // 2} {offset} T {frame.width + 50} {offset - 20}" '
            f'fill="none" stroke="{palette.accent}" stroke-width="2" stroke-opacity="{opacity}"/>'
        )
    return "\n  ".join(lines)


# --------------------------------------------------------------------------
# Rasterization
# --------------------------------------------------------------------------


class RasterizerUnavailable(RuntimeError):
    pass


def rasterize(svg_path: Path | str, png_path: Path | str, width: int, height: int) -> Path:
    """Render SVG to PNG with whatever renderer is present.

    Tries CairoSVG, then the ``rsvg-convert`` and ``inkscape`` CLIs. Any of them
    satisfies §9.1; none of them is a paid service.
    """

    svg_path, png_path = Path(svg_path), Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import cairosvg

        cairosvg.svg2png(
            url=str(svg_path),
            write_to=str(png_path),
            output_width=width,
            output_height=height,
        )
        return png_path
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - renderer specific
        raise RasterizerUnavailable(f"cairosvg failed: {exc}") from exc

    for command in (
        ["rsvg-convert", "-w", str(width), "-h", str(height), "-o", str(png_path), str(svg_path)],
        [
            "inkscape",
            str(svg_path),
            "--export-type=png",
            f"--export-filename={png_path}",
            f"--export-width={width}",
            f"--export-height={height}",
        ],
    ):
        try:
            subprocess.run(command, check=True, capture_output=True)
            return png_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    raise RasterizerUnavailable(
        "no SVG rasterizer available: install cairosvg, rsvg-convert or inkscape"
    )


def rasterizer_available() -> bool:
    try:
        import cairosvg  # noqa: F401

        return True
    except ImportError:
        pass
    for binary in ("rsvg-convert", "inkscape"):
        try:
            subprocess.run([binary, "--version"], check=True, capture_output=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False
