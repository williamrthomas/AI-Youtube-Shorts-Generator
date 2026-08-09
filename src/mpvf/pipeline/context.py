"""Run context: the object every stage receives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mpvf.config.settings import Settings
from mpvf.config.templates import SearchTemplate
from mpvf.models.db import Database
from mpvf.observability.logging import RunLogger
from mpvf.pipeline.artifacts import ArtifactStore


@dataclass
class RunContext:
    """Everything a stage is allowed to reach for.

    Stages must not open their own database connections or invent paths; they
    go through this object so that reruns, logging and artifact layout stay
    consistent.
    """

    run_id: str
    settings: Settings
    template: SearchTemplate
    store: ArtifactStore
    database: Database
    logger: RunLogger
    mode: str = "private_upload"
    dry_run: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)

    def stage_logger(self, stage: str) -> RunLogger:
        return self.logger.bind(stage)

    def get(self, key: str, default: Any = None) -> Any:
        return self.scratch.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self.scratch[key] = value
