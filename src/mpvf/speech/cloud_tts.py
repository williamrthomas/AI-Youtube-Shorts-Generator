"""Narration and transcription through Cloudflare Workers AI (§7.12, §13.4).

These use the native ``/ai/run/{model}`` endpoint, which has no OpenAI-shaped
equivalent for audio on Workers AI. Response parsing is deliberately tolerant:
the audio may arrive as raw bytes or as base64 under one of several keys, and
a model swap should not require a code change.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from mpvf.generation.cloud import CLOUDFLARE_API_BASE, CredentialMissing, workers_ai_run_url
from mpvf.observability.logging import get_logger
from mpvf.speech.tts import TTSError, read_wav_duration

logger = get_logger("speech.cloud")

# Field names observed across Workers AI audio models, in preference order.
_AUDIO_KEYS = ("audio", "audio_base64", "output", "result")
_TEXT_KEYS = ("text", "transcript", "response")


class WorkersAISpeechEngine:
    """Text-to-speech via Workers AI, normalized to WAV.

    Segments are written as 16-bit mono PCM so captions, concatenation and
    loudness normalization work unchanged, and the duration reported is the
    measured one rather than an estimate.
    """

    name = "workers-ai"

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: str = "@cf/myshell-ai/melotts",
        *,
        api_base: str = CLOUDFLARE_API_BASE,
        language: str = "en",
        timeout: int = 120,
        sample_rate: int = 24000,
        ffmpeg_binary: str = "ffmpeg",
        client: httpx.Client | None = None,
    ) -> None:
        if not account_id or not api_token:
            raise CredentialMissing(
                "Workers AI speech needs MPVF_CLOUDFLARE_ACCOUNT_ID and MPVF_CLOUDFLARE_API_TOKEN"
            )
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.api_base = api_base
        self.language = language
        self.timeout = timeout
        self.sample_rate = sample_rate
        self.ffmpeg_binary = ffmpeg_binary
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def available(self) -> bool:
        return bool(self.account_id and self.api_token)

    def synthesize(self, text: str, destination: Path, voice: str, speed: float) -> float:
        """Synthesize one segment as a WAV at ``destination``.

        Workers AI returns MP3. The rest of the pipeline treats narration
        segments as WAV — captions, concatenation and loudness all assume it —
        so the container difference is resolved here rather than leaking into
        four other modules. Transcoding also gives us the *measured* duration
        instead of an estimate, which is what scene timing is built from.
        """

        url = workers_ai_run_url(self.account_id, self.model, self.api_base)
        payload: dict[str, Any] = {"prompt": text, "lang": self.language}
        if voice:
            payload["speaker"] = voice
        if speed and speed != 1.0:
            payload["speed"] = speed

        try:
            response = self._http().post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"workers-ai request failed: {exc}") from exc

        if response.status_code >= 400:
            raise TTSError(f"workers-ai {response.status_code}: {response.text[:300]}")

        audio = _decode_audio(response)
        if not audio:
            raise TTSError(f"workers-ai returned no audio for {self.model}")

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = destination.with_suffix(_suffix_for(audio, response))
        encoded.write_bytes(audio)

        if encoded.suffix == ".wav":
            return read_wav_duration(encoded)

        _transcode_to_wav(encoded, destination, self.sample_rate, self.ffmpeg_binary)
        encoded.unlink(missing_ok=True)
        return read_wav_duration(destination)


class WorkersAITranscriber:
    """Speech-to-text for the §13.4 transcript-similarity gate."""

    name = "workers-ai-whisper"

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: str = "@cf/openai/whisper-large-v3-turbo",
        *,
        api_base: str = CLOUDFLARE_API_BASE,
        timeout: int = 300,
        client: httpx.Client | None = None,
    ) -> None:
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.api_base = api_base
        self.timeout = timeout
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def available(self) -> bool:
        return bool(self.account_id and self.api_token)

    def transcribe(self, audio_path: Path | str) -> str:
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(path)

        url = workers_ai_run_url(self.account_id, self.model, self.api_base)
        payload = {"audio": base64.b64encode(path.read_bytes()).decode("ascii")}
        try:
            response = self._http().post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"workers-ai transcription failed: {exc}") from exc

        if response.status_code >= 400:
            raise RuntimeError(
                f"workers-ai transcription {response.status_code}: {response.text[:300]}"
            )
        return _decode_text(response)


def _envelope(response: httpx.Response) -> dict[str, Any]:
    """Unwrap Cloudflare's ``{"result": ..., "success": true}`` envelope."""

    try:
        body = response.json()
    except ValueError:
        return {}
    if isinstance(body, dict) and isinstance(body.get("result"), dict):
        return body["result"]
    return body if isinstance(body, dict) else {}


def _decode_audio(response: httpx.Response) -> bytes:
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type.startswith("audio/") or content_type == "application/octet-stream":
        return response.content

    result = _envelope(response)
    for key in _AUDIO_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            try:
                return base64.b64decode(value, validate=False)
            except (ValueError, TypeError):
                continue
        if isinstance(value, list) and value and all(isinstance(item, int) for item in value):
            return bytes(value)
    logger.warning(
        "unrecognised workers-ai audio payload",
        extra={"detail": {"keys": sorted(result)[:8]}},
    )
    return b""


def _decode_text(response: httpx.Response) -> str:
    result = _envelope(response)
    for key in _TEXT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError(f"no transcript in response: {json.dumps(result)[:300]}")


def _transcode_to_wav(source: Path, destination: Path, sample_rate: int, binary: str) -> None:
    """Convert the model's container to 16-bit mono PCM."""

    import subprocess

    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise TTSError(
            f"{binary} is required to convert Workers AI audio to WAV; install ffmpeg"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise TTSError(
            f"could not transcode Workers AI audio: {(exc.stderr or b'')[-300:]!r}"
        ) from exc


def _suffix_for(payload: bytes, response: httpx.Response) -> str:
    """Identify the audio container.

    The content-type header describes the *envelope*, which for a base64
    payload is ``application/json`` and tells us nothing about the audio. Sniff
    the magic bytes first and only fall back to the header.
    """

    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        return ".wav"
    if payload[:4] == b"OggS":
        return ".ogg"
    if payload[:3] == b"ID3" or payload[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if payload[4:8] == b"ftyp":
        return ".m4a"

    content_type = (response.headers.get("content-type") or "").lower()
    for marker, suffix in (("wav", ".wav"), ("ogg", ".ogg"), ("mp4", ".m4a"), ("mpeg", ".mp3")):
        if marker in content_type:
            return suffix
    return ".mp3"
