"""OpenAI-compatible chat provider (§12.2).

Both hosted back-ends we use speak the OpenAI chat-completions dialect:

- **Cloudflare Workers AI** at ``/accounts/{id}/ai/v1/chat/completions``
- **OpenRouter** at ``https://openrouter.ai/api/v1/chat/completions``

Sharing one transport means one place to get retries, schema validation and
the §12.3 generation record right, and one place to fix if a vendor changes a
field name. Base URLs, model ids and header sets are configuration, never
hard-coded logic.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from mpvf.generation.provider import GenerationError, GenerationRecord, Message
from mpvf.observability.logging import get_logger

logger = get_logger("generation.openai_compatible")

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def json_schema_payload(schema: type[BaseModel], strict: bool = True) -> dict[str, Any]:
    """Build the ``response_format`` block for schema-constrained output.

    Some hosted models ignore an unknown ``strict`` flag and some reject
    ``additionalProperties`` being absent, so we set both explicitly rather
    than relying on a provider default.
    """

    json_schema = schema.model_json_schema()
    json_schema.setdefault("additionalProperties", False)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": strict,
            "schema": json_schema,
        },
    }


class OpenAICompatibleProvider:
    """Chat provider for any OpenAI-compatible endpoint."""

    name = "openai_compatible"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None,
        *,
        timeout: int = 180,
        max_attempts: int = 3,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        supports_json_schema: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.supports_json_schema = supports_json_schema
        self._client = client

    # -- transport --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def available(self) -> bool:
        """A provider with no credential is unusable; do not probe the network.

        Availability is checked without spending a request so that
        ``mpvf doctor`` stays fast and free. A wrong key surfaces on the first
        real call with the API's own error message.
        """

        return bool(self.api_key)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- generation -------------------------------------------------------
    def generate_structured(
        self,
        task: str,
        messages: list[Message],
        schema: type[BaseModel],
        temperature: float = 0.4,
    ) -> tuple[BaseModel, GenerationRecord]:
        record = GenerationRecord(
            task=task,
            provider=self.name,
            model=self.model,
            prompt_id=task,
            prompt_version=1,
            schema_version=schema.__name__,
            temperature=temperature,
            input_hash=_hash_messages(messages),
            options={"base_url": self.base_url},
        )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.as_dict() for message in messages],
            "temperature": temperature,
            **self.extra_body,
        }
        if self.supports_json_schema:
            payload["response_format"] = json_schema_payload(schema)

        conversation = list(messages)
        last_error = ""
        started = time.perf_counter()

        for attempt in range(1, self.max_attempts + 1):
            record.attempts = attempt
            payload["messages"] = [message.as_dict() for message in conversation]
            try:
                content, usage = self._request(payload)
            except _RetryableAPIError as exc:
                last_error = str(exc)
                if attempt < self.max_attempts:
                    time.sleep(min(2**attempt, 20))
                continue
            except _FatalAPIError as exc:
                record.error = str(exc)
                record.duration_seconds = round(time.perf_counter() - started, 3)
                raise GenerationError(task, str(exc)) from exc

            record.raw_output = content
            record.options["usage"] = usage
            try:
                validated = schema.model_validate_json(_strip_code_fence(content))
            except ValidationError as exc:
                last_error = f"schema validation failed: {exc.error_count()} errors"
                conversation = [
                    *conversation,
                    Message(role="assistant", content=content[:4000]),
                    Message(
                        role="user",
                        content=(
                            "That output did not satisfy the schema. Return only JSON "
                            f"matching it, with no prose or code fence. Errors: {exc.errors()[:5]}"
                        ),
                    ),
                ]
                continue

            record.duration_seconds = round(time.perf_counter() - started, 3)
            return validated, record

        record.duration_seconds = round(time.perf_counter() - started, 3)
        record.error = last_error
        raise GenerationError(task, last_error or "no response", record.raw_output)

    def _request(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._http().post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise _RetryableAPIError(f"transport error: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableAPIError(
                f"{response.status_code} from {self.name}: {_error_detail(response)}"
            )
        if response.status_code >= 400:
            raise _FatalAPIError(
                f"{response.status_code} from {self.name}: {_error_detail(response)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise _RetryableAPIError(f"non-JSON response from {self.name}") from exc

        content = _extract_content(body)
        if content is None:
            raise _RetryableAPIError(
                f"{self.name} returned no message content: {json.dumps(body)[:300]}"
            )
        return content, body.get("usage") or {}


class _RetryableAPIError(RuntimeError):
    pass


class _FatalAPIError(RuntimeError):
    pass


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(body.get("errors"), list) and body["errors"]:
            return json.dumps(body["errors"][:2])[:300]
    return json.dumps(body)[:300]


def _extract_content(body: dict[str, Any]) -> str | None:
    """Pull the assistant text out of a chat-completions response.

    Tolerates the two shapes seen in practice: a plain string ``content``, and
    a list of content parts.
    """

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text", None}
        ]
        joined = "".join(parts)
        return joined or None
    return None


def _strip_code_fence(text: str) -> str:
    """Some models wrap JSON in ```json fences despite a schema constraint."""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()


def _hash_messages(messages: list[Message]) -> str:
    import hashlib

    payload = json.dumps([message.as_dict() for message in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
