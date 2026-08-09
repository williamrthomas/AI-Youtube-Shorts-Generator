"""``mpvf doctor`` — verify every dependency before a scheduled run (§15.3)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from mpvf.config.settings import Settings
from mpvf.config.templates import TemplateRegistry

Status = str  # "ok" | "warn" | "fail"


def _check(name: str, status: Status, detail: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "required": required}


def _database(settings: Settings) -> dict[str, Any]:
    try:
        from sqlalchemy import inspect

        from mpvf.models.db import Database

        database = Database(settings.database_url())
        database.create_all()
        tables = inspect(database.engine).get_table_names()
    except Exception as exc:  # noqa: BLE001
        return _check("database", "fail", f"cannot open {settings.db_path}: {exc}")
    return _check("database", "ok", f"{len(tables)} tables at {settings.db_path}")


def _migrations(settings: Settings) -> dict[str, Any]:
    """Schema must be at head before a scheduled run touches it (§15.3)."""

    from mpvf.models import migrations

    state = migrations.state(settings)
    if state.detail:
        return _check("migrations", "warn", state.detail, required=False)
    if state.current is None:
        drift = migrations.schema_drift(settings)
        hint = (
            "run 'mpvf db upgrade' to adopt it"
            if not drift
            else f"it also differs from the models ({len(drift)} differences); back it up first"
        )
        return _check(
            "migrations",
            "fail",
            f"database predates migration control (head {state.head}); {hint}",
        )
    if state.pending:
        return _check(
            "migrations",
            "fail",
            f"database at {state.current}, head is {state.head}; run 'mpvf db upgrade'",
        )
    drift = migrations.schema_drift(settings)
    if drift:
        return _check(
            "migrations",
            "fail",
            f"{len(drift)} schema differences vs the models (e.g. {drift[0]})",
        )
    return _check("migrations", "ok", f"at head {state.current}, no drift")


def _disk(settings: Settings) -> dict[str, Any]:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(settings.data_dir)
    except OSError as exc:
        return _check("disk", "fail", f"data directory unusable: {exc}")
    free_gb = usage.free / 1024**3
    if free_gb < 10:
        return _check("disk", "fail", f"only {free_gb:.1f} GB free; an episode needs 1-3 GB")
    if free_gb < 50:
        return _check("disk", "warn", f"{free_gb:.1f} GB free; retention cleanup recommended")
    return _check("disk", "ok", f"{free_gb:.0f} GB free at {settings.data_dir}")


def _binary(name: str, binary: str, required: bool = True) -> dict[str, Any]:
    path = shutil.which(binary)
    if not path:
        return _check(name, "fail" if required else "warn", f"{binary} not found on PATH", required)

    import subprocess

    try:
        result = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10)
        version = (result.stdout or result.stderr).splitlines()[0][:70]
    except Exception:  # noqa: BLE001
        version = path
    return _check(name, "ok", version, required)


def _playwright() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _check(
            "playwright",
            "warn",
            "not installed; the fixture adapter still works. install the 'acquisition' extra",
            required=False,
        )
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    except Exception as exc:  # noqa: BLE001
        return _check("playwright", "warn", f"chromium runtime missing: {exc}", required=False)
    return _check("playwright", "ok", f"chromium at {executable}", required=False)


def _cloud_credentials(settings: Settings) -> dict[str, Any]:
    """Hosted inference needs tokens; report presence, never the values (§16)."""

    from mpvf.generation.cloud import CloudCredentials

    credentials = CloudCredentials.load(settings.secrets_dir)
    state = credentials.state()
    wanted: set[str] = set()
    for kind in (settings.provider.kind, settings.provider.secondary_kind):
        if kind == "cloudflare":
            wanted |= {"cloudflare_account_id", "cloudflare_api_token"}
        if kind == "openrouter":
            wanted.add("openrouter_api_key")
    if settings.speech.engine == "cloudflare" or settings.speech.aligner == "cloudflare":
        wanted |= {"cloudflare_account_id", "cloudflare_api_token"}

    if not wanted:
        return _check("cloud credentials", "ok", "no hosted service configured", required=False)

    missing = sorted(name for name in wanted if not state[name])
    if missing:
        return _check(
            "cloud credentials",
            "fail",
            f"missing: {', '.join(missing)} (set MPVF_* env vars or files in {settings.secrets_dir})",
        )
    return _check("cloud credentials", "ok", f"{len(wanted)} secrets present")


def _provider(settings: Settings) -> dict[str, Any]:
    """Which writer will actually run today."""

    from mpvf.generation.cloud import CloudCredentials
    from mpvf.generation.provider import DeterministicProvider, FallbackProvider, build_provider

    provider = build_provider(
        settings.provider, credentials=CloudCredentials.load(settings.secrets_dir)
    )

    def describe(item: Any) -> str:
        if isinstance(item, FallbackProvider):
            return f"{describe(item.primary)} -> {describe(item.secondary)}"
        return f"{item.name}:{item.model}"

    chain = describe(provider)
    if isinstance(provider, DeterministicProvider):
        if settings.provider.kind == "deterministic":
            return _check("model provider", "ok", "deterministic writer by configuration")
        return _check(
            "model provider",
            "fail" if settings.provider.fallback != "deterministic" else "warn",
            f"'{settings.provider.kind}' is not configured; the deterministic writer will be used",
            settings.provider.fallback != "deterministic",
        )
    return _check("model provider", "ok", chain)


def _ollama(settings: Settings) -> dict[str, Any]:
    if settings.provider.kind != "ollama" and settings.provider.secondary_kind != "ollama":
        return _check("ollama", "ok", "not used", required=False)
    from mpvf.generation.provider import OllamaProvider

    provider = OllamaProvider(settings.provider.model, settings.provider.host)
    if provider.available():
        return _check("ollama", "ok", f"has {settings.provider.model}")
    detail = (
        f"ollama at {settings.provider.host} does not have {settings.provider.model}; "
        "the next provider in the chain will be used"
    )
    return _check("ollama", "warn", detail, required=False)


def _kokoro(settings: Settings) -> dict[str, Any]:
    """The TTS engine that will actually be used, after credential resolution."""

    from mpvf.speech.tts import SilentEngine, build_engine

    engine = build_engine(settings.speech, ffmpeg_binary=settings.render.ffmpeg_binary)
    if isinstance(engine, SilentEngine):
        return _check(
            "tts",
            "fail",
            f"'{settings.speech.engine}' is unavailable; narration would be silent and QA "
            "would refuse to publish it",
        )
    detail = (
        f"{engine.name} ({settings.speech.model})" if engine.name == "workers-ai" else engine.name
    )
    return _check("tts", "ok", detail)


def _aligner(settings: Settings) -> dict[str, Any]:
    """Without a transcriber the §13.4 similarity gate cannot run."""

    if settings.speech.aligner == "none":
        return _check(
            "transcript check", "warn", "disabled; narration will not be verified", required=False
        )
    if settings.speech.aligner == "cloudflare":
        from mpvf.generation.cloud import CloudCredentials

        credentials = CloudCredentials.load(settings.secrets_dir)
        if credentials.cloudflare_account_id and credentials.cloudflare_api_token:
            return _check("transcript check", "ok", f"workers-ai {settings.speech.alignment_model}")
        return _check(
            "transcript check",
            "warn",
            "no Cloudflare credentials; the transcript gate will be skipped",
            required=False,
        )
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return _check(
            "transcript check",
            "warn",
            "faster-whisper not installed; the transcript gate will be skipped",
            required=False,
        )
    return _check("transcript check", "ok", f"faster-whisper {settings.speech.alignment_model}")


def _rasterizer() -> dict[str, Any]:
    from mpvf.render.design import rasterizer_available

    if rasterizer_available():
        return _check("svg rasterizer", "ok", "available")
    return _check(
        "svg rasterizer",
        "fail",
        "install cairosvg, rsvg-convert or inkscape to burn in overlays",
    )


def _youtube(settings: Settings) -> dict[str, Any]:
    from mpvf.publish.youtube import YouTubePublisher

    publisher = YouTubePublisher(
        settings.secrets_dir / "youtube-client-secret.json",
        settings.secrets_dir / "youtube-token.json",
    )
    state = publisher.credential_state()
    if not state["libraries_installed"]:
        return _check(
            "youtube",
            "warn",
            "google api libraries not installed ('publish' extra)",
            required=False,
        )
    if state["token_present"]:
        return _check("youtube", "ok", "authorized token present", required=False)
    if state["client_secrets_present"]:
        return _check(
            "youtube", "warn", "client secret present; run 'mpvf youtube auth'", required=False
        )
    return _check("youtube", "warn", "no credentials; uploads unavailable", required=False)


def _templates(settings: Settings) -> dict[str, Any]:
    templates = TemplateRegistry(settings.templates_dir).load_all()
    if not templates:
        return _check("templates", "fail", f"no templates in {settings.templates_dir}")
    problems = {t.slug: t.validation_report() for t in templates}
    broken = {slug: issues for slug, issues in problems.items() if issues}
    if broken:
        first = next(iter(broken.items()))
        return _check(
            "templates",
            "warn",
            f"{len(templates)} templates, {len(broken)} with warnings (e.g. {first[0]}: {first[1][0]})",
            required=False,
        )
    return _check("templates", "ok", f"{len(templates)} valid templates")


def _shared_assets(settings: Settings) -> dict[str, Any]:
    shared = settings.shared_dir
    fonts = list((shared / "fonts").glob("*")) if (shared / "fonts").exists() else []
    music = (
        [
            path
            for path in (shared / "music").glob("*")
            if path.suffix.lower() in {".mp3", ".wav", ".flac", ".m4a"}
        ]
        if (shared / "music").exists()
        else []
    )
    licensed = [path for path in music if (shared / "music" / f"{path.stem}.license.json").exists()]

    if music and not licensed:
        return _check(
            "shared assets",
            "warn",
            f"{len(music)} music files but none has a .license.json record; they will not be used",
            required=False,
        )
    return _check(
        "shared assets",
        "ok",
        f"{len(fonts)} fonts, {len(licensed)} licensed music tracks",
        required=False,
    )


def _secrets(settings: Settings) -> dict[str, Any]:
    path: Path = settings.secrets_dir
    if not path.exists():
        return _check("secrets", "warn", f"{path} does not exist yet", required=False)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return _check(
            "secrets", "warn", f"{path} is group/world accessible (mode {mode:o})", required=False
        )
    return _check("secrets", "ok", f"{path} mode {mode:o}", required=False)


def run_doctor(settings: Settings) -> dict[str, Any]:
    """Run every health check and summarize."""

    settings.ensure_directories()
    checks = [
        _database(settings),
        _migrations(settings),
        _disk(settings),
        _binary("ffmpeg", settings.render.ffmpeg_binary),
        _binary("ffprobe", settings.render.ffprobe_binary),
        _playwright(),
        _cloud_credentials(settings),
        _provider(settings),
        _ollama(settings),
        _kokoro(settings),
        _aligner(settings),
        _rasterizer(),
        _templates(settings),
        _shared_assets(settings),
        _youtube(settings),
        _secrets(settings),
    ]
    ok = all(check["status"] != "fail" for check in checks if check["required"])
    return {
        "ok": ok,
        "checks": checks,
        "failures": [c["name"] for c in checks if c["status"] == "fail"],
        "warnings": [c["name"] for c in checks if c["status"] == "warn"],
    }
