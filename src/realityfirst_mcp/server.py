from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .claims import classify_claim as _classify_claim
from .compaction import compact_evidence as _compact_evidence
from .compaction import record_loss as _record_loss
from .evidence import (
    build_verification_plan as _build_verification_plan,
    check_completion_evidence as _check_completion_evidence,
    resolve_precedence as _resolve_precedence,
)
from .policy import load_policy, replay_gate as _replay_gate


mcp = MCPServer("RealityFirst MCP")


@mcp.tool()
def classify_claim(claim: str, claim_type: str | None = None) -> dict[str, Any]:
    """Classify an agent claim and return the evidence gate it must satisfy."""
    return _classify_claim(claim, claim_type)


@mcp.tool()
def build_verification_plan(
    claim: str,
    claim_type: str,
    available_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Reality-first verification plan without executing external tools."""
    return _build_verification_plan(claim, claim_type, available_tools)


@mcp.tool()
def check_completion_evidence(
    claim_type: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check whether a completion/current-state claim has enough independent evidence."""
    return _check_completion_evidence(claim_type, evidence)


@mcp.tool()
def resolve_precedence(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve conflicting claims using Reality > CURRENT law > cutover > implementation/tests > history."""
    return _resolve_precedence(candidates)


@mcp.tool()
def check_replay_safety(state: str, side_effect_state: str | None = None) -> dict[str, Any]:
    """Block blind replay when side effects or request lineage are unresolved."""
    return _replay_gate(state, side_effect_state).as_dict()


@mcp.tool()
def compact_evidence(
    evidence: list[dict[str, Any]],
    max_items: int = 8,
) -> dict[str, Any]:
    """Compact evidence while retaining source refs and prioritizing direct Reality proof."""
    return _compact_evidence(evidence, max_items)


@mcp.tool()
def record_loss(
    claim: str,
    why_wrong: str,
    corrected_form: str,
    evidence_refs: list[str],
    prevention_rule: str,
) -> dict[str, Any]:
    """Create a structured loss-ledger entry. This tool does not persist it."""
    return _record_loss(claim, why_wrong, corrected_form, evidence_refs, prevention_rule)


@mcp.resource("realityfirst://policy/core")
def core_policy() -> str:
    """Core Reality-first invariants as JSON."""
    import json
    return json.dumps(load_policy("core"), ensure_ascii=False, indent=2)


@mcp.resource("realityfirst://policy/completion")
def completion_policy() -> str:
    """Completion claim gates as JSON."""
    import json
    return json.dumps(load_policy("completion"), ensure_ascii=False, indent=2)


@mcp.resource("realityfirst://policy/precedence")
def precedence_policy() -> str:
    """Current-state source precedence as JSON."""
    import json
    return json.dumps(load_policy("precedence"), ensure_ascii=False, indent=2)


@mcp.prompt()
def reality_first(objective: str = "work on the current task") -> str:
    """Base behavior prompt for Reality-first agent work."""
    return f"""You are working on: {objective}

Use these invariants:
- REALITY > CLAIM.
- Read wide, load narrow: scan with tools, bring only decisive evidence into context.
- File existence does not prove active runtime authority.
- Fresh live Reality > CURRENT law > latest cutover > active implementation/tests > history.
- CREATED/WRITTEN/BUILT/DEPLOYED/PASSED/UPDATED/DELETED/SYNCED claims require independent readback, receipt, or runtime proof.
- UNKNOWN/DO_NOT_REPLAY or unresolved side effects must be reconciled before replay.
- Use tools for facts and the model for decisions.
- Do not invent a subsystem when a smaller missing primitive explains the gap.
- When replacing architecture: prove -> cut over -> retire/delete obsolete path.
- Report only FOUND / CHANGED / VERIFIED / BLOCKED when a compact checkpoint is useful.
"""


@mcp.prompt()
def read_wide(objective: str) -> str:
    """Repository-wide coverage prompt that avoids dumping the whole repo into context."""
    return f"""Audit objective: {objective}

READ WIDE WITHOUT TOKEN EXCUSE:
1. Inventory the whole relevant surface with search/list/query tools.
2. Classify CURRENT / ACTIVE / RETIRED / HISTORICAL / UNKNOWN.
3. Deep-read only authoritative files needed to resolve claims.
4. Never infer current state from file existence alone.
5. Cross-check current claims against fresh runtime/Reality where applicable.
6. Preserve explicit coverage limits instead of pretending every byte was deep-read.
"""


@mcp.prompt()
def crosscheck_completion(claim: str) -> str:
    """Prompt for independently verifying an agent's completion claim."""
    return f"""Cross-check this claim independently:

{claim}

Do not repeat the agent's reasoning. Probe the claimed side effect directly.
Examples:
- wrote/created -> read exact target back
- deleted -> exact absence check
- built -> terminal receipt + artifact stat
- tests passed -> test receipt
- deployed/restarted -> fresh runtime probe
- synced -> compare authoritative state on both sides

If evidence disagrees with the claim, Reality wins.
"""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
