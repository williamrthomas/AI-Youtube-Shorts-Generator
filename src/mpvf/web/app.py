"""Local operator dashboard (§7.18).

FastAPI + Jinja2 + HTMX, bound to localhost by default (§16). Every page is
server-rendered; there is no build step and no external asset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from mpvf.cli.doctor import run_doctor
from mpvf.config.settings import Settings, load_settings
from mpvf.config.templates import TemplateRegistry
from mpvf.models.db import Database, EventRow, RunRow, StageRow
from mpvf.pipeline.artifacts import ArtifactStore
from mpvf.pipeline.stages import registry as stage_registry

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _store_for(database: Database, run_id: str) -> tuple[RunRow, ArtifactStore]:
    with database.session() as session:
        row = session.get(RunRow, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return row, ArtifactStore(row.artifact_dir)


def _read(store: ArtifactStore, directory: str, filename: str, default: Any = None) -> Any:
    try:
        return store.read_json(directory, filename)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_directories()
    database = Database(settings.database_url())
    database.create_all()

    app = FastAPI(title="Maine Property Video Factory", docs_url="/api/docs")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["money"] = lambda value: f"${value:,}" if value else "—"
    templates.env.filters["shortid"] = lambda value: str(value)[-8:] if value else ""

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name=name, context={"settings": settings, **context}
        )

    # -- today ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        """FR-191: current run, stage, elapsed time, candidates, blockers."""

        with database.session() as session:
            runs = session.query(RunRow).order_by(RunRow.created_at.desc()).limit(20).all()
            current = runs[0] if runs else None
            stages = session.query(StageRow).filter_by(run_id=current.id).all() if current else []
            events = (
                session.query(EventRow)
                .filter_by(run_id=current.id)
                .order_by(EventRow.created_at.desc())
                .limit(25)
                .all()
                if current
                else []
            )
        return page(
            request,
            "today.html",
            runs=runs,
            current=current,
            stages=sorted(stages, key=lambda s: s.started_at or s.id),
            events=events,
            stage_names=[spec.name for spec in stage_registry.ordered()],
        )

    # -- run detail -------------------------------------------------------
    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        with database.session() as session:
            stages = session.query(StageRow).filter_by(run_id=run_id).all()
            events = (
                session.query(EventRow)
                .filter_by(run_id=run_id)
                .order_by(EventRow.created_at.desc())
                .limit(80)
                .all()
            )
        return page(
            request,
            "run.html",
            run=row,
            stages=sorted(stages, key=lambda s: s.started_at or s.id),
            events=events,
            qa=_read(store, "render", "qa-report.json"),
            repairs=_read(store, "render", "repair-plan.json", []),
        )

    # -- candidates (FR-192) ----------------------------------------------
    @app.get("/runs/{run_id}/candidates", response_class=HTMLResponse)
    def candidates(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        payload = _read(store, "properties", "candidates.json", [])
        payload.sort(key=lambda item: item.get("total_score", 0), reverse=True)
        return page(request, "candidates.html", run=row, candidates=payload)

    # -- evidence (FR-193) -------------------------------------------------
    @app.get("/runs/{run_id}/evidence", response_class=HTMLResponse)
    def evidence(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        bundle = _read(store, "evidence", "bundle.json", {})
        sources = {source["source_id"]: source for source in bundle.get("sources", [])}
        claims = bundle.get("claims", [])
        for claim in claims:
            claim["source_records"] = [
                sources[source_id] for source_id in claim.get("sources", []) if source_id in sources
            ]
        return page(
            request,
            "evidence.html",
            run=row,
            bundle=bundle,
            claims=claims,
            gaps=_read(store, "evidence", "gaps.json", []),
        )

    # -- script (FR-194) ---------------------------------------------------
    @app.get("/runs/{run_id}/script", response_class=HTMLResponse)
    def script(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        return page(
            request,
            "script.html",
            run=row,
            script=_read(store, "script", "script.json", {}),
            validation=_read(store, "script", "validation.json", {}),
        )

    # -- assets (FR-195) ---------------------------------------------------
    @app.get("/runs/{run_id}/assets", response_class=HTMLResponse)
    def assets(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        payload = _read(store, "assets", "manifest.json", {"assets": [], "by_property": {}})
        return page(
            request,
            "assets.html",
            run=row,
            assets=payload.get("assets", []),
            by_property=payload.get("by_property", {}),
        )

    # -- render + publication (FR-196, FR-197) -----------------------------
    @app.get("/runs/{run_id}/render", response_class=HTMLResponse)
    def render(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        return page(
            request,
            "render.html",
            run=row,
            plan=_read(store, "render", "scene-plan.json", {}),
            qa=_read(store, "render", "qa-report.json"),
            manifest=_read(store, "render", "render-manifest.json", {}),
            preview_exists=store.path("render", "preview.mp4").exists(),
        )

    @app.get("/runs/{run_id}/publication", response_class=HTMLResponse)
    def publication(request: Request, run_id: str) -> HTMLResponse:
        row, store = _store_for(database, run_id)
        return page(
            request,
            "publication.html",
            run=row,
            metadata=_read(store, "publish", "metadata.json", {}),
            publication=_read(store, "publish", "publication.json", {}),
            thumbnails=sorted(p.name for p in store.dir("thumbnails").glob("thumbnail-*")),
        )

    # -- settings (FR-199) -------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return page(
            request,
            "settings.html",
            doctor=run_doctor(settings),
            templates_list=TemplateRegistry(settings.templates_dir).load_all(),
        )

    # -- media + api -------------------------------------------------------
    @app.get("/runs/{run_id}/media/{directory}/{filename}")
    def media(run_id: str, directory: str, filename: str) -> FileResponse:
        _row, store = _store_for(database, run_id)
        try:
            path = store.path(directory, filename)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{directory}/{filename} not found")
        return FileResponse(path)

    @app.get("/api/runs")
    def api_runs() -> JSONResponse:
        with database.session() as session:
            rows = session.query(RunRow).order_by(RunRow.created_at.desc()).limit(100).all()
            return JSONResponse(
                [
                    {
                        "run_id": row.id,
                        "template": row.template_slug,
                        "state": row.state,
                        "mode": row.mode,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "failure_code": row.failure_code,
                    }
                    for row in rows
                ]
            )

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        report = run_doctor(settings)
        return JSONResponse(report, status_code=200 if report["ok"] else 503)

    return app
