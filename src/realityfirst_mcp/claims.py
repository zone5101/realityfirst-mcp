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


def classify_claim(claim: str, explicit_type: str | None = None) -> dict[str, Any]:
    if explicit_type:
        try:
            claim_type = ClaimType(explicit_type)
        except ValueError as exc:
            raise ValueError(f"unknown claim_type: {explicit_type}") from exc
        confidence = 1.0
        matched = ["explicit_type"]
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
