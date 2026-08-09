"""Retention cleanup (§10.2, FR of `mpvf cleanup`).

Published masters are never removed unless the operator explicitly configures
it, and every destructive action is recorded in the append-only event stream.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mpvf.config.settings import Settings
from mpvf.models.db import Database, EventRow, PublicationRow, RunRow

_DURATION = re.compile(r"^(\d+)\s*([dhwm])$", re.IGNORECASE)

# What each retention bucket covers inside a run directory.
BUCKETS = {
    "raw_pages": ("pages", "raw_pages_days"),
    "unused_images": ("assets", "unused_images_days"),
    "preview_renders": ("render", "preview_renders_days"),
}


def parse_duration(value: str) -> timedelta:
    match = _DURATION.match(value.strip())
    if not match:
        raise ValueError(f"cannot parse duration {value!r}; use forms like 90d, 12h, 4w")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
        "m": timedelta(days=30 * amount),
    }[unit]


def _published_run_ids(database: Database) -> set[str]:
    with database.session() as session:
        return {
            row.run_id
            for row in session.query(PublicationRow).all()
            if row.youtube_video_id or row.upload_state in {"uploaded", "scheduled", "published"}
        }


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def run_cleanup(settings: Settings, older_than: str, dry_run: bool = True) -> dict[str, Any]:
    """Delete expired intermediates; report what was (or would be) freed."""

    cutoff = datetime.now(UTC) - parse_duration(older_than)
    database = Database(settings.database_url())
    database.create_all()
    protected = (
        _published_run_ids(database) if settings.retention.protect_published_masters else set()
    )

    actions: list[dict[str, Any]] = []
    freed = 0

    with database.session() as session:
        runs = session.query(RunRow).all()
        run_index = {run.artifact_dir: run for run in runs if run.artifact_dir}

    for directory in sorted(settings.runs_dir.glob("*")):
        if not directory.is_dir():
            continue
        run = run_index.get(str(directory))
        modified = datetime.fromtimestamp(directory.stat().st_mtime, tz=UTC)
        if modified > cutoff:
            continue

        for bucket, (subdir, setting_name) in BUCKETS.items():
            target = directory / subdir
            if not target.exists():
                continue
            bucket_cutoff = datetime.now(UTC) - timedelta(
                days=getattr(settings.retention, setting_name)
            )
            if modified > bucket_cutoff:
                continue

            is_protected = run is not None and run.id in protected
            for item in sorted(target.iterdir()):
                # Masters and thumbnails survive; previews and raw pages do not.
                if is_protected and item.name in {"master.mp4", "render-manifest.json"}:
                    continue
                if bucket == "preview_renders" and item.name not in {"preview.mp4"}:
                    continue
                size = item.stat().st_size if item.is_file() else _directory_size(item)
                actions.append(
                    {
                        "run_dir": str(directory),
                        "bucket": bucket,
                        "path": str(item),
                        "bytes": size,
                        "protected_run": is_protected,
                    }
                )
                freed += size
                if not dry_run:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

    if not dry_run and actions:
        with database.session() as session:
            session.add(
                EventRow(
                    severity="warning",
                    code="retention_cleanup",
                    message=f"removed {len(actions)} artifacts, freeing {freed / 1024**2:.1f} MB",
                    detail_json={"older_than": older_than, "count": len(actions)},
                )
            )

    return {
        "dry_run": dry_run,
        "older_than": older_than,
        "cutoff": cutoff.isoformat(),
        "actions": actions[:200],
        "action_count": len(actions),
        "bytes_freed": freed,
        "megabytes_freed": round(freed / 1024**2, 2),
        "protected_runs": len(protected),
    }
