\
from __future__ import annotations

import json
from typing import Any


_PRIORITY = {
    "runtime_probe": 100,
    "terminal_receipt": 95,
    "test_receipt": 94,
    "readback": 90,
    "absence_check": 90,
    "hash": 85,
    "artifact_stat": 80,
    "current_law": 75,
    "latest_cutover": 70,
    "active_implementation": 60,
    "git_diff": 55,
    "history": 20,
    "model_report": 10,
}


def compact_evidence(
    evidence: list[dict[str, Any]],
    max_items: int = 8,
) -> dict[str, Any]:
    if max_items < 1:
        raise ValueError("max_items must be >= 1")

    deduped: dict[str, dict[str, Any]] = {}
    for item in evidence:
        key = (
            str(item.get("ref") or "")
            or str(item.get("path") or "")
            or json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
        current = deduped.get(key)
        if current is None or _PRIORITY.get(str(item.get("type")), 0) > _PRIORITY.get(str(current.get("type")), 0):
            deduped[key] = dict(item)

    ranked = sorted(
        deduped.values(),
        key=lambda item: (
            _PRIORITY.get(str(item.get("type")), 0),
            1 if item.get("valid", True) else 0,
            str(item.get("timestamp", "")),
        ),
        reverse=True,
    )

    selected = ranked[:max_items]
    return {
        "selected": selected,
        "dropped_count": max(0, len(ranked) - len(selected)),
        "rule": "raw evidence stays authoritative; compaction is derived and must keep source refs",
    }


def record_loss(
    claim: str,
    why_wrong: str,
    corrected_form: str,
    evidence_refs: list[str],
    prevention_rule: str,
) -> dict[str, Any]:
    if not claim.strip() or not why_wrong.strip() or not corrected_form.strip():
        raise ValueError("claim, why_wrong, and corrected_form are required")
    if not prevention_rule.strip():
        raise ValueError("prevention_rule is required")
    return {
        "claim": claim,
        "why_wrong": why_wrong,
        "corrected_form": corrected_form,
        "evidence": evidence_refs,
        "prevention_rule": prevention_rule,
    }
