"""Filesystem artifact manager (§9.5).

Every run owns one directory with a fixed subdirectory layout. Stages write
through this object so paths never get invented ad hoc and so a rerun can find
the previous stage's output without re-fetching anything (§17).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SUBDIRECTORIES = (
    "discovery",
    "pages",
    "sources",
    "properties",
    "assets",
    "research",
    "evidence",
    "script",
    "audio",
    "captions",
    "render",
    "thumbnails",
    "publish",
    "logs",
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "file", max_length: int = 120) -> str:
    """Sanitize a filename derived from acquired content (§16)."""

    cleaned = _UNSAFE.sub("-", name).strip("-._")
    cleaned = cleaned or fallback
    if len(cleaned) > max_length:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[: max_length - 9]}-{digest}"
    return cleaned


class ArtifactStore:
    """Owns one run directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @classmethod
    def for_run(
        cls,
        runs_dir: Path | str,
        run_id: str,
        template_slug: str,
        on_date: date | None = None,
    ) -> ArtifactStore:
        stamp = (on_date or date.today()).isoformat()
        directory = Path(runs_dir) / f"{stamp}_{safe_filename(template_slug)}_{run_id}"
        store = cls(directory)
        store.initialize()
        return store

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRECTORIES:
            (self.root / name).mkdir(exist_ok=True)

    # -- paths ------------------------------------------------------------
    def dir(self, name: str) -> Path:
        if name not in SUBDIRECTORIES:
            raise KeyError(f"unknown artifact directory: {name}")
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, name: str, *parts: str) -> Path:
        target = self.dir(name)
        for part in parts[:-1]:
            target = target / safe_filename(part)
        target.mkdir(parents=True, exist_ok=True)
        return target / safe_filename(parts[-1]) if parts else target

    # -- io ---------------------------------------------------------------
    def write_json(self, name: str, filename: str, payload: Any) -> Path:
        path = self.path(name, filename)
        if isinstance(payload, BaseModel):
            text = payload.model_dump_json(indent=2)
        else:
            text = json.dumps(payload, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def read_json(self, name: str, filename: str) -> Any:
        path = self.path(name, filename)
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def read_model(self, name: str, filename: str, model: type[BaseModel]) -> Any:
        return model.model_validate(self.read_json(name, filename))

    def exists(self, name: str, filename: str) -> bool:
        return self.path(name, filename).exists()

    def write_text(self, name: str, filename: str, text: str) -> Path:
        path = self.path(name, filename)
        path.write_text(text, encoding="utf-8")
        return path

    def write_bytes(self, name: str, filename: str, payload: bytes) -> Path:
        path = self.path(name, filename)
        path.write_bytes(payload)
        return path

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def relative(self, path: Path | str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)

    def prune(self, name: str) -> None:
        """Remove and recreate a subdirectory (used when a stage is rerun)."""

        target = self.root / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def hash_inputs(*parts: Any) -> str:
    """Stable content hash used for stage idempotency (§6.1)."""

    hasher = hashlib.sha256()
    for part in parts:
        if isinstance(part, BaseModel):
            hasher.update(part.model_dump_json().encode("utf-8"))
        elif isinstance(part, (bytes, bytearray)):
            hasher.update(bytes(part))
        else:
            hasher.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:16]
