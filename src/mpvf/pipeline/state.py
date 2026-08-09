"""Run state machine (§6.1).

Transitions are explicit and validated. A stage may always move to its own
failure state or to ``manual_hold``; there are no silent jumps.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    SCHEDULED = "scheduled"
    DISCOVERING = "discovering"
    NORMALIZED = "normalized"
    VERIFYING = "verifying"
    SELECTING = "selecting"
    ACQUIRING_ASSETS = "acquiring_assets"
    RESEARCHING = "researching"
    SCRIPTING = "scripting"
    AUDIO_READY = "audio_ready"
    RENDERING = "rendering"
    QA_PENDING = "qa_pending"
    QA_PASSED = "qa_passed"
    UPLOADED = "uploaded"
    SCHEDULED_FOR_PUBLISH = "scheduled_for_publish"
    PUBLISHED = "published"
    ARCHIVED = "archived"

    # Failure / hold states
    DISCOVERY_FAILED = "discovery_failed"
    VERIFICATION_FAILED = "verification_failed"
    INSUFFICIENT_CANDIDATES = "insufficient_candidates"
    RESEARCH_FAILED = "research_failed"
    SCRIPT_FAILED = "script_failed"
    RENDER_FAILED = "render_failed"
    QA_FAILED = "qa_failed"
    UPLOAD_FAILED = "upload_failed"
    MANUAL_HOLD = "manual_hold"
    SKIPPED = "skipped"


FAILURE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.DISCOVERY_FAILED,
        RunState.VERIFICATION_FAILED,
        RunState.INSUFFICIENT_CANDIDATES,
        RunState.RESEARCH_FAILED,
        RunState.SCRIPT_FAILED,
        RunState.RENDER_FAILED,
        RunState.QA_FAILED,
        RunState.UPLOAD_FAILED,
    }
)

TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.ARCHIVED, RunState.SKIPPED, RunState.MANUAL_HOLD} | set(FAILURE_STATES)
)

HAPPY_PATH: tuple[RunState, ...] = (
    RunState.SCHEDULED,
    RunState.DISCOVERING,
    RunState.NORMALIZED,
    RunState.VERIFYING,
    RunState.SELECTING,
    RunState.ACQUIRING_ASSETS,
    RunState.RESEARCHING,
    RunState.SCRIPTING,
    RunState.AUDIO_READY,
    RunState.RENDERING,
    RunState.QA_PENDING,
    RunState.QA_PASSED,
    RunState.UPLOADED,
    RunState.SCHEDULED_FOR_PUBLISH,
    RunState.PUBLISHED,
    RunState.ARCHIVED,
)

_FORWARD: dict[RunState, set[RunState]] = {
    state: {HAPPY_PATH[index + 1]} for index, state in enumerate(HAPPY_PATH[:-1])
}
_FORWARD[RunState.ARCHIVED] = set()

# Stage-specific failures reachable from the corresponding working state.
_FAILURE_EDGES: dict[RunState, set[RunState]] = {
    RunState.DISCOVERING: {RunState.DISCOVERY_FAILED},
    RunState.NORMALIZED: {RunState.INSUFFICIENT_CANDIDATES},
    RunState.VERIFYING: {RunState.VERIFICATION_FAILED},
    RunState.SELECTING: {RunState.INSUFFICIENT_CANDIDATES},
    RunState.ACQUIRING_ASSETS: {RunState.INSUFFICIENT_CANDIDATES},
    RunState.RESEARCHING: {RunState.RESEARCH_FAILED},
    RunState.SCRIPTING: {RunState.SCRIPT_FAILED},
    RunState.AUDIO_READY: {RunState.RENDER_FAILED},
    RunState.RENDERING: {RunState.RENDER_FAILED},
    RunState.QA_PENDING: {RunState.QA_FAILED},
    RunState.QA_PASSED: {RunState.UPLOAD_FAILED},
    RunState.UPLOADED: {RunState.UPLOAD_FAILED},
    RunState.SCHEDULED_FOR_PUBLISH: {RunState.UPLOAD_FAILED},
}

# Recovery: a failed run may be retried back into the state that failed, and
# any non-terminal state may be put on hold or skipped by an operator.
_RECOVERY_EDGES: dict[RunState, set[RunState]] = {
    RunState.DISCOVERY_FAILED: {RunState.DISCOVERING, RunState.SKIPPED},
    RunState.VERIFICATION_FAILED: {RunState.VERIFYING, RunState.SKIPPED},
    RunState.INSUFFICIENT_CANDIDATES: {RunState.DISCOVERING, RunState.SELECTING, RunState.SKIPPED},
    RunState.RESEARCH_FAILED: {RunState.RESEARCHING, RunState.SKIPPED},
    RunState.SCRIPT_FAILED: {RunState.SCRIPTING, RunState.SKIPPED},
    RunState.RENDER_FAILED: {RunState.RENDERING, RunState.SKIPPED},
    RunState.QA_FAILED: {RunState.RENDERING, RunState.QA_PENDING, RunState.SKIPPED},
    RunState.UPLOAD_FAILED: {RunState.QA_PASSED, RunState.UPLOADED, RunState.SKIPPED},
    RunState.MANUAL_HOLD: set(HAPPY_PATH) | {RunState.SKIPPED},
    RunState.SKIPPED: {RunState.ARCHIVED},
}


class InvalidTransition(RuntimeError):
    def __init__(self, current: RunState, target: RunState) -> None:
        super().__init__(f"cannot move run from {current} to {target}")
        self.current = current
        self.target = target


def allowed_transitions(state: RunState) -> set[RunState]:
    allowed = set(_FORWARD.get(state, set()))
    allowed |= _FAILURE_EDGES.get(state, set())
    allowed |= _RECOVERY_EDGES.get(state, set())
    if state not in TERMINAL_STATES:
        allowed |= {RunState.MANUAL_HOLD, RunState.SKIPPED}
    allowed.discard(state)
    return allowed


def can_transition(current: RunState, target: RunState) -> bool:
    return target in allowed_transitions(current)


def assert_transition(current: RunState, target: RunState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def is_failure(state: RunState) -> bool:
    return state in FAILURE_STATES


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
