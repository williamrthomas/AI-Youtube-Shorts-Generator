"""Narration synthesis (§7.12).

Kokoro is the default engine; it is optional at import time so the pipeline can
be exercised without model weights. Narration is generated one segment at a
time so a single bad line can be regenerated without rebuilding the episode
(FR-110).
"""

from __future__ import annotations

import math
import re
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mpvf.config.settings import SpeechSettings
from mpvf.models.domain import NarrationSegment, ScriptSegment
from mpvf.observability.logging import get_logger
from mpvf.speech.pronunciation import PronunciationLexicon

logger = get_logger("speech.tts")

WORDS_PER_MINUTE = 150.0
MIN_SECONDS_PER_WORD = 0.18  # anything faster than this is a truncated render


class TTSError(RuntimeError):
    pass


@runtime_checkable
class SpeechEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def synthesize(self, text: str, destination: Path, voice: str, speed: float) -> float: ...


@dataclass
class SegmentAudioIssue:
    segment_id: str
    code: str
    detail: str


def estimate_seconds(text: str, words_per_minute: float = WORDS_PER_MINUTE) -> float:
    """Duration estimate used for pacing before audio exists (FR-141)."""

    words = len(text.split())
    pauses = 0.25 * len(re.findall(r"[.!?]", text)) + 0.12 * text.count(",")
    return round(words / words_per_minute * 60 + pauses, 2)


class KokoroEngine:
    """Local Kokoro TTS (Apache-licensed weights)."""

    name = "kokoro"

    def __init__(self, sample_rate: int = 24000, lang_code: str = "a") -> None:
        self.sample_rate = sample_rate
        self.lang_code = lang_code
        self._pipeline: Any = None

    def available(self) -> bool:
        try:
            import kokoro  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_pipeline(self) -> Any:  # pragma: no cover - needs model weights
        if self._pipeline is None:
            from kokoro import KPipeline

            self._pipeline = KPipeline(lang_code=self.lang_code)
        return self._pipeline

    def synthesize(self, text: str, destination: Path, voice: str, speed: float) -> float:
        if not self.available():
            raise TTSError("kokoro is not installed; install the 'speech' extra")
        pipeline = self._get_pipeline()  # pragma: no cover - needs model weights
        chunks: list[Any] = []
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice, speed=speed):
            chunks.append(audio)
        if not chunks:
            raise TTSError("kokoro returned no audio")
        import numpy as np

        samples = np.concatenate([np.asarray(chunk, dtype="float32") for chunk in chunks])
        write_wav(destination, samples.tolist(), self.sample_rate)
        return round(len(samples) / self.sample_rate, 3)


class SilentEngine:
    """Generates correctly-timed silence.

    Used by tests, by ``--dry-run``, and as an explicit degraded mode so a
    render can be inspected for timing without model weights present. Runs that
    use it are marked, and QA fails the episode rather than shipping silence.
    """

    name = "silent"

    def __init__(self, sample_rate: int = 24000) -> None:
        self.sample_rate = sample_rate

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, destination: Path, voice: str, speed: float) -> float:
        duration = max(0.5, estimate_seconds(text) / max(speed, 0.1))
        total = int(duration * self.sample_rate)
        # A near-silent 40 Hz tone keeps waveform tooling from treating the file
        # as a decode failure while remaining inaudible in review.
        samples = [0.0008 * math.sin(2 * math.pi * 40 * n / self.sample_rate) for n in range(total)]
        write_wav(destination, samples, self.sample_rate)
        return round(duration, 3)


