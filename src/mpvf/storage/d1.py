"""Cloudflare D1 client for the control plane.

D1 is SQLite over HTTP. It is a good fit for the small, frequently-read rows
the dashboard needs — runs, stages, publications — so a Worker can serve the
dashboard without a container running.

The full relational store stays on local SQLite inside the run container: the
pipeline uses SQLAlchemy sessions, transactions and joins that a per-statement
HTTP API cannot serve efficiently. This class publishes the control plane to
D1; it does not attempt to be a SQLAlchemy dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from mpvf.observability.logging import get_logger

logger = get_logger("storage.d1")

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

CONTROL_PLANE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    template_slug TEXT NOT NULL,
    template_version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT,
    completed_at TEXT,
    failure_code TEXT,
    failure_detail TEXT,
    quality_score REAL,
    artifact_prefix TEXT
);
CREATE TABLE IF NOT EXISTS run_stages (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    error_code TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS publications (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    youtube_video_id TEXT,
    url TEXT,
    privacy TEXT,
    title TEXT,
    upload_state TEXT,
    scheduled_for TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);
"""


class D1Error(RuntimeError):
    pass


@dataclass
class D1Config:
    account_id: str
    database_id: str
    api_token: str
    api_base: str = CLOUDFLARE_API_BASE

    def complete(self) -> bool:
        return bool(self.account_id and self.database_id and self.api_token)

    @property
    def query_url(self) -> str:
        return (
            f"{self.api_base.rstrip('/')}/accounts/{self.account_id}"
            f"/d1/database/{self.database_id}/query"
        )


class D1Client:
    """Thin, typed wrapper over the D1 query endpoint."""

    def __init__(self, config: D1Config, client: httpx.Client | None = None, timeout: int = 30):
        self.config = config
        self.timeout = timeout
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def available(self) -> bool:
        return self.config.complete()

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run one statement and return its rows."""

        if not self.config.complete():
            raise D1Error("D1 is not configured (account id, database id and API token required)")

        payload: dict[str, Any] = {"sql": sql}
        if params:
            payload["params"] = [_bind(value) for value in params]

        try:
            response = self._http().post(
                self.config.query_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.config.api_token}"},
            )
        except httpx.HTTPError as exc:
            raise D1Error(f"D1 request failed: {exc}") from exc

        if response.status_code >= 400:
            raise D1Error(f"D1 {response.status_code}: {response.text[:300]}")

        body = response.json()
        if not body.get("success", False):
            errors = body.get("errors") or []
            raise D1Error(f"D1 rejected the statement: {errors[:2]}")

        results = body.get("result") or []
        rows: list[dict[str, Any]] = []
        for block in results:
            rows.extend(block.get("results") or [])
        return rows

    def execute_script(self, script: str) -> int:
        """Run a multi-statement script; returns the number of statements."""

        statements = [chunk.strip() for chunk in script.split(";") if chunk.strip()]
        for statement in statements:
            self.query(statement)
        return len(statements)

    def ensure_schema(self) -> int:
        return self.execute_script(CONTROL_PLANE_SCHEMA)

    # -- control-plane publishing ----------------------------------------
    def upsert_run(self, run: dict[str, Any]) -> None:
        self.query(
            """
            INSERT INTO runs (id, template_slug, template_version, state, mode, created_at,
                              completed_at, failure_code, failure_detail, quality_score,
                              artifact_prefix)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                completed_at = excluded.completed_at,
                failure_code = excluded.failure_code,
                failure_detail = excluded.failure_detail,
                quality_score = excluded.quality_score
            """,
            [
                run["id"],
                run["template_slug"],
                run.get("template_version", 1),
                run["state"],
                run.get("mode", ""),
                run.get("created_at"),
                run.get("completed_at"),
                run.get("failure_code"),
                run.get("failure_detail"),
                run.get("quality_score"),
                run.get("artifact_prefix"),
            ],
        )

    def upsert_stage(self, run_id: str, stage: dict[str, Any]) -> None:
        self.query(
            """
            INSERT INTO run_stages (run_id, name, state, attempt, duration_seconds, error_code)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, name) DO UPDATE SET
                state = excluded.state,
                attempt = excluded.attempt,
                duration_seconds = excluded.duration_seconds,
                error_code = excluded.error_code
            """,
            [
                run_id,
                stage["name"],
                stage.get("state", "pending"),
                stage.get("attempt", 0),
                stage.get("duration_seconds"),
                stage.get("error_code"),
            ],
        )

    def recent_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", [limit])


def _bind(value: Any) -> Any:
    """D1 accepts JSON scalars; coerce anything else to a string."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_client(settings: Any, credentials: Any = None, client: httpx.Client | None = None):
    """Return a D1 client, or ``None`` when D1 is not in use."""

    if settings.storage.database != "d1":
        return None
    config = D1Config(
        account_id=(credentials.cloudflare_account_id if credentials else "") or "",
        database_id=settings.storage.d1_database_id,
        api_token=(credentials.cloudflare_api_token if credentials else "") or "",
    )
    if not config.complete() and client is None:
        logger.warning("d1 selected but not configured")
        return None
    return D1Client(config, client=client)
