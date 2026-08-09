"""Cloudflare R2 artifact storage (§9.5).

Runs write to the local filesystem first — FFmpeg, Playwright and Pillow all
need real files — and the run directory is then mirrored to R2 so the compute
node can be discarded. That ordering is deliberate: object storage is the
durable copy, not the working copy.

R2 is S3-compatible, so this speaks S3 through ``boto3`` when it is installed.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mpvf.observability.logging import get_logger
from mpvf.pipeline.artifacts import ArtifactStore

logger = get_logger("storage.r2")

# Big and re-derivable: mirrored last, and skipped entirely by --skip-bulk.
BULK_DIRECTORIES = frozenset({"pages", "assets"})


class R2Unavailable(RuntimeError):
    """boto3 is not installed, or R2 is not configured."""


@dataclass
class R2Config:
    bucket: str
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    prefix: str = "runs"
    region: str = "auto"

    @classmethod
    def from_settings(cls, settings: Any, credentials: Any = None) -> R2Config:
        """Build config from settings plus environment/secret-file credentials."""

        import os

        storage = settings.storage
        endpoint = storage.r2_endpoint
        if not endpoint:
            account = (
                credentials.cloudflare_account_id if credentials else None
            ) or os.environ.get("MPVF_CLOUDFLARE_ACCOUNT_ID", "")
            if account:
                endpoint = f"https://{account}.r2.cloudflarestorage.com"

        def secret(*names: str) -> str:
            for name in names:
                value = os.environ.get(name)
                if value:
                    return value.strip()
            directory = Path(settings.secrets_dir)
            for name in names:
                path = directory / name.lower().replace("_", "-").removeprefix("mpvf-")
                if path.exists():
                    return path.read_text(encoding="utf-8").strip()
            return ""

        return cls(
            bucket=storage.r2_bucket,
            endpoint_url=endpoint,
            access_key_id=secret("MPVF_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
            secret_access_key=secret("MPVF_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
            prefix=storage.r2_prefix,
        )

    def complete(self) -> bool:
        return bool(
            self.bucket and self.endpoint_url and self.access_key_id and self.secret_access_key
        )

    def missing(self) -> list[str]:
        names = {
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
        }
        return sorted(name for name, value in names.items() if not value)


@dataclass
class MirrorResult:
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_uploaded: int = 0

    @property
    def count(self) -> int:
        return len(self.uploaded)


class R2ArtifactStore:
    """Mirrors a local run directory to R2 and restores it again.

    Deliberately not a drop-in replacement for :class:`ArtifactStore`: stages
    keep using real files, and this handles durability at the run boundary.
    """

    def __init__(self, config: R2Config, client: Any = None) -> None:
        self.config = config
        self._client = client

    def _s3(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.complete():
            raise R2Unavailable(f"R2 is not configured; missing {', '.join(self.config.missing())}")
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise R2Unavailable("boto3 is required for R2; install the 'cloud' extra") from exc

        self._client = boto3.client(
            "s3",
            endpoint_url=self.config.endpoint_url,
            aws_access_key_id=self.config.access_key_id,
            aws_secret_access_key=self.config.secret_access_key,
            region_name=self.config.region,
        )
        return self._client

    def key_for(self, run_id: str, relative_path: str) -> str:
        prefix = self.config.prefix.strip("/")
        parts = [part for part in (prefix, run_id, relative_path.replace("\\", "/")) if part]
        return "/".join(parts)

    def mirror(
        self,
        store: ArtifactStore,
        run_id: str,
        include_bulk: bool = True,
    ) -> MirrorResult:
        """Upload every file in the run directory."""

        client = self._s3()
        result = MirrorResult()

        for path in sorted(store.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(store.root).as_posix()
            top = relative.split("/", 1)[0]
            if not include_bulk and top in BULK_DIRECTORIES:
                result.skipped.append(relative)
                continue

            key = self.key_for(run_id, relative)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            client.upload_file(
                str(path),
                self.config.bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
            result.uploaded.append(key)
            result.bytes_uploaded += path.stat().st_size

        logger.info(
            "mirrored run to r2",
            extra={
                "run_id": run_id,
                "detail": {
                    "objects": result.count,
                    "megabytes": round(result.bytes_uploaded / 1024**2, 2),
                    "skipped": len(result.skipped),
                },
            },
        )
        return result

    def restore(self, store: ArtifactStore, run_id: str) -> list[str]:
        """Pull a run's artifacts back down so a stage can be re-run."""

        client = self._s3()
        prefix = self.key_for(run_id, "")
        restored: list[str] = []

        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                relative = key[len(prefix) :].lstrip("/")
                if not relative:
                    continue
                destination = store.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(self.config.bucket, key, str(destination))
                restored.append(relative)
        return restored

    def list_run(self, run_id: str) -> list[str]:
        client = self._s3()
        prefix = self.key_for(run_id, "")
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def public_url(self, run_id: str, relative_path: str) -> str:
        """Object URL, for the dashboard to link at rather than proxy."""

        return f"{self.config.endpoint_url.rstrip('/')}/{self.config.bucket}/{self.key_for(run_id, relative_path)}"


def build_store(
    settings: Any, credentials: Any = None, client: Any = None
) -> R2ArtifactStore | None:
    """Return a configured store, or ``None`` when R2 is not in use."""

    if settings.storage.artifacts != "r2":
        return None
    config = R2Config.from_settings(settings, credentials)
    if not config.complete() and client is None:
        logger.warning(
            "r2 selected but not configured",
            extra={"detail": {"missing": config.missing()}},
        )
        return None
    return R2ArtifactStore(config, client=client)
