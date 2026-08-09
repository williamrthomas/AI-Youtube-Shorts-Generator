"""Caption generation and alignment (§7.13).

Timing is derived from narration segment durations, then optionally verified
against a local faster-whisper transcription. Captions never split a proper
name or a price across lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpvf.models.domain import CaptionCue, NarrationSegment, ScriptSegment

MAX_LINE_CHARS = 42
MAX_LINES = 2
MIN_CUE_SECONDS = 1.0
MAX_CUE_SECONDS = 6.0

_PRICE = re.compile(r"\$[\d,]+(?:\.\d+)?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def split_lines(text: str, max_chars: int = MAX_LINE_CHARS) -> list[str]:
    """Wrap to at most two readable lines without breaking a price (FR-122)."""

    # Protect prices and multi-word proper names from being split.
    protected: dict[str, str] = {}

    def protect(match: re.Match[str]) -> str:
        token = f"{len(protected)}"
        protected[token] = match.group(0)
        return token

    guarded = _PRICE.sub(protect, text)
    guarded = re.sub(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", protect, guarded)

    lines: list[str] = []
    current = ""
    for word in guarded.split():
        candidate = f"{current} {word}".strip()
        if len(_restore(candidate, protected)) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    restored = [_restore(line, protected) for line in lines]
    if len(restored) <= MAX_LINES:
        return restored
    # Rebalance: keep the first line, fold the rest into the second.
    head = restored[0]
    tail = " ".join(restored[1:])
    return [head, tail]


def _restore(text: str, protected: dict[str, str]) -> str:
    for token, value in protected.items():
        text = text.replace(token, value)
    return text


@dataclass
class _Chunk:
    text: str
    weight: float


def _chunk_segment(text: str) -> list[_Chunk]:
    """Split a segment into caption-sized chunks weighted by length."""

    chunks: list[_Chunk] = []
    for sentence in _SENTENCE.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        words = sentence.split()
        if len(words) <= 14:
            chunks.append(_Chunk(sentence, float(len(words))))
            continue
        # Long sentence: break on a comma near the midpoint, else at word 12.
        midpoint = len(words) // 2
        break_index = midpoint
        for offset in range(0, 5):
            for index in (midpoint - offset, midpoint + offset):
                if 0 < index < len(words) and words[index - 1].endswith(","):
                    break_index = index
                    break
            else:
                continue
            break
        chunks.append(_Chunk(" ".join(words[:break_index]), float(break_index)))
        chunks.append(_Chunk(" ".join(words[break_index:]), float(len(words) - break_index)))
    return chunks


def build_cues(
    segments: list[ScriptSegment],
    narrations: list[NarrationSegment],
    gap_seconds: float = 0.35,
) -> list[CaptionCue]:
    """Derive cues from segment audio durations (FR-121)."""

    by_id = {narration.segment_id: narration for narration in narrations}
    cues: list[CaptionCue] = []
    clock = 0.0
    index = 1

    for segment in segments:
        narration = by_id.get(segment.segment_id)
        if narration is None:
            continue
        chunks = _chunk_segment(segment.spoken_text)
        total_weight = sum(chunk.weight for chunk in chunks) or 1.0
        cursor = clock
        for chunk in chunks:
            share = narration.duration_seconds * (chunk.weight / total_weight)
            duration = max(MIN_CUE_SECONDS, min(MAX_CUE_SECONDS, share))
            cues.append(
                CaptionCue(
                    index=index,
                    start=round(cursor, 3),
                    end=round(cursor + duration, 3),
                    lines=split_lines(chunk.text),
                )
            )
            cursor += share
            index += 1
        clock += narration.duration_seconds + gap_seconds

    return _resolve_overlaps(cues)


def _resolve_overlaps(cues: list[CaptionCue]) -> list[CaptionCue]:
    for previous, current in zip(cues, cues[1:], strict=False):
        if current.start < previous.end:
            previous.end = round(max(previous.start + 0.4, current.start - 0.02), 3)
    return cues


def _timestamp(seconds: float, comma: bool = True) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # rounding carry
        secs, millis = secs + 1, 0
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def to_srt(cues: list[CaptionCue]) -> str:
    blocks: list[str] = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n"
            f"{_timestamp(cue.start)} --> {_timestamp(cue.end)}\n" + "\n".join(cue.lines)
        )
    return "\n\n".join(blocks) + "\n"


def to_vtt(cues: list[CaptionCue]) -> str:
    blocks = ["WEBVTT", ""]
    for cue in cues:
        blocks.append(
            f"{_timestamp(cue.start, comma=False)} --> {_timestamp(cue.end, comma=False)}\n"
            + "\n".join(cue.lines)
        )
    return "\n\n".join(blocks) + "\n"


def write_captions(
    cues: list[CaptionCue], directory: Path | str, stem: str = "captions"
) -> dict[str, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    srt_path = directory / f"{stem}.srt"
    vtt_path = directory / f"{stem}.vtt"
    srt_path.write_text(to_srt(cues), encoding="utf-8")
    vtt_path.write_text(to_vtt(cues), encoding="utf-8")
    return {"srt": srt_path, "vtt": vtt_path}


def parse_srt(text: str) -> list[CaptionCue]:
    """Parse SRT back into cues; used by technical QA (§13.5)."""

    cues: list[CaptionCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        match = re.match(
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
            lines[1],
        )
        if not match:
            continue
        parts = [int(value) for value in match.groups()]
        start = parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000
        end = parts[4] * 3600 + parts[5] * 60 + parts[6] + parts[7] / 1000
        cues.append(CaptionCue(index=index, start=start, end=end, lines=lines[2:]))
    return cues


def transcript_similarity(spoken: str, transcript: str) -> float:
    """Word-level similarity ignoring punctuation and case (§13.4, 97% gate)."""

    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9']+", text.lower())

    left, right = words(spoken), words(transcript)
    if not left:
        return 0.0
    from difflib import SequenceMatcher

    return round(SequenceMatcher(None, left, right).ratio(), 4)


class WhisperAligner:
    """Optional local alignment/verification via faster-whisper."""

    def __init__(self, model_name: str = "base.en") -> None:
        self.model_name = model_name
        self._model: Any = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def transcribe(self, audio_path: Path | str) -> str:  # pragma: no cover - needs weights
        if not self.available():
            raise RuntimeError("faster-whisper is not installed; install the 'speech' extra")
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="auto", compute_type="int8")
        segments, _info = self._model.transcribe(str(audio_path), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments)
