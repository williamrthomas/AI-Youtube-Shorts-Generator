"""FFmpeg composition and encoding (§7.14, §7.15).

The renderer builds an explicit filter graph from the scene plan, records the
exact command and input hashes for reproducibility (FR-144), and never invents
a one-off command per episode.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mpvf.config.settings import AudioMixSettings, RenderSettings
from mpvf.models.domain import AssetRecord, Scene, ScenePlan
from mpvf.observability.logging import get_logger
from mpvf.pipeline.artifacts import hash_inputs

logger = get_logger("render.ffmpeg")


class FFmpegUnavailable(RuntimeError):
    pass


class RenderFailed(RuntimeError):
    def __init__(self, message: str, command: list[str], stderr: str) -> None:
        super().__init__(message)
        self.command = command
        self.stderr = stderr[-8000:]


@dataclass
class RenderManifest:
    """Everything needed to reproduce a render (FR-144)."""

    profile: str
    command: list[str]
    input_hash: str
    scene_hash: str
    settings: dict[str, Any]
    software: dict[str, str] = field(default_factory=dict)
    output_path: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "command": self.command,
            "command_string": " ".join(shlex.quote(part) for part in self.command),
            "input_hash": self.input_hash,
            "scene_hash": self.scene_hash,
            "settings": self.settings,
            "software": self.software,
            "output_path": self.output_path,
            "duration_seconds": self.duration_seconds,
        }


def ffmpeg_version(binary: str = "ffmpeg") -> str | None:
    try:
        result = subprocess.run([binary, "-version"], capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.splitlines()[0] if result.stdout else "unknown"


def ffprobe_json(path: Path | str, binary: str = "ffprobe") -> dict[str, Any]:
    """Stream/format probe used by technical QA (§13.5)."""

    command = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FFmpegUnavailable(f"{binary} not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RenderFailed("ffprobe failed", command, exc.stderr or "") from exc
    return json.loads(result.stdout or "{}")


def _zoompan_filter(
    scene: Scene,
    asset: AssetRecord,
    settings: RenderSettings,
) -> str:
    """Ken Burns move that stays inside the image and never stretches (FR-133)."""

    scene_asset = scene.assets[0]
    crop = scene_asset.crop_focus
    frames = max(1, int(round(scene_asset.duration * settings.fps)))
    zoom_start, zoom_end = crop.zoom_start, crop.zoom_end
    step = (zoom_end - zoom_start) / frames

    # Scale to cover the frame first so zoompan never enlarges beyond source.
    scale = (
        f"scale={settings.width * 2}:{settings.height * 2}:force_original_aspect_ratio=increase,"
        f"crop={settings.width * 2}:{settings.height * 2}"
    )
    zoom_expression = f"'min(zoom+{step:.6f},{max(zoom_start, zoom_end):.4f})'"
    x_expression = f"'iw*{crop.x:.3f}-(iw/zoom*{crop.x:.3f})'"
    y_expression = f"'ih*{crop.y:.3f}-(ih/zoom*{crop.y:.3f})'"
    zoompan = (
        f"zoompan=z={zoom_expression}:x={x_expression}:y={y_expression}:"
        f"d={frames}:s={settings.width}x{settings.height}:fps={settings.fps}"
    )
    return f"{scale},{zoompan},setsar=1"


@dataclass
class RenderInputs:
    """Resolved file inputs for one render."""

    assets: dict[str, AssetRecord]
    overlay_pngs: dict[str, Path] = field(default_factory=dict)
    narration_path: Path | None = None
    music_path: Path | None = None


def build_filter_graph(
    plan: ScenePlan,
    inputs: RenderInputs,
    settings: RenderSettings,
) -> tuple[list[str], str, list[str]]:
    """Return ``(input_args, filter_complex, output_labels)``.

    Scenes become one input each; missing assets fall back to a solid colour
    source so a single bad image cannot abort the whole render.
    """

    input_args: list[str] = []
    filters: list[str] = []
    labels: list[str] = []

    for index, scene in enumerate(sorted(plan.scenes, key=lambda s: s.start)):
        label = f"v{index}"
        asset = None
        if scene.assets:
            asset = inputs.assets.get(scene.assets[0].asset_id)

        if asset and Path(asset.local_path).exists():
            input_args += ["-loop", "1", "-t", f"{scene.duration:.3f}", "-i", asset.local_path]
            filters.append(f"[{index}:v]{_zoompan_filter(scene, asset, settings)}[{label}]")
        else:
            input_args += [
                "-f",
                "lavfi",
                "-t",
                f"{scene.duration:.3f}",
                "-i",
                f"color=c=0x0F2A43:s={settings.width}x{settings.height}:r={settings.fps}",
            ]
            filters.append(f"[{index}:v]setsar=1[{label}]")
        labels.append(label)

    if not labels:
        raise RenderFailed("scene plan produced no video inputs", [], "")

    # Crossfades between scenes, cuts where the plan asks for one.
    current = labels[0]
    offset = 0.0
    ordered = sorted(plan.scenes, key=lambda s: s.start)
    for index in range(1, len(labels)):
        scene = ordered[index]
        transition = scene.assets[0].transition if scene.assets else "crossfade"
        duration = 0.0 if transition == "cut" else 0.5
        offset += ordered[index - 1].duration - duration
        merged = f"m{index}"
        filters.append(
            f"[{current}][{labels[index]}]xfade=transition="
            f"{'fade' if transition != 'dip' else 'fadeblack'}:duration={duration or 0.02:.3f}:"
            f"offset={max(offset, 0.02):.3f}[{merged}]"
        )
        current = merged

    return input_args, ";".join(filters), [current]


def build_command(
    plan: ScenePlan,
    inputs: RenderInputs,
    output_path: Path | str,
    settings: RenderSettings,
    mix: AudioMixSettings,
    preview: bool = False,
) -> tuple[list[str], RenderManifest]:
    """Assemble the full FFmpeg invocation plus its reproducibility manifest."""

    output_path = Path(output_path)
    input_args, filter_complex, labels = build_filter_graph(plan, inputs, settings)
    video_label = labels[0]

    audio_index = len(plan.scenes)
    audio_filters: list[str] = []
    audio_label: str | None = None

    if inputs.narration_path and Path(inputs.narration_path).exists():
        input_args += ["-i", str(inputs.narration_path)]
        narration_label = f"{audio_index}:a"
        audio_index += 1
        # FR-117/FR-152: normalize narration, duck music beneath it.
        if mix.music_enabled and inputs.music_path and Path(inputs.music_path).exists():
            input_args += ["-stream_loop", "-1", "-i", str(inputs.music_path)]
            music_label = f"{audio_index}:a"
            audio_index += 1
            audio_filters.append(
                f"[{narration_label}]loudnorm=I={mix.target_lufs}:TP={mix.true_peak_db}:"
                f"LRA={mix.loudness_range}[narr]"
            )
            audio_filters.append(f"[{music_label}]volume={mix.music_gain_db}dB[musicraw]")
            audio_filters.append(
                "[musicraw][narr]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[ducked]"
            )
            audio_filters.append(
                "[narr][ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            audio_label = "aout"
        else:
            audio_filters.append(
                f"[{narration_label}]loudnorm=I={mix.target_lufs}:TP={mix.true_peak_db}:"
                f"LRA={mix.loudness_range}[aout]"
            )
            audio_label = "aout"

    if audio_filters:
        filter_complex = ";".join([filter_complex, *audio_filters])

    height = settings.preview_height if preview else settings.height
    width = int(height * settings.width / settings.height)
    scale_filter = f"[{video_label}]scale={width}:{height}[vout]"
    filter_complex = ";".join([filter_complex, scale_filter])

    command = [settings.ffmpeg_binary, "-hide_banner", "-y", *input_args]
    command += ["-filter_complex", filter_complex, "-map", "[vout]"]
    if audio_label:
        command += ["-map", f"[{audio_label}]"]
    command += [
        "-c:v",
        settings.video_codec,
        "-preset",
        "veryfast" if preview else settings.video_preset,
        "-crf",
        str(settings.video_crf + (6 if preview else 0)),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(settings.fps),
        "-movflags",
        "+faststart",
    ]
    if audio_label:
        command += [
            "-c:a",
            settings.audio_codec,
            "-b:a",
            settings.audio_bitrate,
            "-ar",
            str(settings.audio_sample_rate),
            "-ac",
            "2",
        ]
    command += ["-t", f"{plan.total_duration:.3f}", str(output_path)]

    manifest = RenderManifest(
        profile="preview" if preview else "master",
        command=command,
        input_hash=hash_inputs(
            sorted(asset.sha256 for asset in inputs.assets.values()),
            str(inputs.narration_path or ""),
            str(inputs.music_path or ""),
        ),
        scene_hash=plan.content_hash(),
        settings=settings.model_dump() | {"mix": mix.model_dump()},
        software={"ffmpeg": ffmpeg_version(settings.ffmpeg_binary) or "unavailable"},
        output_path=str(output_path),
        duration_seconds=plan.total_duration,
    )
    return command, manifest


def run_render(
    command: list[str],
    timeout_seconds: int = 5400,
) -> str:
    """Execute FFmpeg, returning stderr for the log."""

    if not command:
        raise RenderFailed("empty ffmpeg command", command, "")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=timeout_seconds
        )
    except FileNotFoundError as exc:
        raise FFmpegUnavailable(f"{command[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderFailed("ffmpeg timed out", command, str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise RenderFailed("ffmpeg exited non-zero", command, exc.stderr or "") from exc
    return result.stderr or ""


def measure_loudness(path: Path | str, binary: str = "ffmpeg") -> dict[str, float]:
    """Second-pass loudness measurement for audio QA (§13.4)."""

    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FFmpegUnavailable(f"{binary} not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RenderFailed("loudness measurement failed", command, exc.stderr or "") from exc

    stderr = result.stderr or ""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        payload = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return {
        key: float(value)
        for key, value in payload.items()
        if key.startswith("input_") and _is_number(value)
    }


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def detect_black_frames(
    path: Path | str, binary: str = "ffmpeg", min_duration: float = 0.5
) -> list[float]:
    """Return start times of black stretches longer than ``min_duration`` (§13.3)."""

    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vf",
        f"blackdetect=d={min_duration}:pix_th=0.10",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FFmpegUnavailable(f"{binary} not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RenderFailed("blackdetect failed", command, exc.stderr or "") from exc

    starts: list[float] = []
    for line in (result.stderr or "").splitlines():
        if "black_start" in line:
            for token in line.split():
                if token.startswith("black_start:"):
                    try:
                        starts.append(float(token.split(":", 1)[1]))
                    except ValueError:
                        continue
    return starts
