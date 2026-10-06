# RealityFirst MCP

A small MCP behavior layer for coding agents that should **verify reality instead of trusting their own completion claims**.

It does not replace your filesystem, terminal, GitHub, browser, deployment, or machine-control tools. It sits beside them and gives the agent a reusable discipline:

> **Use tools for facts. Use the model for decisions. Reality beats the report.**

## Why

Coding agents are often good at doing work and surprisingly bad at proving that they actually did it.

A typical failure looks like:

```text
agent: "I wrote compact_summary.json"
disk:  "compact_summary placeholder"
```

RealityFirst turns completion claims into explicit evidence gates.

- `wrote/created` -> read the exact target back
- `deleted` -> prove exact absence
- `built` -> terminal receipt + artifact check
- `tests passed` -> test/terminal receipt
- `deployed/restarted` -> fresh runtime probe
- `UNKNOWN_DO_NOT_REPLAY` -> reconcile the same lineage before any replay

## Core invariants

```text
REALITY > CLAIM

READ WIDE, LOAD NARROW

FILE EXISTS != ACTIVE

HISTORY != CURRENT

fresh live Reality
> CURRENT law
> latest cutover
> active implementation/tests
> history
> model report

CREATED / WRITTEN / BUILT / DEPLOYED / PASSED / UPDATED / DELETED / SYNCED
=> independent readback / receipt / runtime proof

UNKNOWN SIDE EFFECT
=> NO BLIND REPLAY

REPLACE
=> PROVE -> CUTOVER -> RETIRE OLD
```

## What it exposes

### Tools

- `classify_claim` — classify a claim and return its evidence gate
- `build_verification_plan` — suggest the smallest independent verification plan
- `check_completion_evidence` — PASS/FAIL/UNKNOWN for supplied evidence
- `resolve_precedence` — resolve current-vs-history conflicts
- `check_replay_safety` — block blind replay on unresolved side effects
- `compact_evidence` — keep decisive evidence without losing source refs
- `record_loss` — create a structured anti-repeat/loss-ledger entry

### Resources

- `realityfirst://policy/core`
- `realityfirst://policy/completion`
- `realityfirst://policy/precedence`

### Prompts

- `reality_first`
- `read_wide`
- `crosscheck_completion`

## Install and run

Requires Python 3.10+ and an MCP host.

Using `uvx` directly from GitHub:

```bash
uvx --from git+https://github.com/zone5101/realityfirst-mcp realityfirst-mcp
```

Or clone it:

```bash
git clone https://github.com/zone5101/realityfirst-mcp
cd realityfirst-mcp
uv sync
uv run realityfirst-mcp
```

The server uses **stdio** by default.

The current official MCP Python SDK 2.x is used (`mcp>=2,<3`).

## MCP host config

Generic stdio configuration:

```json
{
  "mcpServers": {
    "realityfirst": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/zone5101/realityfirst-mcp",
        "realityfirst-mcp"
      ]
    }
  }
}
```

For Cline, add that server in its MCP server configuration. Other MCP hosts use the same command/args shape even if the surrounding configuration file differs.

## Suggested agent instruction

Adding the MCP server is useful; telling the agent **when to call it** makes it much more effective:

```text
Use RealityFirst whenever you are about to trust or emit a completion/current-state claim.

Before saying created/written/built/deployed/passed/updated/deleted/synced:
1. classify the claim,
2. obtain independent evidence with the real filesystem/terminal/runtime tool,
3. call check_completion_evidence,
4. only claim completion on PASS.

For architecture/current-state questions, resolve conflicts with:
live Reality > CURRENT law > latest cutover > active implementation/tests > history.

Never blindly replay UNKNOWN_DO_NOT_REPLAY or unresolved side effects.
```

## Example: catching a fake completion

```python
# The agent says it wrote a file.
classify_claim("compact_summary.json was written")
# -> claim_type: file_written
# -> requires readback

# If all we have is the agent's own report:
check_completion_evidence(
    claim_type="file_written",
    evidence=[{"type": "model_report", "valid": True}]
)
# -> FAIL

# After the real filesystem tool reads the target:
check_completion_evidence(
    claim_type="file_written",
    evidence=[{"type": "readback", "valid": True, "ref": "file://compact_summary.json"}]
)
# -> PASS
```

## Design boundary

RealityFirst is deliberately **not** another execution engine.

```text
Agent
├── filesystem / terminal / GitHub / LivingOS / cloud tools
└── RealityFirst MCP
      ├── classify the claim
      ├── define required proof
      ├── resolve precedence
      └── reject unsupported completion
```

The authoritative side effect still belongs to the tool that owns it.

RealityFirst should never become a second mutation authority.

## Development

```bash
uv sync --extra dev
uv run pytest -q
```

Test case zero is the failure that motivated the project: an agent reports that output files were written while disk reality still contains placeholders.

## Status

`0.1.0` — alpha.

The first release intentionally stays small: claim classification, evidence gates, current-state precedence, replay safety, compaction, and reusable MCP prompts/resources.

## License

MIT
