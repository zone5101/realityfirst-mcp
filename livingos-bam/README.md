# LivingOS BAM ⇄ Chrome Separation (2026-10-08)

Snapshot of the "tách Chrome khỏi BAM" separation: **BAM is a pure CDP client — it does not
spawn, reparent, hide, kill, or own Chrome in any form.** The Chrome fleet is owned by an
independent owner.

## Layout

- `source/rock_man_executor/` — modified/new BAM stage modules (live copy from
  `G:\LivingOS\_ACTIVE_WORK_20260905\Black Armor\rock_man_executor_phase2_stage\rock_man_executor\`):
  - `chrome_fleet_supervisor.py` — NEW independent fleet owner (task `LivingOS-Chrome-Profile-Fleet`,
    control socket `127.0.0.1:9411`, ops `status/ensure/reveal/close/adopt`, heartbeat every 10s,
    separation KPI `coupled_to_bam`).
  - `chrome_profile_runtime.py` — NEW ownership-free spawn primitive (`DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP` + breakaway when allowed) + fleet client (`fleet_request`).
  - `chrome_embed.py` — overlay API (`attach_overlay/move_overlay/detach_overlay`, no `SetParent`);
    `embed_hwnd` now raises `CHROME_EMBED_RETIRED_USE_OVERLAY` for Chrome windows.
  - `chrome_profiles_ui.py` — `launch_profile` delegates to the fleet owner (no local spawn,
    loud `CHROME_FLEET_OWNER_UNAVAILABLE` when the owner is down); `close_profile_runtime`
    delegates to the owner; window-hijack flags and the hide guard removed.
  - `workbench_ui.py` — overlay call sites, `_hwnd_matches_profile` requires a TOP-LEVEL
    (`parent == 0`) window, flash-guard retired, ATTACHED/SELECTED receipts report
    `top_level` + `window_owner:"chrome"` instead of `parent`.
- `source/browser_runtime.py` — BAM's browser endpoint (CDP 9223) is attach-only:
  `_fleet_owner_ensure()` asks the owner; BAM never launches its own Chrome.
- `agentbus/bam_profile_open.py` — AgentBus-side containment now records
  `contained:false, host:"CHROME_FLEET", bam_role:"CDP_CLIENT"`.
- `fleet/` — Chrome-side ownership artifacts: task XML (`LivingOS-Chrome-Profile-Fleet`),
  `fleet_profiles.json` (incl. `bamruntime`), `chrome_connection_registry.json` (the
  `chatgpt_web_acc01..05` + manager connections moved OUT of BAM MCP's
  `connection_registry.json`), `SEPARATION-RECEIPT-20261008.md` (full evidence),
  `fleet-receipts/` (owner-start/adopt/acceptance receipts), disabled
  `LivingOS-BAM-WorkbenchSupervisorV1` backup XML (BAM UI auto-revive removed by owner request).

## Evidence (A/B)

- OLD code: terminating workbench 10132 killed 4 live Chrome (windows were `SetParent` children).
- NEW code: 5 fleet-owned Chrome survived ≥2 workbench restarts (53632→78240) with unchanged
  pids/CDP/HWNDs, all `window_parent == 0`, `coupled_to_bam == False`. Acceptance receipt:
  `fleet/fleet-receipts/acceptance-1791440233109.json` → `ACCEPTANCE_OVERALL = PASS`.

## Rollback

Every replaced file has a same-tree backup `*.bak-separation-20261008` in the stage.
Re-enable BAM auto-revive: `Enable-ScheduledTask LivingOS-BAM-WorkbenchSupervisorV1`.
