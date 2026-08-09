"""Asset acquisition: bounded, validated image download (FR-050, FR-051, FR-059).

Downloads are size-capped, MIME-validated, checksummed and perceptually
hashed. Nothing acquired is ever executed or trusted as a filename (§16).
"""

from __future__ import annotations

import hashlib
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

from mpvf.config.settings import AcquisitionSettings
from mpvf.media.imaging import ImageProbe, perceptual_hash, probe_image
from mpvf.models.domain import AssetRecord, utcnow
from mpvf.observability.logging import get_logger
from mpvf.pipeline.artifacts import safe_filename

logger = get_logger("media.download")

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class DownloadRejected(RuntimeError):
    pass


class AssetDownloader:
    def __init__(
        self,
        destination: Path | str,
        settings: AcquisitionSettings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        self.settings = settings or AcquisitionSettings()
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": self.settings.user_agent},
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    def fetch_one(
        self,
        url: str,
        property_key: str | None = None,
        source_page: str = "",
        credit_text: str = "",
        license_text: str = "",
    ) -> AssetRecord:
        """Download and validate a single image."""

        response = self._http().get(url)
        response.raise_for_status()

        mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not mime:
            mime = mimetypes.guess_type(url)[0] or ""
        if mime not in self.settings.allowed_image_mime:
            raise DownloadRejected(f"disallowed mime {mime!r} for {url}")

        payload = response.content
        if len(payload) > self.settings.max_download_bytes:
            raise DownloadRejected(
                f"{url} is {len(payload)} bytes, over the {self.settings.max_download_bytes} cap"
            )

        digest = hashlib.sha256(payload).hexdigest()
        stem = safe_filename(Path(urlparse(url).path).stem or digest[:12])
        path = self.destination / f"{digest[:16]}-{stem}{_EXTENSIONS.get(mime, '.img')}"
        path.write_bytes(payload)

        probe: ImageProbe = probe_image(path)
        if not probe.valid:
            path.unlink(missing_ok=True)
            raise DownloadRejected(f"{url} did not decode as an image: {probe.detail}")

        return AssetRecord(
            property_key=property_key,
            source_url=url,
            source_page=source_page,
            local_path=str(path),
            width=probe.width,
            height=probe.height,
            mime_type=mime,
            sha256=digest,
            perceptual_hash=perceptual_hash(path),
            credit_text=credit_text,
            license_text=license_text,
            retrieved_at=utcnow(),
        )

    def fetch_many(
        self,
        urls: list[str],
        property_key: str | None = None,
        source_page: str = "",
        credit_text: str = "",
        limit: int | None = None,
    ) -> list[AssetRecord]:
        """Download with bounded concurrency, skipping individual failures."""

        targets = list(dict.fromkeys(urls))[: limit or len(urls)]
        records: list[AssetRecord] = []
        workers = max(1, min(self.settings.max_concurrency, len(targets) or 1))

        def task(url: str) -> AssetRecord | None:
            try:
                return self.fetch_one(url, property_key, source_page, credit_text)
            except (httpx.HTTPError, DownloadRejected, OSError) as exc:
                logger.warning(
                    "asset download skipped",
                    extra={"property_key": property_key, "detail": {"url": url, "error": str(exc)}},
                )
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for record in pool.map(task, targets):
                if record is not None:
                    records.append(record)
        return records

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def deduplicate(records: list[AssetRecord], hamming_threshold: int = 6) -> list[AssetRecord]:
    """Drop exact and near-duplicate images (FR-052)."""

    kept: list[AssetRecord] = []
    seen_digests: set[str] = set()
    for record in records:
        if record.sha256 and record.sha256 in seen_digests:
            continue
        if record.perceptual_hash and any(
            hamming(record.perceptual_hash, other.perceptual_hash) <= hamming_threshold
            for other in kept
            if other.perceptual_hash
        ):
            continue
        seen_digests.add(record.sha256)
        kept.append(record)
    return kept


def hamming(left: str, right: str) -> int:
    """Hamming distance between two hex perceptual hashes."""

    if not left or not right or len(left) != len(right):
        return 64
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return 64
