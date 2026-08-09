"""Starter configuration written by ``mpvf init``.

Everything user-editable lives outside application code (§22): settings,
templates, pronunciation entries and the verification-domain allow list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from mpvf.config.settings import Settings
from mpvf.speech.pronunciation import PronunciationLexicon

SETTINGS_EXAMPLE = """# Maine Property Video Factory settings.
# Copy to config/settings.yaml and edit. Secrets never belong in this file.

data_dir: data
secrets_dir: secrets
templates_dir: config/templates
pronunciation_path: config/pronunciation.yaml
source_domains_path: config/source-domains.yaml

timezone: America/New_York
# review | private_upload | scheduled | full_auto
# Keep private_upload until the twenty-run pilot passes (§4.3).
publishing_mode: private_upload
earliest_publish_hour: 17

provider:
  kind: ollama            # ollama | deterministic | openai_compatible
  model: qwen2.5:14b-instruct
  host: http://127.0.0.1:11434
  temperature: 0.4
  fallback: deterministic

speech:
  engine: kokoro
  voice: af_heart
  speed: 1.0
  aligner: faster_whisper

render:
  width: 1920
  height: 1080
  fps: 30
  video_crf: 18

mix:
  music_enabled: true
  target_lufs: -14.0
  true_peak_db: -1.0

acquisition:
  request_delay_seconds: 2.5
  max_concurrency: 4

gates:
  editorial_pass_score: 82
  min_candidate_quality: 60

dashboard_host: 127.0.0.1
dashboard_port: 8765
"""

SOURCE_DOMAINS = """# Verification-source allow list (§11.3).
# Only these hosts are fetched for verification. Add the brokerages that
# actually list in your target regions; keep the list small and deliberate.

brokerages:
  - legacysir.com
  - landvest.com
  - bhhsnewengland.com
  - kw.com
  - remax.com
  - cbrealty.com
  - betterhomesmaine.com
  - swanagency.com
  - tworiversmaine.com

portals:
  - realtor.com
  - redfin.com

