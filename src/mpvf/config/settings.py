"""Application settings.

Settings come from a YAML file (``config/settings.yaml`` by default) with
environment-variable overrides prefixed ``MPVF_``. Nothing in here is allowed
to hold a secret: credentials live in the secrets directory (see §16).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

PublishingMode = Literal["review", "private_upload", "scheduled", "full_auto"]


class ProviderSettings(BaseModel):
    """Generation-provider configuration (§12.2). Model names are config."""

    kind: Literal["ollama", "deterministic", "openai_compatible"] = "deterministic"
    model: str = "qwen2.5:14b-instruct"
    host: str = "http://127.0.0.1:11434"
    temperature: float = 0.4
    timeout_seconds: int = 180
    fallback: Literal["deterministic", "none"] = "deterministic"


class SpeechSettings(BaseModel):
    engine: Literal["kokoro", "null"] = "kokoro"
    voice: str = "af_heart"
    speed: float = 1.0
    sample_rate: int = 24000
    sentence_pause_ms: int = 220
    section_pause_ms: int = 550
    target_lufs: float = -16.0
    aligner: Literal["faster_whisper", "none"] = "faster_whisper"
    alignment_model: str = "base.en"


class RenderSettings(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    video_codec: str = "libx264"
    video_crf: int = 18
    video_preset: str = "medium"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 48000
    preview_height: int = 540
    safe_margin_pct: float = 0.05
    min_scene_seconds: float = 2.5
    max_scene_seconds: float = 9.0
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"


class AudioMixSettings(BaseModel):
    """§7.15 music and mix targets."""

    music_enabled: bool = True
    music_gain_db: float = -22.0
    music_duck_db: float = -12.0
    target_lufs: float = -14.0
    true_peak_db: float = -1.0
    loudness_range: float = 11.0


class AcquisitionSettings(BaseModel):
    request_delay_seconds: float = 2.5
    max_concurrency: int = 4
    max_download_bytes: int = 12 * 1024 * 1024
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MPVF/0.1"
    browser_profile_dir: str = "browser-profile"
    navigation_timeout_ms: int = 45000
    allowed_image_mime: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")


class QualityGates(BaseModel):
    """§13 pass thresholds."""

    editorial_pass_score: int = 82
    editorial_subscore_floor_pct: float = 0.60
    min_candidate_quality: int = 60
    transcript_similarity: float = 0.97
    max_static_image_seconds: float = 12.0
    asset_repeat_window_seconds: float = 20.0


class RetentionSettings(BaseModel):
    raw_pages_days: int = 90
    unused_images_days: int = 90
    preview_renders_days: int = 30
    protect_published_masters: bool = True


class Settings(BaseModel):
    """Root settings object."""

    data_dir: Path = Path("data")
    secrets_dir: Path = Path("secrets")
    templates_dir: Path = Path("config/templates")
    pronunciation_path: Path = Path("config/pronunciation.yaml")
    source_domains_path: Path = Path("config/source-domains.yaml")

    timezone: str = "America/New_York"
    publishing_mode: PublishingMode = "private_upload"
    earliest_publish_hour: int = 17

    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    mix: AudioMixSettings = Field(default_factory=AudioMixSettings)
    acquisition: AcquisitionSettings = Field(default_factory=AcquisitionSettings)
    gates: QualityGates = Field(default_factory=QualityGates)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mpvf.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def shared_dir(self) -> Path:
        return self.data_dir / "shared"

    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.resolve()}"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.runs_dir,
            self.shared_dir,
            self.shared_dir / "fonts",
            self.shared_dir / "music",
            self.shared_dir / "sfx",
            self.shared_dir / "maps",
            self.secrets_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Secrets must not be world-readable (§16).
        with contextlib.suppress(OSError):  # platform dependent
            self.secrets_dir.chmod(0o700)


DEFAULT_SETTINGS_PATHS = (
    Path("config/settings.yaml"),
    Path("config/settings.example.yaml"),
)


def _apply_env_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply ``MPVF_SECTION__KEY=value`` overrides onto a settings mapping."""

    for env_key, raw in os.environ.items():
        if not env_key.startswith("MPVF_"):
            continue
        path = env_key[len("MPVF_") :].lower().split("__")
        cursor: dict[str, Any] = payload
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):  # pragma: no cover - misconfiguration
                break
        else:
            cursor[path[-1]] = yaml.safe_load(raw)
    return payload


def load_settings(path: Path | str | None = None) -> Settings:
    """Load settings from ``path`` or the first default location that exists."""

    candidates = [Path(path)] if path else list(DEFAULT_SETTINGS_PATHS)
    payload: dict[str, Any] = {}
    for candidate in candidates:
        if candidate.exists():
            payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            break
    payload = _apply_env_overrides(payload)
    return Settings.model_validate(payload)
