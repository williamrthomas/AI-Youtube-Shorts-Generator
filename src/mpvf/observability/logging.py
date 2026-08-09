"""Structured logging and the operational event stream (§15.1).

Two sinks: JSON lines for machines (run artifact directory + optional global
log) and a readable console line for local operation. Tokens, cookies and
credential-shaped values are redacted before anything is written.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACT_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "access_token",
    "refresh_token",
    "client_secret",
    "client_id",
    "api_key",
    "password",
    "token",
    "set-cookie",
}

_TOKEN_PATTERN = re.compile(
    r"(?i)\b(ya29\.[\w.\-]+|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._\-]{12,})"
)


def redact(value: Any) -> Any:
    """Recursively redact secret-shaped keys and values."""

    if isinstance(value, dict):
        return {
            key: ("[redacted]" if str(key).lower() in REDACT_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _TOKEN_PATTERN.sub("[redacted]", value)
    return value


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "stage", "adapter", "property_key", "code", "duration_seconds"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        detail = getattr(record, "detail", None)
        if detail:
            payload["detail"] = redact(detail)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        run_id = getattr(record, "run_id", None)
        stage = getattr(record, "stage", None)
        prefix = " ".join(part for part in (run_id, stage) if part)
        context = f"[{prefix}] " if prefix else ""
        return f"{stamp} {record.levelname[0]} {context}{record.getMessage()}"


def configure_logging(
    level: str = "INFO",
    json_path: Path | str | None = None,
    console: bool = True,
) -> logging.Logger:
    """Install handlers on the ``mpvf`` logger. Safe to call repeatedly."""

    logger = logging.getLogger("mpvf")
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(ConsoleFormatter())
        logger.addHandler(stream)

    if json_path:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "mpvf") -> logging.Logger:
    return logging.getLogger(name if name.startswith("mpvf") else f"mpvf.{name}")


class RunLogger:
    """Logger bound to a run, mirroring events into the database."""

    def __init__(self, run_id: str, database: Any | None = None, stage: str | None = None) -> None:
        self.run_id = run_id
        self.database = database
        self.stage = stage
        self._logger = get_logger("run")

    def bind(self, stage: str) -> RunLogger:
        return RunLogger(self.run_id, self.database, stage)

    def event(
        self,
        code: str,
        message: str,
        severity: str = "info",
        **detail: Any,
    ) -> None:
        extra = {
            "run_id": self.run_id,
            "stage": self.stage,
            "code": code,
            "detail": detail or None,
        }
        log_level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(
            severity, logging.INFO
        )
        self._logger.log(log_level, message, extra=extra)
        self._persist(code, message, severity, detail)

    def info(self, code: str, message: str, **detail: Any) -> None:
        self.event(code, message, "info", **detail)

    def warning(self, code: str, message: str, **detail: Any) -> None:
        self.event(code, message, "warning", **detail)

    def error(self, code: str, message: str, **detail: Any) -> None:
        self.event(code, message, "error", **detail)

    def _persist(self, code: str, message: str, severity: str, detail: dict[str, Any]) -> None:
        if self.database is None:
            return
        from mpvf.models.db import EventRow  # local import avoids a cycle

        try:
            with self.database.session() as session:
                session.add(
                    EventRow(
                        run_id=self.run_id,
                        stage=self.stage,
                        severity=severity,
                        code=code,
                        message=message,
                        detail_json=redact(detail) or {},
                    )
                )
        except Exception:  # pragma: no cover - logging must never break a run
            self._logger.debug("failed to persist event %s", code, exc_info=True)
