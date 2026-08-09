"""Image inspection, perceptual hashing and crop math.

Pillow is a hard dependency for rendering, but every helper here degrades to a
safe answer if an image cannot be decoded so a single bad file never takes down
a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - import guard
    from PIL import Image, ImageStat

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageStat = None  # type: ignore[assignment]
    PIL_AVAILABLE = False

MAX_DECODE_PIXELS = 80_000_000  # decompression-bomb guard (§16)


@dataclass(frozen=True)
class ImageProbe:
    valid: bool
    width: int = 0
    height: int = 0
    mode: str = ""
    detail: str = ""

    @property
    def aspect(self) -> float:
        return round(self.width / self.height, 4) if self.height else 0.0

    @property
    def is_landscape(self) -> bool:
        return self.width >= self.height


def probe_image(path: Path | str) -> ImageProbe:
    if not PIL_AVAILABLE:  # pragma: no cover - dependency guard
        return ImageProbe(valid=False, detail="pillow not installed")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            mode = image.mode
    except Exception as exc:  # noqa: BLE001 - any decode failure is a rejection
        return ImageProbe(valid=False, detail=str(exc))
    if width * height > MAX_DECODE_PIXELS:
        return ImageProbe(valid=False, detail="image exceeds decode limit")
    return ImageProbe(valid=True, width=width, height=height, mode=mode)


def perceptual_hash(path: Path | str, size: int = 8) -> str:
    """Average-hash: 64-bit fingerprint rendered as 16 hex characters."""

    if not PIL_AVAILABLE:  # pragma: no cover
        return ""
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
            pixels = list(grayscale.getdata())
    except Exception:  # noqa: BLE001
        return ""
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):0{size * size // 4}x}"


def brightness_and_contrast(path: Path | str) -> tuple[float, float]:
    """Mean brightness (0..1) and normalized standard deviation (0..1)."""

    if not PIL_AVAILABLE:  # pragma: no cover
        return 0.5, 0.5
    try:
        with Image.open(path) as image:
            stat = ImageStat.Stat(image.convert("L"))
    except Exception:  # noqa: BLE001
        return 0.5, 0.5
    return round(stat.mean[0] / 255, 3), round(min(1.0, stat.stddev[0] / 80), 3)


def color_signature(path: Path | str, buckets: int = 4) -> tuple[float, float, float]:
    """Average R/G/B in 0..1, used as a cheap scene-classification signal."""

    if not PIL_AVAILABLE:  # pragma: no cover
        return 0.5, 0.5, 0.5
    try:
        with Image.open(path) as image:
            small = image.convert("RGB").resize((buckets, buckets))
            pixels = list(small.getdata())
    except Exception:  # noqa: BLE001
        return 0.5, 0.5, 0.5
    count = len(pixels) or 1
    red = sum(p[0] for p in pixels) / count / 255
    green = sum(p[1] for p in pixels) / count / 255
    blue = sum(p[2] for p in pixels) / count / 255
    return round(red, 3), round(green, 3), round(blue, 3)


def upscale_factor(probe: ImageProbe, target_width: int, target_height: int) -> float:
    """How much an image must be enlarged to fill the frame (FR-056)."""

    if not probe.valid or not probe.width or not probe.height:
        return 99.0
    return round(max(target_width / probe.width, target_height / probe.height), 3)


def fits_frame(
    probe: ImageProbe, target_width: int, target_height: int, tolerance: float = 1.15
) -> bool:
    """True when the image fills the frame without unacceptable enlargement."""

    return upscale_factor(probe, target_width, target_height) <= tolerance


def ken_burns_path(
    probe: ImageProbe,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    zoom_start: float = 1.0,
    zoom_end: float = 1.08,
) -> dict[str, float]:
    """Crop path for a Ken Burns move that never leaves the image bounds."""

    focus_x = min(max(focus_x, 0.0), 1.0)
    focus_y = min(max(focus_y, 0.0), 1.0)
    zoom_end = max(zoom_end, zoom_start)
    max_zoom = 1.25  # anything more reads as a rapid zoom (FR-134)
    return {
        "focus_x": focus_x,
        "focus_y": focus_y,
        "zoom_start": round(min(zoom_start, max_zoom), 3),
        "zoom_end": round(min(zoom_end, max_zoom), 3),
        "aspect": probe.aspect,
    }
