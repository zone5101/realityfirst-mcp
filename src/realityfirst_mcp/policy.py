from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
import json
from typing import Any


class ClaimType(str, Enum):
    OBSERVATION = "observation"
    CURRENT_STATE = "current_state"
    FILE_WRITTEN = "file_written"
    FILE_DELETED = "file_deleted"
    BUILD_COMPLETED = "build_completed"
    TESTS_PASSED = "tests_passed"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    RUNTIME_CHANGED = "runtime_changed"
    GENERIC_COMPLETION = "generic_completion"
    UNKNOWN = "unknown"


class EvidenceType(str, Enum):
    READBACK = "readback"
    ABSENCE_CHECK = "absence_check"
    HASH = "hash"
    ARTIFACT_STAT = "artifact_stat"
    TERMINAL_RECEIPT = "terminal_receipt"
    TEST_RECEIPT = "test_receipt"
    RUNTIME_PROBE = "runtime_probe"
    CURRENT_LAW = "current_law"
    LATEST_CUTOVER = "latest_cutover"
    ACTIVE_IMPLEMENTATION = "active_implementation"
    GIT_DIFF = "git_diff"
    HISTORY = "history"
    MODEL_REPORT = "model_report"


PRECEDENCE = {
    "live_reality": 100,
    "current_law": 90,
    "latest_cutover": 80,
    "active_implementation_test": 70,
    "history": 20,
    "model_report": 10,
}


REQUIREMENTS: dict[ClaimType, tuple[set[str], ...]] = {
    ClaimType.OBSERVATION: ({"readback", "runtime_probe", "terminal_receipt", "current_law"},),
    ClaimType.CURRENT_STATE: ({"runtime_probe", "current_law"},),
    ClaimType.FILE_WRITTEN: ({"readback"}, {"hash", "artifact_stat"}),
    ClaimType.FILE_DELETED: ({"absence_check"},),
    ClaimType.BUILD_COMPLETED: ({"terminal_receipt"}, {"artifact_stat"}),
    ClaimType.TESTS_PASSED: ({"test_receipt", "terminal_receipt"},),
    ClaimType.DEPLOYMENT_COMPLETED: ({"runtime_probe"}, {"terminal_receipt"}),
    ClaimType.RUNTIME_CHANGED: ({"runtime_probe"},),
    ClaimType.GENERIC_COMPLETION: ({"terminal_receipt", "readback", "runtime_probe", "test_receipt"},),
    ClaimType.UNKNOWN: ({"readback", "runtime_probe", "terminal_receipt"},),
}


@dataclass(frozen=True)
class GateResult:
    verdict: str
    reason: str
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "missing": list(self.missing),
        }


def load_policy(name: str) -> dict[str, Any]:
    if name not in {"core", "completion", "precedence"}:
        raise ValueError(f"unknown policy: {name}")
    target = files("realityfirst_mcp.policies").joinpath(f"{name}.json")
    return json.loads(target.read_text(encoding="utf-8"))


def replay_gate(state: str, side_effect_state: str | None = None) -> GateResult:
    state_u = (state or "").upper()
    effect_u = (side_effect_state or "").upper()

    if "DO_NOT_REPLAY" in state_u:
        return GateResult("BLOCK", "terminal state forbids blind replay")
    if state_u in {"RUNNING", "SUBMITTED", "ADMITTED"}:
        return GateResult("BLOCK", "request is still in progress; resolve the same lineage first")
    if effect_u in {"UNKNOWN", "UNKNOWN_IN_PROGRESS", "POSSIBLE"}:
        return GateResult("BLOCK", "side effects are unresolved; reconcile before replay")
    if state_u in {"FAILED_NO_SIDE_EFFECT", "TERMINAL_FAILURE_NO_SIDE_EFFECT"}:
        return GateResult("ALLOW_WITH_NEW_DECISION", "terminal failure explicitly proves no side effect")
    if state_u in {"COMPLETED", "PASS"}:
        return GateResult("BLOCK", "operation already completed; do not duplicate it")
    return GateResult("RECONCILE", "state does not prove replay safety")
