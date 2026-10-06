\
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .policy import ClaimType, PRECEDENCE, REQUIREMENTS


_INDEPENDENT_TYPES = {
    "readback",
    "absence_check",
    "hash",
    "artifact_stat",
    "terminal_receipt",
    "test_receipt",
    "runtime_probe",
    "current_law",
    "latest_cutover",
    "active_implementation",
    "git_diff",
}


def check_completion_evidence(
    claim_type: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        parsed_type = ClaimType(claim_type)
    except ValueError as exc:
        raise ValueError(f"unknown claim_type: {claim_type}") from exc

    valid = [
        item for item in evidence
        if item.get("valid", True) is True and str(item.get("type", "")).strip()
    ]
    present = {str(item["type"]) for item in valid}
    groups = REQUIREMENTS[parsed_type]

    matched_groups = [sorted(group) for group in groups if group & present]
    independent = [item for item in valid if item.get("type") in _INDEPENDENT_TYPES]

    if matched_groups and independent:
        verdict = "PASS"
        reason = "completion claim has independent evidence satisfying its gate"
        missing: list[list[str]] = []
    elif matched_groups:
        verdict = "UNKNOWN"
        reason = "matching evidence exists but is not independently verifiable"
        missing = []
    else:
        verdict = "FAIL"
        reason = "completion claim lacks required evidence"
        missing = [sorted(group) for group in groups]

    return {
        "verdict": verdict,
        "reason": reason,
        "claim_type": parsed_type.value,
        "present_evidence_types": sorted(present),
        "matched_requirement_groups": matched_groups,
        "missing_requirement_groups": missing,
        "independent_evidence_count": len(independent),
        "rule": "CLAIM != FACT; completion requires readback/receipt/runtime proof",
    }


def resolve_precedence(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank conflicting statements by Reality-first source precedence."""
    ranked: list[dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        source_class = str(item.get("source_class", "model_report"))
        score = PRECEDENCE.get(source_class, 0)
        ranked.append({**item, "_precedence_score": score, "_input_order": idx})

    ranked.sort(
        key=lambda item: (
            item["_precedence_score"],
            str(item.get("timestamp", "")),
            -item["_input_order"],
        ),
        reverse=True,
    )
    winner = ranked[0] if ranked else None
    conflicts = []
    if winner is not None:
        winner_value = winner.get("value")
        conflicts = [
            item for item in ranked[1:]
            if item.get("value") != winner_value
        ]

    return {
        "winner": winner,
        "conflicts": conflicts,
        "precedence": [
            "live_reality",
            "current_law",
            "latest_cutover",
            "active_implementation_test",
            "history",
            "model_report",
        ],
        "rule": "fresh Reality > CURRENT law > latest cutover > active implementation/tests > history > model report",
    }


def build_verification_plan(
    claim: str,
    claim_type: str,
    available_tools: list[str] | None = None,
) -> dict[str, Any]:
    tools = set(available_tools or [])
    try:
        parsed_type = ClaimType(claim_type)
    except ValueError as exc:
        raise ValueError(f"unknown claim_type: {claim_type}") from exc

    suggestions: dict[ClaimType, list[tuple[str, tuple[str, ...]]]] = {
        ClaimType.FILE_WRITTEN: [
            ("Read the exact target back and compare expected content.", ("read_file", "file.read_text", "machine_query")),
            ("Check artifact size/hash when exact bytes matter.", ("file.stat", "hash", "machine_query")),
        ],
        ClaimType.FILE_DELETED: [
            ("Probe exact path absence after mutation.", ("file.exists", "machine_query")),
        ],
        ClaimType.BUILD_COMPLETED: [
            ("Read terminal exit receipt.", ("terminal_read",)),
            ("Check expected build artifact exists and is non-placeholder.", ("file.stat", "machine_query")),
        ],
        ClaimType.TESTS_PASSED: [
            ("Read exact test terminal receipt and summary.", ("terminal_read",)),
        ],
        ClaimType.DEPLOYMENT_COMPLETED: [
            ("Probe live runtime/health after deployment.", ("runtime_probe", "livingos_reality", "machine_query")),
            ("Keep the deployment receipt.", ("terminal_read",)),
        ],
        ClaimType.RUNTIME_CHANGED: [
            ("Inspect fresh runtime/process/port state.", ("runtime_probe", "livingos_reality", "machine_query")),
        ],
        ClaimType.CURRENT_STATE: [
            ("Prefer fresh live Reality; then CURRENT law; never infer from historical file existence.", ("livingos_reality", "machine_query")),
        ],
        ClaimType.OBSERVATION: [
            ("Collect one direct observation from the authoritative surface.", ("machine_query", "read_file", "runtime_probe")),
        ],
        ClaimType.GENERIC_COMPLETION: [
            ("Identify the side effect, then read it back or obtain its terminal receipt.", ("terminal_read", "machine_query")),
        ],
        ClaimType.UNKNOWN: [
            ("Do not accept the claim yet; classify the side effect and collect direct evidence.", ()),
        ],
    }

    steps = []
    for instruction, preferred in suggestions[parsed_type]:
        matching = [tool for tool in preferred if tool in tools]
        steps.append({
            "instruction": instruction,
            "preferred_tools": list(preferred),
            "available_matches": matching,
        })

    return {
        "claim": claim,
        "claim_type": parsed_type.value,
        "steps": steps,
        "stop_condition": "A completion claim is accepted only after independent Reality evidence satisfies its gate.",
    }
