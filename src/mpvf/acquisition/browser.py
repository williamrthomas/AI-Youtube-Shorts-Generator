"""Browser session management (§9.1, §16).

Playwright is an optional dependency: importing MPVF, running tests and
parsing fixtures must all work without it. A dedicated persistent profile is
used for acquisition so the operator's everyday browser profile is never
touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mpvf.config.settings import AcquisitionSettings
from mpvf.observability.logging import get_logger

logger = get_logger("acquisition.browser")


class BrowserUnavailable(RuntimeError):
    """Playwright or its browser runtime is not installed."""


class BrowserSession:
    """Lazily-started persistent Chromium context."""

    def __init__(
        self,
        settings: AcquisitionSettings | None = None,
        profile_dir: Path | str | None = None,
        headless: bool = True,
    ) -> None:
        self.settings = settings or AcquisitionSettings()
        self.profile_dir = Path(profile_dir or self.settings.browser_profile_dir)
        self.headless = headless
        self._playwright: Any = None
        self._context: Any = None

    def ensure_available(self) -> None:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise BrowserUnavailable(
                "playwright is not installed; install the 'acquisition' extra and run "
                "'playwright install chromium'"
            ) from exc

    def start(self) -> Any:
        if self._context is not None:
            return self._context
        self.ensure_available()
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                user_agent=self.settings.user_agent,
                viewport={"width": 1440, "height": 1000},
            )
        except Exception as exc:  # pragma: no cover - runtime missing
            self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailable(f"could not launch chromium: {exc}") from exc
        self._context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        return self._context

    def fetch_html(self, url: str, wait_for: str | None = None) -> str:
        context = self.start()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=self.settings.navigation_timeout_ms)
            page.wait_for_timeout(1200)
            return page.content()
        finally:
            page.close()

    def screenshot(self, url: str, destination: Path | str) -> Path:
        context = self.start()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination), full_page=False)
            return destination
        finally:
            page.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # pragma: no cover
                logger.debug("browser context close failed", exc_info=True)
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                logger.debug("playwright stop failed", exc_info=True)
            self._playwright = None

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ReplaySession:
    """A ``BrowserSession`` stand-in that serves saved HTML.

    Used by fixture tests and by ``mpvf run --replay`` so a full pipeline can
    be exercised offline against captured pages.
    """

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def ensure_available(self) -> None:
        return None

    def start(self) -> Any:  # pragma: no cover - never used
        return None

    def fetch_html(self, url: str, wait_for: str | None = None) -> str:
        self.requested.append(url)
        if url in self.pages:
            return self.pages[url]
        for key, html in self.pages.items():
            if url.startswith(key) or key.startswith(url):
                return html
        raise BrowserUnavailable(f"no replay fixture for {url}")

    def screenshot(self, url: str, destination: Path | str) -> Path:  # pragma: no cover
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def close(self) -> None:
        return None
