from __future__ import annotations

import re
from typing import Any

from .policy import ClaimType, REQUIREMENTS


_PATTERNS: list[tuple[ClaimType, tuple[str, ...]]] = [
    (ClaimType.FILE_WRITTEN, ("wrote", "written", "created file", "saved file", "materialized", "ghi file", "đã ghi")),
    (ClaimType.FILE_DELETED, ("deleted", "removed", "absent", "xóa", "xoá")),
    (ClaimType.TESTS_PASSED, ("tests pass", "test pass", "passed tests", "pytest pass", "tests passed")),
    (ClaimType.BUILD_COMPLETED, ("build pass", "build completed", "built bundle", "compiled", "build succeeded")),
    (ClaimType.DEPLOYMENT_COMPLETED, ("deployed", "deployment completed", "published", "released")),
    (ClaimType.RUNTIME_CHANGED, ("restarted", "runtime changed", "service started", "service stopped", "reloaded")),
    (ClaimType.CURRENT_STATE, ("currently", "current state", "is active", "is running", "hiện tại", "đang chạy")),
    (ClaimType.GENERIC_COMPLETION, ("completed", "finished", "done", "xong", "hoàn tất")),
]


CLAIM_TYPE_ALIASES: dict[str, ClaimType] = {
    "completion": ClaimType.GENERIC_COMPLETION,
    "complete": ClaimType.GENERIC_COMPLETION,
    "current": ClaimType.CURRENT_STATE,
    "state": ClaimType.CURRENT_STATE,
    "write": ClaimType.FILE_WRITTEN,
    "written": ClaimType.FILE_WRITTEN,
    "created": ClaimType.FILE_WRITTEN,
    "delete": ClaimType.FILE_DELETED,
    "deleted": ClaimType.FILE_DELETED,
    "build": ClaimType.BUILD_COMPLETED,
    "built": ClaimType.BUILD_COMPLETED,
    "test": ClaimType.TESTS_PASSED,
    "tests": ClaimType.TESTS_PASSED,
    "passed": ClaimType.TESTS_PASSED,
    "deploy": ClaimType.DEPLOYMENT_COMPLETED,
    "deployed": ClaimType.DEPLOYMENT_COMPLETED,
    "restart": ClaimType.RUNTIME_CHANGED,
    "restarted": ClaimType.RUNTIME_CHANGED,
    "observe": ClaimType.OBSERVATION,
    "observation": ClaimType.OBSERVATION,
    "unknown": ClaimType.UNKNOWN,
}


def resolve_claim_type(value: str) -> ClaimType | None:
    """Resolve a caller-supplied claim type with tolerant aliases; None if unrecognized."""
    raw = str(value or "").strip()
    try:
        return ClaimType(raw)
    except ValueError:
        pass
    key = raw.lower().replace("-", "_").replace(" ", "_")
    try:
        return ClaimType(key)
    except ValueError:
        pass
    return CLAIM_TYPE_ALIASES.get(key)


def classify_claim(claim: str, explicit_type: str | None = None) -> dict[str, Any]:
    resolved = resolve_claim_type(explicit_type) if explicit_type else None
    if resolved is not None:
        claim_type = resolved
        confidence = 1.0
        matched = ["explicit_type"]
        try:
            ClaimType(str(explicit_type).strip())
        except ValueError:
            matched = [f"explicit_type_alias:{explicit_type}"]
    else:
        lowered = re.sub(r"\s+", " ", claim.lower()).strip()
        claim_type = ClaimType.UNKNOWN
        matched: list[str] = []
        for candidate, needles in _PATTERNS:
            matched = [needle for needle in needles if needle in lowered]
            if matched:
                claim_type = candidate
                break
        confidence = 0.85 if matched else 0.25

    requirement_groups = [sorted(group) for group in REQUIREMENTS[claim_type]]
    return {
        "claim": claim,
        "claim_type": claim_type.value,
        "confidence": confidence,
        "matched": matched,
        "evidence_requirement_groups": requirement_groups,
        "invariant": "REALITY > CLAIM",
    }
