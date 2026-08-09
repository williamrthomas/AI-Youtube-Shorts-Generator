"""Automated QA (§13).

Findings are typed and actionable: each carries the check name, the category,
and enough detail for the dashboard to offer a retry. Errors block upload;
warnings surface for review.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

from mpvf.config.settings import AudioMixSettings, QualityGates, RenderSettings
from mpvf.models.domain import (
    CaptionCue,
    EvidenceBundle,
    NarrationSegment,
    QAFinding,
    QAReport,
    ScenePlan,
    Script,
)
from mpvf.render.ffmpeg import (
    FFmpegUnavailable,
    RenderFailed,
    detect_black_frames,
    ffprobe_json,
    measure_loudness,
)
from mpvf.scripting import editorial
from mpvf.scripting.fact_validator import ValidationOutcome, validate_script
from mpvf.speech.captions import transcript_similarity


def _finding(
    check: str,
    category: str,
    message: str,
    severity: str = "error",
    repairable: bool = False,
    **detail: Any,
) -> QAFinding:
    return QAFinding(
        check=check,
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        message=message,
        detail=detail,
        repairable=repairable,
    )


# --------------------------------------------------------------------------
# Factual (§13.1)
# --------------------------------------------------------------------------


def factual_checks(
    script: Script,
    bundle: EvidenceBundle,
    max_check_age_hours: float = 6.0,
    now: Any = None,
) -> tuple[list[QAFinding], ValidationOutcome]:
    from datetime import datetime

    findings: list[QAFinding] = []
    outcome = validate_script(script, bundle)
    findings.extend(outcome.findings)

    now = now or datetime.now(UTC)
    checked = bundle.checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    age_hours = (now - checked).total_seconds() / 3600
    if age_hours > max_check_age_hours:
        findings.append(
            _finding(
                "stale_verification",
                "factual",
                f"listing facts were last checked {age_hours:.1f} hours ago (limit {max_check_age_hours})",
                repairable=True,
                age_hours=round(age_hours, 2),
            )
        )

    for candidate in bundle.candidates:
        if not candidate.selected:
            continue
        verification = candidate.property.verification
        if verification is None or not verification.verified:
            findings.append(
                _finding(
                    "unverified_property",
                    "factual",
                    f"{candidate.property.town}: no clean second-source verification",
                    property_key=candidate.property_key,
                )
            )
        elif verification.blocked:
            findings.append(
                _finding(
                    "material_conflict",
                    "factual",
                    f"{candidate.property.town}: unresolved conflict "
                    f"({'; '.join(verification.blocking_conflicts[:2])})",
                    property_key=candidate.property_key,
                )
            )
        if not (verification.verification_url if verification else None):
            findings.append(
                _finding(
                    "missing_listing_link",
                    "factual",
                    f"{candidate.property.town}: no listing URL for the description",
                    severity="warning",
                    property_key=candidate.property_key,
                    repairable=True,
                )
            )

    selected_count = sum(1 for c in bundle.candidates if c.selected)
    property_segments = len(script.property_segments())
    if property_segments != selected_count:
        findings.append(
            _finding(
                "lineup_count_mismatch",
                "factual",
                f"script covers {property_segments} properties but {selected_count} were selected",
            )
        )
    return findings, outcome


def title_promise_check(title: str, bundle: EvidenceBundle) -> list[QAFinding]:
    """The title's numeric promise must match the actual lineup (§13.1)."""

    import re

    findings: list[QAFinding] = []
    selected = [c for c in bundle.candidates if c.selected]
    match = re.search(r"\b(\d+)\b", title)
    if match and int(match.group(1)) != len(selected):
        findings.append(
            _finding(
                "title_count_mismatch",
                "factual",
                f"title promises {match.group(1)} properties, lineup has {len(selected)}",
                repairable=True,
            )
        )

    ceiling = re.search(r"under \$?([\d,]+)([kKmM]?)", title, re.IGNORECASE)
    if ceiling:
        raw = float(ceiling.group(1).replace(",", ""))
        raw *= {"k": 1_000, "m": 1_000_000}.get(ceiling.group(2).lower(), 1)
        over = [c for c in selected if c.property.price and c.property.price > raw]
        if over:
            findings.append(
                _finding(
                    "title_ceiling_violated",
                    "factual",
                    f"{len(over)} listings exceed the price ceiling stated in the title",
                    prices=[c.property.price for c in over],
                )
            )
    return findings


# --------------------------------------------------------------------------
# Visual (§13.3)
# --------------------------------------------------------------------------