blocked: []
"""

COASTAL_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "slug": "coastal-under-1m",
    "name": "Five Stunning Maine Coastal Homes Under $1M",
    "enabled": True,
    "editorial_promise": "Five Maine coastal homes under a million dollars, verified this morning",
    "schedule": "0 5 * * 1",
    "result_count": 5,
    "alternates_count": 1,
    "target_runtime_seconds": 420,
    "target_words": 980,
    "word_range": [850, 1100],
    "max_segment_words": 140,
    "fallback_template": "oceanfront-now",
    "min_candidate_quality": 60,
    "title_patterns": [
        "{count} Stunning Maine Coastal Homes Under {ceiling}",
        "{count} Maine Coastal Homes You Can Actually Buy",
    ],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/me/waterfront/"],
        "max_pages": 4,
        "fallback_adapters": ["fixture"],
    },
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
    "scoring": {
        "theme_fit": 25,
        "visual_strength": 25,
        "story_value": 15,
        "evidence_quality": 15,
        "freshness": 10,
        "geographic_diversity": 5,
        "asset_completeness": 5,
    },
    "exclusions": {
        "statuses": ["pending", "contingent", "sold", "off_market"],
        "featured_within_days": 120,
        "land_only": True,
        "auction": True,
    },
    "publishing": {
        "playlist": "Maine Coastal Homes",
        "privacy": "private",
        "earliest_publish_hour": 17,
    },
    "design": {"palette": "coastal", "map_style": "chart", "music_family": "warm_acoustic"},
}


def _section(name: str, **changes: Any) -> dict[str, Any]:
    """Copy one section of the coastal template with a few values changed."""

    base = cast(dict[str, Any], COASTAL_TEMPLATE[name])
    return {**base, **changes}


OCEANFRONT_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "oceanfront-now",
    "name": "Five Maine Oceanfront Homes You Can Buy Right Now",
    "editorial_promise": "Direct oceanfront only — no coastal-ZIP-code impostors",
    "schedule": "0 5 * * 2",
    "fallback_template": None,
    "title_patterns": ["{count} Maine Oceanfront Homes You Can Buy Right Now"],
    "hard_filters": _section("hard_filters", price_max=2500000),
    "theme_rules": {"any_of": ["owned_ocean_frontage", "owned_frontage_measured"]},
    "publishing": _section("publishing", playlist="Maine Oceanfront"),
}

LAKE_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "lake-under-750k",
    "name": "Five Beautiful Maine Lake Homes Under $750,000",
    "editorial_promise": "Maine lake houses and camps under $750,000, waterfront verified",
    "schedule": "0 5 * * 2",
    "fallback_template": "camps-under-500k",
    "title_patterns": ["{count} Beautiful Maine Lake Homes Under {ceiling}"],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/me/lakefront/"],
        "max_pages": 4,
        "fallback_adapters": ["fixture"],
    },
    "hard_filters": _section("hard_filters", price_max=749999),
    "theme_rules": {"any_of": ["lake_frontage", "named_lake", "deeded_beach_access"]},
    "publishing": _section("publishing", playlist="Maine Lake Homes"),
    "design": {"palette": "mountain", "map_style": "chart", "music_family": "warm_acoustic"},
}

SKI_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "ski-mountain",
    "name": "Five Maine Mountain Homes Near the Ski Slopes",
    "editorial_promise": "Homes within reach of Sugarloaf, Sunday River and Saddleback",
    "schedule": "0 5 * * 4",
    "fallback_template": "unique-maine",
    "title_patterns": ["{count} Maine Mountain Homes Near the Ski Slopes"],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/carrabassett-valley-me/"],
        "max_pages": 3,
        "fallback_adapters": ["fixture"],
    },
    "hard_filters": _section("hard_filters", price_max=1500000),
    "theme_rules": {"any_of": ["ski_area_proximity"]},
    "publishing": _section("publishing", playlist="Maine Mountain Homes"),
    "design": {"palette": "mountain", "map_style": "topographic", "music_family": "warm_acoustic"},
}

HISTORIC_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "historic-maine",
    "name": "Five Historic Maine Homes Full of Original Character",
    "editorial_promise": "Pre-1900 Maine houses with documented history and intact detail",
    "schedule": "0 5 * * 3",
    "fallback_template": "unique-maine",
    "max_history_beats": 3,
    "title_patterns": ["{count} Historic Maine Homes Full of Original Character"],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/me/historic/"],
        "max_pages": 4,
        "fallback_adapters": ["fixture"],
    },
    "hard_filters": _section("hard_filters", price_max=1200000, max_year_built=1899),
    "theme_rules": {"any_of": ["pre_1900", "historic_character"]},
    "publishing": _section("publishing", playlist="Historic Maine Homes"),
    "design": {"palette": "historic", "map_style": "chart", "music_family": "chamber"},
}

CAMPS_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "camps-under-500k",
    "name": "Five Maine Hunting and Fishing Camps Under $500,000",
    "editorial_promise": "Camps with acreage, water and real access — seasonal use stated plainly",
    "schedule": "0 5 * * 2",
    "fallback_template": None,
    "title_patterns": ["{count} Maine Hunting and Fishing Camps Under {ceiling}"],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/me/camp/"],
        "max_pages": 3,
        "fallback_adapters": ["fixture"],
    },
    "hard_filters": _section(
        "hard_filters",
        price_max=499999,
        min_usable_images=6,
        property_types=["single_family", "seasonal_residence"],
    ),
    "theme_rules": {"any_of": ["acreage", "sporting_access", "seasonal_camp", "lake_frontage"]},
    "publishing": _section("publishing", playlist="Maine Camps"),
    "design": {"palette": "mountain", "map_style": "topographic", "music_family": "warm_acoustic"},
}

UNIQUE_TEMPLATE: dict[str, Any] = {
    **COASTAL_TEMPLATE,
    "slug": "unique-maine",
    "name": "Five Wildly Unique Maine Homes on the Market",
    "editorial_promise": "Converted, unusual, and architect-designed Maine properties",
    "schedule": "0 5 * * 6",
    "fallback_template": None,
    "title_patterns": ["{count} Wildly Unique Maine Homes on the Market"],
    "discovery": {
        "adapter": "zillow",
        "search_urls": ["https://www.zillow.com/me/unique-homes/"],
        "max_pages": 4,
        "fallback_adapters": ["fixture"],
    },
    "hard_filters": _section("hard_filters", price_max=2000000),
    "theme_rules": {"any_of": ["unique_property", "island_location", "large_acreage"]},
    "publishing": _section("publishing", playlist="Unique Maine Properties"),
}

TEMPLATES = (
    COASTAL_TEMPLATE,
    OCEANFRONT_TEMPLATE,
    LAKE_TEMPLATE,
    SKI_TEMPLATE,
    HISTORIC_TEMPLATE,
    CAMPS_TEMPLATE,
    UNIQUE_TEMPLATE,
)


def write_starter_config(settings: Settings, force: bool = False) -> list[Path]:
    """Write settings example, templates, pronunciation and domain allow list."""

    created: list[Path] = []
    config_dir = Path("config")
    config_dir.mkdir(parents=True, exist_ok=True)
    templates_dir = Path(settings.templates_dir)
    templates_dir.mkdir(parents=True, exist_ok=True)

    def write(path: Path, content: str) -> None:
        if path.exists() and not force:
            return
        path.write_text(content, encoding="utf-8")
        created.append(path)

    write(config_dir / "settings.example.yaml", SETTINGS_EXAMPLE)
    write(Path(settings.source_domains_path), SOURCE_DOMAINS)

    for payload in TEMPLATES:
        write(
            templates_dir / f"{payload['slug']}.yaml",
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        )

    pronunciation_path = Path(settings.pronunciation_path)
    if force or not pronunciation_path.exists():
        PronunciationLexicon().save(pronunciation_path)
        created.append(pronunciation_path)

    return created