def write_wav(path: Path | str, samples: list[float], sample_rate: int) -> Path:
    """Write float samples in -1..1 as 16-bit PCM."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for sample in samples:
        clipped = max(-1.0, min(1.0, sample))
        frames.extend(struct.pack("<h", int(clipped * 32767)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return path


def read_wav_duration(path: Path | str) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
    return round(frames / rate, 3)


def build_engine(settings: SpeechSettings) -> SpeechEngine:
    if settings.engine == "kokoro":
        engine = KokoroEngine(sample_rate=settings.sample_rate)
        if engine.available():
            return engine
        logger.warning("kokoro unavailable, falling back to silent engine")
    return SilentEngine(sample_rate=settings.sample_rate)


class Narrator:
    """Synthesizes one audio file per script segment."""

    def __init__(
        self,
        engine: SpeechEngine,
        lexicon: PronunciationLexicon,
        settings: SpeechSettings | None = None,
    ) -> None:
        self.engine = engine
        self.lexicon = lexicon
        self.settings = settings or SpeechSettings()

    def prepare_text(self, text: str) -> str:
        """Apply lexicon overrides and pause hints (FR-113, FR-115)."""

        spoken = self.lexicon.apply(text)
        # Pause after a price, an address and a broker credit.
        spoken = re.sub(r"(\$[\d,]+)", r"\1,", spoken)
        spoken = re.sub(r"(Listed by [^.]+)\.", r"\1. ", spoken)
        return re.sub(r"\s+", " ", spoken).strip()

    def synthesize_segment(
        self, segment: ScriptSegment, destination_dir: Path, retries: int = 2
    ) -> tuple[NarrationSegment, list[SegmentAudioIssue]]:
        destination = Path(destination_dir) / f"{segment.order:02d}-{segment.segment_id}.wav"
        text = self.prepare_text(segment.spoken_text)
        issues: list[SegmentAudioIssue] = []

        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                duration = self.engine.synthesize(
                    text, destination, self.settings.voice, self.settings.speed
                )
                break
            except TTSError as exc:  # pragma: no cover - engine specific
                last_error = str(exc)
                logger.warning(
                    "tts attempt failed",
                    extra={"detail": {"segment": segment.segment_id, "attempt": attempt}},
                )
        else:  # pragma: no cover - engine specific
            raise TTSError(f"segment {segment.segment_id}: {last_error}")

        words = len(text.split())
        narration = NarrationSegment(
            segment_id=segment.segment_id,
            audio_path=str(destination),
            duration_seconds=duration,
            sample_rate=self.settings.sample_rate,
            words=words,
            engine=self.engine.name,
            voice=self.settings.voice,
        )

        # FR-116: detect clipped words, excess silence and short output.
        if words and duration / words < MIN_SECONDS_PER_WORD:
            issue = SegmentAudioIssue(
                segment.segment_id,
                "audio_too_short",
                f"{duration:.2f}s for {words} words suggests truncated synthesis",
            )
            issues.append(issue)
            narration.warnings.append(issue.detail)
        expected = estimate_seconds(text)
        if expected and duration > expected * 1.9:
            issue = SegmentAudioIssue(
                segment.segment_id,
                "audio_too_long",
                f"{duration:.2f}s against an estimate of {expected:.2f}s",
            )
            issues.append(issue)
            narration.warnings.append(issue.detail)
        if self.engine.name == "silent":
            narration.warnings.append("synthesized with the silent engine; not publishable")
        return narration, issues

    def synthesize_script(
        self, segments: list[ScriptSegment], destination_dir: Path
    ) -> tuple[list[NarrationSegment], list[SegmentAudioIssue]]:
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        narrations: list[NarrationSegment] = []
        issues: list[SegmentAudioIssue] = []
        for segment in segments:
            narration, segment_issues = self.synthesize_segment(segment, destination_dir)
            narrations.append(narration)
            issues.extend(segment_issues)
            segment.estimated_seconds = narration.duration_seconds
        return narrations, issues

    def watchlist_report(self, text: str) -> list[dict[str, str]]:
        """Terms an operator should spot-check during the pilot (§13.4)."""

        return [
            {"term": entry.term, "phonetic": entry.phonetic, "note": entry.note}
            for entry in self.lexicon.watchlist_terms(text)
        ]


def concatenate_wavs(
    paths: list[Path | str], destination: Path | str, gap_seconds: float = 0.35
) -> Path:
    """Join segment WAVs with a fixed gap; returns the destination path."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not paths:
        raise ValueError("no audio segments to concatenate")

    with wave.open(str(paths[0]), "rb") as first:
        params = first.getparams()
    gap_frames = b"\x00\x00" * int(gap_seconds * params.framerate)

    with wave.open(str(destination), "wb") as output:
        output.setparams(params)
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as handle:
                output.writeframes(handle.readframes(handle.getnframes()))
            if index < len(paths) - 1:
                output.writeframes(gap_frames)
    return destination