def visual_checks(
    plan: ScenePlan,
    video_path: Path | str | None,
    settings: RenderSettings,
    gates: QualityGates,
) -> list[QAFinding]:
    findings: list[QAFinding] = []

    from mpvf.render.scene_plan import validate_plan

    for problem in validate_plan(plan, gates.max_static_image_seconds):
        findings.append(_finding("scene_plan", "visual", problem, repairable=True))

    if plan.width != settings.width or plan.height != settings.height:
        findings.append(
            _finding(
                "wrong_dimensions",
                "visual",
                f"scene plan is {plan.width}x{plan.height}, expected {settings.width}x{settings.height}",
            )
        )

    if not video_path or not Path(video_path).exists():
        findings.append(_finding("missing_render", "visual", "no rendered video to inspect"))
        return findings

    try:
        probe = ffprobe_json(video_path, settings.ffprobe_binary)
    except (FFmpegUnavailable, RenderFailed) as exc:
        findings.append(
            _finding(
                "probe_failed", "visual", f"could not probe the render: {exc}", severity="warning"
            )
        )
        return findings

    streams = probe.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        findings.append(_finding("no_video_stream", "visual", "render contains no video stream"))
        return findings

    if (
        int(video_stream.get("width", 0)) != settings.width
        or int(video_stream.get("height", 0)) != settings.height
    ):
        findings.append(
            _finding(
                "wrong_frame_size",
                "visual",
                f"render is {video_stream.get('width')}x{video_stream.get('height')}",
            )
        )
    fps = _parse_rate(video_stream.get("r_frame_rate", "0/1"))
    if abs(fps - settings.fps) > 0.5:
        findings.append(_finding("wrong_frame_rate", "visual", f"render is {fps:.2f} fps"))

    sar = video_stream.get("sample_aspect_ratio", "1:1")
    if sar not in {"1:1", "0:1", None}:
        findings.append(
            _finding(
                "non_square_pixels", "visual", f"sample aspect ratio is {sar}", severity="warning"
            )
        )

    try:
        black = detect_black_frames(video_path, settings.ffmpeg_binary)
        # A fade at the very start or end is intentional; anything else is not.
        unexpected = [start for start in black if 1.0 < start < plan.total_duration - 2.0]
        if unexpected:
            findings.append(
                _finding(
                    "black_frames",
                    "visual",
                    f"{len(unexpected)} black stretches longer than 0.5s inside the episode",
                    starts=unexpected[:8],
                )
            )
    except (FFmpegUnavailable, RenderFailed):
        pass
    return findings


def _parse_rate(value: str) -> float:
    try:
        numerator, _, denominator = value.partition("/")
        return float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# --------------------------------------------------------------------------
# Audio (§13.4)
# --------------------------------------------------------------------------


def audio_checks(
    script: Script,
    narrations: list[NarrationSegment],
    audio_path: Path | str | None,
    mix: AudioMixSettings,
    gates: QualityGates,
    transcript: str | None = None,
    ffmpeg_binary: str = "ffmpeg",
) -> list[QAFinding]:
    findings: list[QAFinding] = []
    narrated = {narration.segment_id for narration in narrations}

    for segment in script.segments:
        if segment.segment_id not in narrated:
            findings.append(
                _finding(
                    "missing_narration",
                    "audio",
                    f"no narration audio for the {segment.type} segment",
                    segment_id=segment.segment_id,
                    repairable=True,
                )
            )

    for narration in narrations:
        for warning in narration.warnings:
            severity = "error" if "not publishable" in warning else "warning"
            findings.append(
                _finding(
                    "narration_warning",
                    "audio",
                    warning,
                    severity=severity,
                    segment_id=narration.segment_id,
                    repairable=True,
                )
            )

    if transcript is not None:
        similarity = transcript_similarity(script.spoken_text(), transcript)
        if similarity < gates.transcript_similarity:
            findings.append(
                _finding(
                    "transcript_mismatch",
                    "audio",
                    f"narration transcript matches the script at {similarity:.1%}, "
                    f"below the {gates.transcript_similarity:.0%} gate",
                    similarity=similarity,
                )
            )

    if audio_path and Path(audio_path).exists():
        try:
            measured = measure_loudness(audio_path, ffmpeg_binary)
        except (FFmpegUnavailable, RenderFailed):
            measured = {}
        integrated = measured.get("input_i")
        true_peak = measured.get("input_tp")
        if integrated is not None and abs(integrated - mix.target_lufs) > 2.0:
            findings.append(
                _finding(
                    "loudness_out_of_range",
                    "audio",
                    f"integrated loudness is {integrated:.1f} LUFS, target {mix.target_lufs}",
                    repairable=True,
                    measured=integrated,
                )
            )
        if true_peak is not None and true_peak > mix.true_peak_db + 0.3:
            findings.append(
                _finding(
                    "true_peak_exceeded",
                    "audio",
                    f"true peak is {true_peak:.2f} dBTP, ceiling {mix.true_peak_db}",
                    repairable=True,
                )
            )
    return findings


# --------------------------------------------------------------------------
# Technical (§13.5)
# --------------------------------------------------------------------------


