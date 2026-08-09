"""Hosted inference back-ends: Cloudflare Workers AI and OpenRouter.

Credentials come from the environment or the secrets directory — never from a
config file, never from the database, and never into a prompt (§12.2, §16).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mpvf.generation.openai_compatible import OpenAICompatibleProvider

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class CredentialMissing(RuntimeError):
    """A hosted provider was selected without the credential it needs."""


@dataclass
class CloudCredentials:
    """Resolved secrets for the hosted back-ends."""

    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    openrouter_api_key: str | None = None

    @classmethod
    def load(cls, secrets_dir: Path | str | None = None) -> CloudCredentials:
        """Read from ``MPVF_*``/vendor env vars, then the secrets directory.

        A file is used only if the corresponding variable is unset, so a
        deployment can inject secrets without writing them to disk.
        """

        directory = Path(secrets_dir) if secrets_dir else None

        def resolve(env_names: tuple[str, ...], filename: str) -> str | None:
            for name in env_names:
                value = os.environ.get(name)
                if value:
                    return value.strip()
            if directory is not None:
                path = directory / filename
                if path.exists():
                    return path.read_text(encoding="utf-8").strip() or None
            return None

        return cls(
            cloudflare_account_id=resolve(
                ("MPVF_CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID"),
                "cloudflare-account-id",
            ),
            cloudflare_api_token=resolve(
                ("MPVF_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN"),
                "cloudflare-api-token",
            ),
            openrouter_api_key=resolve(
                ("MPVF_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
                "openrouter-api-key",
            ),
        )

    def state(self) -> dict[str, bool]:
        """What is present, never what it is."""

        return {
            "cloudflare_account_id": bool(self.cloudflare_account_id),
            "cloudflare_api_token": bool(self.cloudflare_api_token),
            "openrouter_api_key": bool(self.openrouter_api_key),
        }


def workers_ai_base_url(account_id: str, api_base: str = CLOUDFLARE_API_BASE) -> str:
    """Workers AI exposes an OpenAI-compatible surface under the account."""

    return f"{api_base.rstrip('/')}/accounts/{account_id}/ai/v1"


def workers_ai_run_url(account_id: str, model: str, api_base: str = CLOUDFLARE_API_BASE) -> str:
    """The native ``/ai/run`` endpoint, used for the non-chat modalities."""

    return f"{api_base.rstrip('/')}/accounts/{account_id}/ai/run/{model}"


class WorkersAIProvider(OpenAICompatibleProvider):
    """Cloudflare Workers AI through its OpenAI-compatible endpoint."""

    name = "cloudflare"

    def __init__(
        self,
        model: str,
        account_id: str,
        api_token: str,
        *,
        api_base: str = CLOUDFLARE_API_BASE,
        timeout: int = 180,
        max_attempts: int = 3,
        supports_json_schema: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        if not account_id or not api_token:
            raise CredentialMissing(
                "Workers AI needs MPVF_CLOUDFLARE_ACCOUNT_ID and MPVF_CLOUDFLARE_API_TOKEN"
            )
        super().__init__(
            model=model,
            base_url=workers_ai_base_url(account_id, api_base),
            api_key=api_token,
            timeout=timeout,
            max_attempts=max_attempts,
            supports_json_schema=supports_json_schema,
            client=client,
        )
        self.account_id = account_id
        self.api_base = api_base


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter, which fronts many vendors behind one OpenAI-shaped API."""

    name = "openrouter"

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        api_base: str = OPENROUTER_API_BASE,
        timeout: int = 180,
        max_attempts: int = 3,
        app_url: str = "https://github.com/mpvf",
        app_title: str = "Maine Property Video Factory",
        require_parameters: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise CredentialMissing("OpenRouter needs MPVF_OPENROUTER_API_KEY")

        extra_body: dict[str, Any] = {}
        if require_parameters:
            # Only route to upstreams that honour our schema constraint; without
            # this a request can silently land on a model that ignores it.
            extra_body["provider"] = {"require_parameters": True}

        super().__init__(
            model=model,
            base_url=api_base,
            api_key=api_key,
            timeout=timeout,
            max_attempts=max_attempts,
            # OpenRouter uses these for attribution in its dashboards.
            extra_headers={"HTTP-Referer": app_url, "X-Title": app_title},
            extra_body=extra_body,
            supports_json_schema=True,
            client=client,
        )
