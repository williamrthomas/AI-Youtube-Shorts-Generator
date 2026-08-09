"""The ``mpvf`` command-line interface (§7.19)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from mpvf.cli.doctor import run_doctor
from mpvf.config.settings import Settings, load_settings
from mpvf.config.templates import TemplateRegistry
from mpvf.models.db import Database
from mpvf.observability.logging import configure_logging
from mpvf.pipeline.runner import Runner, registry

app = typer.Typer(
    name="mpvf",
    help="Maine Property Video Factory — evidence-first property video production.",
    no_args_is_help=True,
    add_completion=False,
)
templates_app = typer.Typer(help="Manage search templates.")
render_app = typer.Typer(help="Render previews and masters.")
script_app = typer.Typer(help="Script generation.")
sources_app = typer.Typer(help="Source adapters.")
youtube_app = typer.Typer(help="YouTube credentials and publishing.")
run_app = typer.Typer(help="Run control.")

app.add_typer(templates_app, name="templates")
app.add_typer(render_app, name="render")
app.add_typer(script_app, name="script")
app.add_typer(sources_app, name="sources")
app.add_typer(youtube_app, name="youtube")
app.add_typer(run_app, name="run-control")


def _settings(config: Path | None = None) -> Settings:
    return load_settings(config)


def _runner(config: Path | None = None) -> Runner:
    return Runner(_settings(config))


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


# --------------------------------------------------------------------------
# init / doctor
# --------------------------------------------------------------------------


@app.command()
def init(
    config: Path | None = typer.Option(None, help="Path to settings.yaml"),
    force: bool = typer.Option(False, help="Overwrite existing scaffolding"),
) -> None:
    """Create the data directories, database and starter configuration."""

    settings = _settings(config)
    settings.ensure_directories()
    database = Database(settings.database_url())
    database.create_all()

    from mpvf.cli.scaffold import write_starter_config

    created = write_starter_config(settings, force=force)
    typer.echo(f"Data directory: {settings.data_dir.resolve()}")
    typer.echo(f"Database:       {settings.db_path.resolve()}")
    for path in created:
        typer.echo(f"Created:        {path}")
    typer.echo("\nNext: review config/templates, then run 'mpvf doctor'.")


@app.command()
def doctor(
    config: Path | None = typer.Option(None, help="Path to settings.yaml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output"),
) -> None:
    """Check every dependency the pipeline needs (§15.3)."""

    report = run_doctor(_settings(config))
    if json_output:
        _echo_json(report)
        raise typer.Exit(code=0 if report["ok"] else 1)

    for check in report["checks"]:
        marker = {"ok": "✓", "warn": "!", "fail": "✗"}[check["status"]]
        typer.echo(f"{marker} {check['name']:<28} {check['detail']}")
    typer.echo("")
    typer.echo("All required checks passed." if report["ok"] else "Some required checks failed.")
    raise typer.Exit(code=0 if report["ok"] else 1)


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


@templates_app.command("list")
def templates_list(config: Path | None = typer.Option(None)) -> None:
    """List configured search templates."""

    settings = _settings(config)
    for template in TemplateRegistry(settings.templates_dir).load_all():
        state = "enabled" if template.enabled else "disabled"
        typer.echo(
            f"{template.slug:<24} v{template.version}  {state:<9} "
            f"{template.result_count} properties  {template.name}"
        )


@templates_app.command("validate")
def templates_validate(
    slug: str = typer.Argument(..., help="Template slug, or 'all'"),
    config: Path | None = typer.Option(None),
) -> None:
    """Report configuration problems without starting a run (FR-004)."""

    settings = _settings(config)
    registry_ = TemplateRegistry(settings.templates_dir)
    templates = registry_.load_all() if slug == "all" else [registry_.get(slug)]

    failures = 0
    for template in templates:
        problems = template.validation_report()
        if problems:
            failures += 1
            typer.echo(f"{template.slug}:")
            for problem in problems:
                typer.echo(f"  - {problem}")
        else:
            typer.echo(f"{template.slug}: ok")
    raise typer.Exit(code=1 if failures else 0)


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------


@app.command()
def discover(
    template: str = typer.Argument(..., help="Template slug"),
    config: Path | None = typer.Option(None),
    fixture_dir: Path | None = typer.Option(None, help="Run against saved fixtures"),
) -> None:
    """Run discovery and normalization only."""

    runner = _runner(config)
    summary = runner.run(
        template,
        to_stage="normalize",
        context_overrides={"fixture_dir": fixture_dir} if fixture_dir else None,
    )
    _echo_json(summary)
    raise typer.Exit(code=0 if summary.get("outcome") == "completed" else 1)


@app.command("run")
def run_command(
    template: str = typer.Argument(..., help="Template slug"),
    from_stage: str | None = typer.Option(None, "--from-stage", help="Start at this stage"),
    to_stage: str | None = typer.Option(None, "--to-stage", help="Stop after this stage"),
    run_id: str | None = typer.Option(None, "--run-id", help="Reuse an existing run"),
    mode: str | None = typer.Option(None, help="review|private_upload|scheduled|full_auto"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan the render without encoding"),
    fixture_dir: Path | None = typer.Option(None, help="Run against saved fixtures"),
    config: Path | None = typer.Option(None),
) -> None:
    """Execute the pipeline for a template."""

    configure_logging()
    overrides: dict[str, object] = {}
    if fixture_dir:
        overrides["fixture_dir"] = fixture_dir
        overrides["verification_adapter"] = "fixture"
    summary = _runner(config).run(
        template,
        from_stage=from_stage,
        to_stage=to_stage,
        run_id=run_id,
        mode=mode,
        dry_run=dry_run,
        context_overrides=overrides,
    )
    _echo_json(summary)
    raise typer.Exit(code=0 if summary.get("outcome") in {"completed", "skipped"} else 1)


@run_app.command("resume")
def run_resume(
    run_id: str = typer.Argument(...),
    from_stage: str | None = typer.Option(None, "--from-stage"),
    config: Path | None = typer.Option(None),
) -> None:
    """Resume a run from a stage without repeating upstream acquisition."""

    runner = _runner(config)
    status = runner.status(run_id)
    if not status:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(code=1)
    template = status[0]["template"]
    summary = runner.run(template, from_stage=from_stage, run_id=run_id)
    _echo_json(summary)
    raise typer.Exit(code=0 if summary.get("outcome") in {"completed", "skipped"} else 1)


@app.command()
def candidates(run_id: str = typer.Argument(...), config: Path | None = typer.Option(None)) -> None:
    """Show the scored candidates for a run."""

    runner = _runner(config)
    status = runner.status(run_id)
    if not status:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(code=1)
    from mpvf.pipeline.artifacts import ArtifactStore

    store = ArtifactStore(status[0]["artifact_dir"])
    payload = store.read_json("properties", "candidates.json")
    for item in sorted(payload, key=lambda c: c.get("total_score", 0), reverse=True):
        marker = "★" if item.get("selected") else ("+" if item.get("alternate") else " ")
        record = item["property"]
        price = record.get("canonical", {}).get("price")
        typer.echo(
            f"{marker} {item.get('total_score', 0):5.1f}  {record.get('city') or '?':<18} "
            f"{('$' + format(price, ',')) if price else 'price ?':>12}  {record.get('normalized_address', '')[:52]}"
        )


@script_app.command("generate")
def script_generate(
    run_id: str = typer.Argument(...), config: Path | None = typer.Option(None)
) -> None:
    """Regenerate the script for an existing run."""

    runner = _runner(config)
    status = runner.status(run_id)
    if not status:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(code=1)
    summary = runner.run(
        status[0]["template"], from_stage="script", to_stage="script", run_id=run_id
    )
    _echo_json(summary)


@render_app.command("preview")
def render_preview(
    run_id: str = typer.Argument(...), config: Path | None = typer.Option(None)
) -> None:
    """Build the low-resolution preview (FR-143)."""

    runner = _runner(config)
    status = runner.status(run_id)
    summary = runner.run(
        status[0]["template"], from_stage="render", to_stage="render", run_id=run_id, dry_run=False
    )
    _echo_json(summary)


@render_app.command("final")
def render_final(
    run_id: str = typer.Argument(...), config: Path | None = typer.Option(None)
) -> None:
    """Build the 1080p master."""

    runner = _runner(config)
    status = runner.status(run_id)
    summary = runner.run(status[0]["template"], from_stage="render", to_stage="qa", run_id=run_id)
    _echo_json(summary)


@app.command()
def qa(run_id: str = typer.Argument(...), config: Path | None = typer.Option(None)) -> None:
    """Re-run QA against the existing artifacts."""

    runner = _runner(config)
    status = runner.status(run_id)
    summary = runner.run(status[0]["template"], from_stage="qa", to_stage="qa", run_id=run_id)
    _echo_json(summary)


@app.command()
def publish(
    run_id: str = typer.Argument(...),
    privacy: str = typer.Option("private", help="private|unlisted|public"),
    config: Path | None = typer.Option(None),
) -> None:
    """Assemble the publication package and upload."""

    runner = _runner(config)
    status = runner.status(run_id)
    if not status:
        typer.echo(f"unknown run: {run_id}", err=True)
        raise typer.Exit(code=1)
    if privacy != "private":
        typer.confirm(
            f"Upload this episode as {privacy}? Private-upload mode is the recommended default.",
            abort=True,
        )
    summary = runner.run(
        status[0]["template"], from_stage="publish", to_stage="publish", run_id=run_id
    )
    _echo_json(summary)


@app.command()
def status(
    run_id: str | None = typer.Argument(None), config: Path | None = typer.Option(None)
) -> None:
    """Show recent runs and their stages."""

    for item in _runner(config).status(run_id):
        typer.echo(f"{item['run_id']}  {item['template']:<22} {item['state']:<24} {item['mode']}")
        if item["failure_detail"]:
            typer.echo(f"   ↳ {item['failure_code']}: {item['failure_detail']}")
        if run_id:
            for stage in item["stages"]:
                duration = f"{stage['duration']}s" if stage["duration"] else "-"
                typer.echo(f"   {stage['name']:<12} {stage['state']:<10} {duration}")


@app.command()
def stages() -> None:
    """List the pipeline stages in order."""

    for spec in registry.ordered():
        typer.echo(f"{spec.order:>4}  {spec.name:<12} {spec.describe}")


# --------------------------------------------------------------------------
# sources / youtube / cleanup
# --------------------------------------------------------------------------


@sources_app.command("test")
def sources_test(config: Path | None = typer.Option(None)) -> None:
    """Health-check every registered adapter."""

    from mpvf.adapters.registry import (
        discovery_names,
        get_discovery_adapter,
        get_verification_adapter,
        verification_names,
    )

    settings = _settings(config)
    for name in discovery_names():
        health = get_discovery_adapter(name, settings).healthcheck()
        typer.echo(
            f"discovery  {name:<12} {'ok' if health.available else 'unavailable'}  {health.detail}"
        )
    for name in verification_names():
        health = get_verification_adapter(name, settings).healthcheck()
        typer.echo(
            f"verify     {name:<12} {'ok' if health.available else 'unavailable'}  {health.detail}"
        )


@youtube_app.command("auth")
def youtube_auth(
    headless: bool = typer.Option(False, help="Use the console flow instead of a local server"),
    config: Path | None = typer.Option(None),
) -> None:
    """Authorize the YouTube account and store the refresh token."""

    from mpvf.publish.youtube import CredentialsMissing, YouTubePublisher

    settings = _settings(config)
    publisher = YouTubePublisher(
        settings.secrets_dir / "youtube-client-secret.json",
        settings.secrets_dir / "youtube-token.json",
    )
    try:
        path = publisher.authorize(headless=headless)
    except CredentialsMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Token stored at {path}")


@youtube_app.command("state")
def youtube_state(config: Path | None = typer.Option(None)) -> None:
    """Show credential status without printing any secret."""

    from mpvf.publish.youtube import YouTubePublisher

    settings = _settings(config)
    publisher = YouTubePublisher(
        settings.secrets_dir / "youtube-client-secret.json",
        settings.secrets_dir / "youtube-token.json",
    )
    _echo_json(publisher.credential_state())


@app.command()
def cleanup(
    older_than: str = typer.Option("90d", "--older-than", help="e.g. 90d, 12h"),
    dry_run: bool = typer.Option(True, "--dry-run/--execute"),
    config: Path | None = typer.Option(None),
) -> None:
    """Apply the retention policy to run artifacts (§10.2)."""

    from mpvf.cli.cleanup import run_cleanup

    report = run_cleanup(_settings(config), older_than, dry_run=dry_run)
    _echo_json(report)


@app.command()
def serve(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
    config: Path | None = typer.Option(None),
) -> None:
    """Start the local dashboard."""

    import uvicorn

    settings = _settings(config)
    from mpvf.web.app import create_app

    uvicorn.run(
        create_app(settings),
        host=host or settings.dashboard_host,
        port=port or settings.dashboard_port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    app()
