"""Publication package assembly (§18).

The publish directory is the deliverable: master, preview, thumbnails,
captions, script, description, metadata, chapters, sources, asset manifest, QA
report, render manifest and the publication record.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpvf.models.domain import (
    EvidenceBundle,
    PublicationMetadata,
    PublicationResult,
    QAReport,
    Script,
)
from mpvf.pipeline.artifacts import ArtifactStore

REQUIRED_FILES = (
    "master.mp4",
    "preview.mp4",
    "thumbnail-a.jpg",
    "captions.srt",
    "captions.vtt",
    "script.md",
    "script.json",
    "description.md",
    "metadata.json",
    "chapters.json",
    "sources.json",
    "assets-manifest.json",
    "qa-report.json",
    "render-manifest.json",
)


@dataclass
class PackageResult:
    directory: Path
    files: dict[str, Path]
    missing: list[str]

    @property
    def complete(self) -> bool:
        return not self.missing


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def sources_payload(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source.source_id,
            "type": source.type,
            "url": source.url,
            "title": source.title,
            "publisher": source.publisher,
            "retrieved_at": source.retrieved_at.isoformat(),
            "reliability_tier": source.reliability_tier,
            "property_key": source.property_key,
        }
        for source in bundle.sources
    ]


def assets_manifest(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    """Source-credit manifest for every image that reaches the render (FR-057)."""

    return [
        {
            "asset_id": asset.asset_id,
            "property_key": asset.property_key,
            "category": asset.category,
            "source_url": asset.source_url,
            "source_page": asset.source_page,
            "local_path": asset.local_path,
            "width": asset.width,
            "height": asset.height,
            "sha256": asset.sha256,
            "perceptual_hash": asset.perceptual_hash,
            "credit": asset.credit_text,
            "license": asset.license_text,
            "selected": asset.selected,
            "hero": asset.hero,
        }
        for asset in bundle.assets
        if asset.selected
    ]


def build_package(
    store: ArtifactStore,
    bundle: EvidenceBundle,
    script: Script,
    script_markdown: str,
    metadata: PublicationMetadata,
    qa_report: QAReport,
    render_manifest: dict[str, Any],
    master_path: Path | str | None,
    preview_path: Path | str | None,
    caption_paths: dict[str, Path] | None = None,
    thumbnail_paths: list[Path] | None = None,
    publication: PublicationResult | None = None,
) -> PackageResult:
    """Copy and write everything §18 requires into ``publish/``."""

    directory = store.dir("publish")
    files: dict[str, Path] = {}

    def copy(source: Path | str | None, name: str) -> None:
        if source and Path(source).exists():
            target = directory / name
            if Path(source).resolve() != target.resolve():
                shutil.copy2(source, target)
            files[name] = target

    copy(master_path, "master.mp4")
    copy(preview_path, "preview.mp4")
    for key, path in (caption_paths or {}).items():
        copy(path, f"captions.{key}")
    for index, path in enumerate(thumbnail_paths or []):
        copy(path, f"thumbnail-{chr(ord('a') + index)}{Path(path).suffix or '.jpg'}")

    files["script.md"] = directory / "script.md"
    files["script.md"].write_text(script_markdown, encoding="utf-8")
    files["script.json"] = _write_json(directory / "script.json", script.model_dump(mode="json"))
    files["description.md"] = directory / "description.md"
    files["description.md"].write_text(metadata.description, encoding="utf-8")
    files["metadata.json"] = _write_json(
        directory / "metadata.json", metadata.model_dump(mode="json")
    )
    files["chapters.json"] = _write_json(directory / "chapters.json", metadata.chapters)
    files["sources.json"] = _write_json(directory / "sources.json", sources_payload(bundle))
    files["assets-manifest.json"] = _write_json(
        directory / "assets-manifest.json", assets_manifest(bundle)
    )
    files["qa-report.json"] = _write_json(
        directory / "qa-report.json", qa_report.model_dump(mode="json")
    )
    files["render-manifest.json"] = _write_json(directory / "render-manifest.json", render_manifest)
    files["evidence.json"] = _write_json(
        directory / "evidence.json", bundle.model_dump(mode="json")
    )
    if publication is not None:
        files["publication.json"] = _write_json(
            directory / "publication.json", publication.model_dump(mode="json")
        )

    missing = [name for name in REQUIRED_FILES if name not in files]
    return PackageResult(directory=directory, files=files, missing=missing)


def write_publication_record(store: ArtifactStore, publication: PublicationResult) -> Path:
    return _write_json(
        store.dir("publish") / "publication.json", publication.model_dump(mode="json")
    )
