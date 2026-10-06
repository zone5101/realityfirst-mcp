from __future__ import annotations

from collections import defaultdict
import re
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


_HASH_LENGTHS = {
    "md5": 32,
    "sha1": 40,
    "sha224": 56,
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}


def _evidence_ref(item: dict[str, Any]) -> str:
    for key in ("ref", "path", "uri", "terminal_ref", "source_ref", "tool_ref"):
        value = str(item.get(key, "") or "").strip()
        if value:
            return value
    return ""


def validate_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence structure independently of a model-supplied type label."""
    evidence_type = str(item.get("type", "") or "").strip()
    reasons: list[str] = []

    if not evidence_type:
        reasons.append("missing evidence type")
    if item.get("valid", True) is not True:
        reasons.append("evidence explicitly marked invalid")

    ref = _evidence_ref(item)

    if evidence_type == "hash":
        algorithm = str(item.get("algorithm", "") or "").lower().replace("-", "")
        digest = str(item.get("digest", "") or "").strip().lower()
        expected_len = _HASH_LENGTHS.get(algorithm)
        if not ref:
            reasons.append("hash evidence requires a target ref/path")
        if expected_len is None:
            reasons.append("hash evidence requires a supported algorithm")
        elif not re.fullmatch(rf"[0-9a-f]{{{expected_len}}}", digest):
            reasons.append(f"{algorithm} digest must be {expected_len} hex characters")

    elif evidence_type == "artifact_stat":
        path = str(item.get("path", "") or "").strip() or ref
        size = item.get("size_bytes", item.get("size"))
        mtime = str(item.get("mtime", item.get("modified_at", "")) or "").strip()
        if not path:
            reasons.append("artifact_stat requires path/ref")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            reasons.append("artifact_stat requires non-negative integer size_bytes/size")
        if not mtime:
            reasons.append("artifact_stat requires mtime/modified_at")

    elif evidence_type == "readback":
        if not ref:
            reasons.append("readback requires source ref/path/uri")

    elif evidence_type == "absence_check":
        if not ref:
            reasons.append("absence_check requires target ref/path")
        if item.get("exists") is not False:
            reasons.append("absence_check requires exists=false")

    elif evidence_type in {
        "terminal_receipt",
        "test_receipt",
        "runtime_probe",
        "current_law",
        "latest_cutover",
        "active_implementation",
        "git_diff",
    }:
        if not ref:
            reasons.append(f"{evidence_type} requires source ref/path/uri")

    return {
        "type": evidence_type,
        "valid": not reasons,
        "reasons": reasons,
        "ref": ref or None,
    }


def validate_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    items = [validate_evidence_item(item) for item in evidence]
    return {
        "valid_count": sum(1 for item in items if item["valid"]),
        "invalid_count": sum(1 for item in items if not item["valid"]),
        "items": items,
    }


def check_completion_evidence(
    claim_type: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        parsed_type = ClaimType(claim_type)
    except ValueError as exc:
        raise ValueError(f"unknown claim_type: {claim_type}") from exc

    validations = [validate_evidence_item(item) for item in evidence]
    valid = [
        item
        for item, validation in zip(evidence, validations)
        if validation["valid"]
    ]
    rejected = [
        {"index": idx, **validation}
        for idx, validation in enumerate(validations)
        if not validation["valid"]
    ]
    present = {str(item["type"]) for item in valid}
    groups = REQUIREMENTS[parsed_type]

    # AND across requirement groups; OR within each group.
    # FILE_WRITTEN = readback AND (hash OR artifact_stat).
    matched_groups = [sorted(group) for group in groups if group & present]
    missing = [sorted(group) for group in groups if not (group & present)]
    independent = [item for item in valid if item.get("type") in _INDEPENDENT_TYPES]

    if not missing and independent:
        verdict = "PASS"
        reason = "all required evidence groups are satisfied by structurally valid independent evidence"
    elif not missing:
        verdict = "UNKNOWN"
        reason = "all requirement groups match, but evidence is not independently verifiable"
    else:
        verdict = "FAIL"
        reason = "completion claim lacks one or more required evidence groups"

    return {
        "verdict": verdict,
        "reason": reason,
        "claim_type": parsed_type.value,
        "present_evidence_types": sorted(present),
        "matched_requirement_groups": matched_groups,
        "missing_requirement_groups": missing,
        "independent_evidence_count": len(independent),
        "rejected_evidence": rejected,
        "rule": "CLAIM != FACT; evidence labels do not count unless their structure validates",
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