def technical_checks(
    master_path: Path | str | None,
    cues: list[CaptionCue],
    package_files: dict[str, Path | str | None],
    settings: RenderSettings,
    expected_duration: float,
) -> list[QAFinding]:
    findings: list[QAFinding] = []

    for name, path in package_files.items():
        if path is None or not Path(path).exists():
            findings.append(
                _finding(
                    "missing_package_file", "technical", f"required file missing: {name}", file=name
                )
            )

    if cues:
        last_end = max(cue.end for cue in cues)
        if expected_duration and last_end > expected_duration + 0.5:
            findings.append(
                _finding(
                    "captions_overrun",
                    "technical",
                    f"captions end at {last_end:.1f}s, past the {expected_duration:.1f}s video",
                    repairable=True,
                )
            )
        for cue in cues:
            if cue.end <= cue.start:
                findings.append(
                    _finding(
                        "caption_bad_timing", "technical", f"cue {cue.index} ends before it starts"
                    )
                )
            if len(cue.lines) > 2:
                findings.append(
                    _finding(
                        "caption_too_many_lines",
                        "technical",
                        f"cue {cue.index} has {len(cue.lines)} lines",
                        severity="warning",
                        repairable=True,
                    )
                )
    else:
        findings.append(_finding("no_captions", "technical", "no caption cues were produced"))

    if not master_path or not Path(master_path).exists():
        findings.append(_finding("no_master", "technical", "final master file does not exist"))
        return findings

    try:
        probe = ffprobe_json(master_path, settings.ffprobe_binary)
    except (FFmpegUnavailable, RenderFailed) as exc:
        findings.append(
            _finding(
                "probe_failed",
                "technical",
                f"ffprobe could not read the master: {exc}",
                severity="warning",
            )
        )
        return findings

    fmt = probe.get("format", {})
    duration = float(fmt.get("duration", 0) or 0)
    if expected_duration and abs(duration - expected_duration) > 2.0:
        findings.append(
            _finding(
                "duration_mismatch",
                "technical",
                f"master is {duration:.1f}s, scene plan expects {expected_duration:.1f}s",
            )
        )

    streams = probe.get("streams", [])
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        findings.append(_finding("no_audio_stream", "technical", "master contains no audio stream"))
    audio: dict[str, Any] = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if audio:
        rate = int(audio.get("sample_rate", 0) or 0)
        if rate and rate != settings.audio_sample_rate:
            findings.append(
                _finding(
                    "audio_sample_rate",
                    "technical",
                    f"audio is {rate} Hz, expected {settings.audio_sample_rate}",
                    severity="warning",
                )
            )
    return findings


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def run_all(
    run_id: str,
    script: Script,
    bundle: EvidenceBundle,
    plan: ScenePlan | None,
    narrations: list[NarrationSegment],
    cues: list[CaptionCue],
    settings: RenderSettings,
    mix: AudioMixSettings,
    gates: QualityGates,
    target_words: int,
    word_range: tuple[int, int],
    master_path: Path | str | None = None,
    audio_path: Path | str | None = None,
    transcript: str | None = None,
    package_files: dict[str, Path | str | None] | None = None,
    title: str | None = None,
) -> QAReport:
    """Run every QA category and produce a single report."""

    report = QAReport(run_id=run_id)

    factual, _outcome = factual_checks(script, bundle)
    report.findings.extend(factual)
    if title:
        report.findings.extend(title_promise_check(title, bundle))

    editorial_result = editorial.evaluate(script, bundle, target_words, word_range, gates)
    report.findings.extend(editorial_result.findings)
    report.scores.update({f"editorial_{k}": v for k, v in editorial_result.subscores.items()})
    report.scores["editorial_total"] = editorial_result.total

    if plan is not None:
        report.findings.extend(visual_checks(plan, master_path, settings, gates))
    report.findings.extend(
        audio_checks(script, narrations, audio_path, mix, gates, transcript, settings.ffmpeg_binary)
    )
    report.findings.extend(
        technical_checks(
            master_path,
            cues,
            package_files or {},
            settings,
            plan.total_duration if plan else 0.0,
        )
    )

    report.scores["errors"] = float(len(report.errors))
    report.scores["warnings"] = float(len(report.warnings))
    return report.finalize()


def repair_plan(report: QAReport) -> list[dict[str, Any]]:
    """Group findings into retryable repair tasks (§13.6, FR-198)."""

    tasks: dict[str, dict[str, Any]] = {}
    for finding in report.findings:
        if finding.severity != "error":
            continue
        task = tasks.setdefault(
            finding.check,
            {
                "check": finding.check,
                "category": finding.category,
                "count": 0,
                "repairable": finding.repairable,
                "stage": _repair_stage(finding.check, finding.category),
                "examples": [],
            },
        )
        task["count"] += 1
        if len(task["examples"]) < 3:
            task["examples"].append(finding.message)
    return sorted(tasks.values(), key=lambda item: (-item["count"], item["check"]))


_STAGE_BY_CATEGORY = {
    "factual": "script",
    "editorial": "script",
    "visual": "render",
    "audio": "narrate",
    "technical": "render",
}


def _repair_stage(check: str, category: str) -> str:
    if check in {"stale_verification", "material_conflict", "unverified_property"}:
        return "verify"
    if check.startswith("caption"):
        return "captions"
    if check in {"missing_narration", "narration_warning", "transcript_mismatch"}:
        return "narrate"
    return _STAGE_BY_CATEGORY.get(category, "render")
