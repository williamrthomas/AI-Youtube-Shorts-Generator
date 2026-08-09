"""Stage framework: registration, idempotency and rerun semantics (§6.1).

A stage declares the run state it produces, the artifact it writes, and the
failure state it moves to. Reruns with unchanged inputs reuse cached output;
a changed input hash produces an explicitly versioned replacement.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mpvf.pipeline.context import RunContext
from mpvf.pipeline.state import RunState


class StageError(RuntimeError):
    """A stage failure carrying the state the run should move to."""

    def __init__(
        self,
        code: str,
        message: str,
        failure_state: RunState = RunState.MANUAL_HOLD,
        detail: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.failure_state = failure_state
        self.detail = detail or {}
        self.artifact_path = artifact_path

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "failure_state": str(self.failure_state),
            "detail": self.detail,
            "artifact_path": self.artifact_path,
        }


class SkipEpisode(RuntimeError):
    """A quality gate decided that publishing nothing is the right output (§2.2)."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class StageResult:
    name: str
    state: RunState
    input_hash: str
    output_path: str | None
    cached: bool
    duration_seconds: float
    payload: Any = None


@dataclass(frozen=True)
class StageSpec:
    name: str
    order: int
    produces: RunState
    failure_state: RunState
    run: Callable[[RunContext], Any]
    describe: str = ""


class StageRegistry:
    """Ordered registry of pipeline stages."""

    def __init__(self) -> None:
        self._stages: dict[str, StageSpec] = {}

    def register(
        self,
        name: str,
        *,
        order: int,
        produces: RunState,
        failure_state: RunState,
        describe: str = "",
    ) -> Callable[[Callable[[RunContext], Any]], Callable[[RunContext], Any]]:
        def decorator(func: Callable[[RunContext], Any]) -> Callable[[RunContext], Any]:
            summary = describe
            if not summary and func.__doc__:
                lines = func.__doc__.strip().splitlines()
                summary = lines[0].strip() if lines else ""
            self._stages[name] = StageSpec(
                name=name,
                order=order,
                produces=produces,
                failure_state=failure_state,
                run=func,
                describe=summary,
            )
            return func

        return decorator

    def ordered(self) -> list[StageSpec]:
        return sorted(self._stages.values(), key=lambda spec: spec.order)

    def names(self) -> list[str]:
        return [spec.name for spec in self.ordered()]

    def get(self, name: str) -> StageSpec:
        if name not in self._stages:
            raise KeyError(f"unknown stage: {name}. known: {', '.join(self.names())}")
        return self._stages[name]

    def slice(self, from_stage: str | None, to_stage: str | None) -> list[StageSpec]:
        stages = self.ordered()
        start = 0
        end = len(stages)
        if from_stage:
            start = stages.index(self.get(from_stage))
        if to_stage:
            end = stages.index(self.get(to_stage)) + 1
        if start > end:
            raise ValueError("--from-stage occurs after --to-stage")
        return stages[start:end]


registry = StageRegistry()


def timed(func: Callable[[RunContext], Any], context: RunContext) -> tuple[Any, float]:
    started = time.perf_counter()
    payload = func(context)
    return payload, round(time.perf_counter() - started, 3)
