"""Generation-provider interface and implementations (§12.2).

Two rules hold everywhere: model output is always schema-validated before it is
used, and the raw output is always kept for debugging. Models never receive
credentials, cookies or unrelated user data.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from mpvf.observability.logging import get_logger

logger = get_logger("generation")


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class GenerationRecord:
    """Everything §12.3 requires us to store about one generation."""

    task: str
    provider: str
    model: str
    prompt_id: str
    prompt_version: int
    schema_version: str
    temperature: float
    input_hash: str
    raw_output: str = ""
    duration_seconds: float = 0.0
    attempts: int = 1
    error: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "provider": self.provider,
            "model": self.model,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "temperature": self.temperature,
            "input_hash": self.input_hash,
            "duration_seconds": self.duration_seconds,
            "attempts": self.attempts,
            "error": self.error,
            "options": self.options,
            "raw_output": self.raw_output[:20_000],
        }


class GenerationError(RuntimeError):
    def __init__(self, task: str, detail: str, raw_output: str = "") -> None:
        super().__init__(f"{task}: {detail}")
        self.task = task
        self.detail = detail
        self.raw_output = raw_output


@runtime_checkable
class GenerationProvider(Protocol):
    name: str
    model: str

    def available(self) -> bool: ...

    def generate_structured(
        self,
        task: str,
        messages: list[Message],
        schema: type[BaseModel],
        temperature: float = 0.4,
    ) -> tuple[BaseModel, GenerationRecord]: ...


class OllamaProvider:
    """Local Ollama with JSON-schema-constrained structured output (FR-081)."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://127.0.0.1:11434",
        timeout: int = 180,
        max_attempts: int = 2,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts

    def available(self) -> bool:
        try:
            import httpx

            response = httpx.get(f"{self.host}/api/tags", timeout=4.0)
            response.raise_for_status()
            models = {entry.get("name", "") for entry in response.json().get("models", [])}
        except Exception:  # noqa: BLE001 - availability probe must not raise
            return False
        base = self.model.split(":")[0]
        return any(name == self.model or name.startswith(base) for name in models)

    def generate_structured(
        self,
        task: str,
        messages: list[Message],
        schema: type[BaseModel],
        temperature: float = 0.4,
    ) -> tuple[BaseModel, GenerationRecord]:
        import httpx

        json_schema = schema.model_json_schema()
        record = GenerationRecord(
            task=task,
            provider=self.name,
            model=self.model,
            prompt_id=task,
            prompt_version=1,
            schema_version=str(json_schema.get("title", schema.__name__)),
            temperature=temperature,
            input_hash=_hash_messages(messages),
            options={"host": self.host},
        )

        last_error = ""
        started = time.perf_counter()
        for attempt in range(1, self.max_attempts + 1):
            record.attempts = attempt
            payload = {
                "model": self.model,
                "messages": [message.as_dict() for message in messages],
                "format": json_schema,
                "stream": False,
                "options": {"temperature": temperature},
            }
            try:
                response = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
                response.raise_for_status()
                content = response.json().get("message", {}).get("content", "")
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue

            record.raw_output = content
            try:
                validated = schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = f"schema validation failed: {exc.error_count()} errors"
                messages = [
                    *messages,
                    Message(role="assistant", content=content[:4000]),
                    Message(
                        role="user",
                        content=(
                            "That output did not satisfy the schema. Return only valid JSON "
                            f"matching the schema. Errors: {exc.errors()[:5]}"
                        ),
                    ),
                ]
                continue

            record.duration_seconds = round(time.perf_counter() - started, 3)
            return validated, record

        record.duration_seconds = round(time.perf_counter() - started, 3)
        record.error = last_error
        raise GenerationError(task, last_error, record.raw_output)


class DeterministicProvider:
    """Template-driven fallback that needs no model at all.

    This exists so the pipeline is runnable and testable without a model, and
    so a model outage degrades to a plainer episode rather than a failed run
    (§9.1: the provider is an interface, not a dependency).
    """

    name = "deterministic"

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.model = "deterministic-v1"
        self.handlers: dict[str, Any] = handlers or {}

    def available(self) -> bool:
        return True

    def register(self, task: str, handler: Any) -> None:
        self.handlers[task] = handler

    def generate_structured(
        self,
        task: str,
        messages: list[Message],
        schema: type[BaseModel],
        temperature: float = 0.0,
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
        )
        handler = self.handlers.get(task)
        if handler is None:
            raise GenerationError(task, "no deterministic handler registered for this task")
        payload = handler(messages)
        try:
            validated = schema.model_validate(payload)
        except ValidationError as exc:
            record.error = str(exc)
            raise GenerationError(
                task, f"deterministic handler produced invalid output: {exc}"
            ) from exc
        record.raw_output = validated.model_dump_json()
        return validated, record


class FallbackProvider:
    """Tries a primary provider, then falls back (FR-082)."""

    name = "fallback"

    def __init__(self, primary: GenerationProvider, secondary: GenerationProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        self.model = f"{primary.model}->{secondary.model}"

    def available(self) -> bool:
        return self.primary.available() or self.secondary.available()

    def generate_structured(
        self,
        task: str,
        messages: list[Message],
        schema: type[BaseModel],
        temperature: float = 0.4,
    ) -> tuple[BaseModel, GenerationRecord]:
        if self.primary.available():
            try:
                return self.primary.generate_structured(task, messages, schema, temperature)
            except GenerationError as exc:
                logger.warning(
                    "primary provider failed, falling back",
                    extra={"detail": {"task": task, "error": exc.detail}},
                )
        return self.secondary.generate_structured(task, messages, schema, temperature)


def _hash_messages(messages: list[Message]) -> str:
    import hashlib

    payload = json.dumps([message.as_dict() for message in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_provider(
    settings: Any, deterministic_handlers: dict[str, Any] | None = None
) -> GenerationProvider:
    """Construct the configured provider chain from settings."""

    deterministic = DeterministicProvider(deterministic_handlers)
    if settings.kind == "ollama":
        primary = OllamaProvider(
            model=settings.model, host=settings.host, timeout=settings.timeout_seconds
        )
        if settings.fallback == "deterministic":
            return FallbackProvider(primary, deterministic)
        return primary
    return deterministic
