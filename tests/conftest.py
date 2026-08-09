"""Shared test fixtures. No test in this suite touches the network (§22)."""

from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from mpvf.config.settings import Settings
from mpvf.config.templates import SearchTemplate
from mpvf.models.db import Database
from mpvf.models.domain import (
    AssetRecord,
    Candidate,
    Claim,
    EvidenceBundle,
    ListingObservation,
    PropertyRecord,
    SourceRecord,
)
from mpvf.normalization.dedup import build_property_records
from mpvf.normalization.listing import build_observation
from mpvf.selection.verification import build_verification
from mpvf.speech.pronunciation import PronunciationLexicon

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def zillow_html():
    def _load(name: str) -> str:
        return (FIXTURE_DIR / "zillow" / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def listing_payloads() -> list[dict]:
    payload = json.loads((FIXTURE_DIR / "listings" / "coastal.json").read_text(encoding="utf-8"))
    return payload["listings"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted entirely inside tmp_path."""

    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        templates_dir=templates_dir,
        pronunciation_path=tmp_path / "pronunciation.yaml",
        source_domains_path=tmp_path / "source-domains.yaml",
    )
    settings.provider.kind = "deterministic"
    settings.speech.engine = "null"
    settings.mix.music_enabled = False
    settings.ensure_directories()
    return settings


@pytest.fixture
def template(settings: Settings) -> SearchTemplate:
    payload = {
        "version": 1,
        "slug": "coastal-under-1m",
        "name": "Five Stunning Maine Coastal Homes Under $1M",
        "editorial_promise": "Five Maine coastal homes under a million dollars",
        "result_count": 5,
        "alternates_count": 1,
        "target_words": 500,
        "word_range": [220, 900],
        "min_candidate_quality": 20,
        "title_patterns": ["{count} Stunning Maine Coastal Homes Under {ceiling}"],
        "discovery": {"adapter": "fixture", "search_urls": ["fixture://coastal"], "max_pages": 1},
        "hard_filters": {
            "status": ["active"],
            "property_types": ["single_family", "condo", "seasonal_residence"],
            "price_max": 999999,
            "state": "ME",
            "min_usable_images": 8,
        },
        "theme_rules": {
            "any_of": [
                "owned_ocean_frontage",
                "ocean_view",
                "deeded_beach_access",
                "island_location",
                "verified_coastal_village",
            ]
        },
        "publishing": {"playlist": "Maine Coastal Homes", "privacy": "private"},
    }
    path = settings.templates_dir / "coastal-under-1m.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return SearchTemplate.model_validate(payload)


@pytest.fixture
def database(settings: Settings) -> Database:
    database = Database(settings.database_url())
    database.create_all()
    return database


@pytest.fixture
def observations(listing_payloads: list[dict]) -> list[ListingObservation]:
    return [
        build_observation("fixture", dict(payload), str(payload["listing_url"]))
        for payload in listing_payloads
    ]


@pytest.fixture
def records(observations: list[ListingObservation]) -> list[PropertyRecord]:
    """Verified property records, as the select stage would see them."""

    records = build_property_records(observations)
    for record in records:
        second = record.canonical.model_copy(deep=True)
        second.source = "brokerage"
        record.verification = build_verification(
            record.property_key, record.canonical, second, "brokerage"
        )
    return records


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff0300000600"
    "05fdf1e7a10000000049454e44ae426082"
)


def _wide_png(width: int = 1920, height: int = 1080, seed: int = 0) -> bytes:
    """A decodable JPEG at render resolution with a seed-dependent pattern.

    The pattern has to vary between images: a flat colour would collide under
    perceptual hashing and the deduplicator would (correctly) throw them away.
    """

    from PIL import Image, ImageDraw

    base = (40 + seed * 7 % 120, 60 + seed * 13 % 140, 90 + seed * 29 % 150)
    image = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(image)
    rng = random.Random(seed)
    for _ in range(14):
        x0 = rng.randrange(0, width - 200)
        y0 = rng.randrange(0, height - 200)
        shade = rng.randrange(0, 256)
        draw.rectangle(
            [x0, y0, x0 + rng.randrange(80, 400), y0 + rng.randrange(80, 300)],
            fill=(shade, (shade + 60) % 256, (shade + 130) % 256),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=75)
    return buffer.getvalue()


@pytest.fixture
def image_client() -> httpx.Client:
    """An httpx client that serves generated images for any images.test URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "images.test" not in request.url.host:
            return httpx.Response(404)
        # Seed from the URL so the same image is byte-identical on refetch and
        # different images stay visually distinct.
        payload = _wide_png(seed=abs(hash(str(request.url))) % 10_000)
        return httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def lexicon() -> PronunciationLexicon:
    return PronunciationLexicon()


@pytest.fixture
def bundle(records: list[PropertyRecord], template: SearchTemplate, lexicon) -> EvidenceBundle:
    """A minimal but complete evidence bundle for scripting tests."""

    from mpvf.evidence.bundle import build_bundle
    from mpvf.research.claims import listing_claims
    from mpvf.selection.scoring import build_candidates, select_lineup

    eligible = [record for record in records if record.canonical.status == "active"][:6]
    candidates = build_candidates(eligible, template)
    select_lineup(candidates, template)

    sources: list[SourceRecord] = []
    claims: list[Claim] = []
    assets: list[AssetRecord] = []
    for candidate in candidates:
        record = candidate.property
        source = SourceRecord(
            property_key=record.property_key,
            type="verification",
            url=record.canonical.listing_url or "https://example.test",
            title=f"{record.town} listing",
            publisher="brokerage",
            reliability_tier="A",
        )
        sources.append(source)
        claims.extend(listing_claims(record, [source.source_id]))
        for index, category in enumerate(
            [
                "exterior",
                "aerial",
                "water_view",
                "kitchen",
                "living_room",
                "bedroom",
                "bathroom",
                "detail",
            ]
        ):
            assets.append(
                AssetRecord(
                    property_key=record.property_key,
                    source_id=source.source_id,
                    category=category,  # type: ignore[arg-type]
                    source_url=f"https://images.test/{record.property_key}-{index}.jpg",
                    local_path="",
                    width=1920,
                    height=1080,
                    sha256=f"{record.property_key}{index}",
                    quality_score=0.9 - index * 0.03,
                    selected=True,
                    hero=index == 0,
                )
            )

    return build_bundle(
        run_id="run_test",
        template=template,
        candidates=candidates,
        claims=claims,
        sources=sources,
        assets=assets,
        lexicon=lexicon,
        checked_at=datetime.now(UTC),
    )


@pytest.fixture
def selected_candidates(bundle: EvidenceBundle) -> list[Candidate]:
    return [candidate for candidate in bundle.candidates if candidate.selected]
