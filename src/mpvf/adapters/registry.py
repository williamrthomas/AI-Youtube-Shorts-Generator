"""Adapter registry (FR-016).

Adapters are looked up by name so a template can name its discovery source and
a future Realtor.com / Redfin / saved-search adapter drops in without touching
the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mpvf.adapters.brokerage import BrokerageAdapter, SourceDomains
from mpvf.adapters.fixture import FixtureAdapter
from mpvf.adapters.zillow.adapter import ZillowAdapter
from mpvf.adapters.zillow.parser import parse_listing_html
from mpvf.config.settings import Settings

AdapterFactory = Callable[[Settings, dict[str, Any]], Any]

_DISCOVERY: dict[str, AdapterFactory] = {}
_VERIFICATION: dict[str, AdapterFactory] = {}


def register_discovery(name: str, factory: AdapterFactory) -> None:
    _DISCOVERY[name] = factory


def register_verification(name: str, factory: AdapterFactory) -> None:
    _VERIFICATION[name] = factory


def discovery_names() -> list[str]:
    return sorted(_DISCOVERY)


def verification_names() -> list[str]:
    return sorted(_VERIFICATION)


def get_discovery_adapter(name: str, settings: Settings, **options: Any) -> Any:
    if name not in _DISCOVERY:
        raise KeyError(f"unknown discovery adapter '{name}'. known: {', '.join(discovery_names())}")
    return _DISCOVERY[name](settings, options)


def get_verification_adapter(name: str, settings: Settings, **options: Any) -> Any:
    if name not in _VERIFICATION:
        raise KeyError(
            f"unknown verification adapter '{name}'. known: {', '.join(verification_names())}"
        )
    return _VERIFICATION[name](settings, options)


def _zillow(settings: Settings, options: dict[str, Any]) -> ZillowAdapter:
    return ZillowAdapter(
        settings=settings.acquisition,
        debug_dir=options.get("debug_dir"),
        session=options.get("session"),
    )


def _fixture(settings: Settings, options: dict[str, Any]) -> FixtureAdapter:
    directory = options.get("directory") or settings.data_dir / "fixtures"
    return FixtureAdapter(
        Path(directory), html_parser=options.get("html_parser", parse_listing_html)
    )


def _brokerage(settings: Settings, options: dict[str, Any]) -> BrokerageAdapter:
    domains = options.get("domains") or SourceDomains.load(settings.source_domains_path)
    return BrokerageAdapter(
        domains=domains, settings=settings.acquisition, client=options.get("client")
    )


register_discovery("zillow", _zillow)
register_discovery("fixture", _fixture)
register_verification("brokerage", _brokerage)
register_verification("fixture", _fixture)
