# SEPARATION RECEIPT — Chrome tách khỏi BAM (2026-10-08)

Contract: BAM thành **CDP-client thuần**, không sở hữu Chrome dưới bất kỳ hình thức nào
(process, window, capability, MCP-connection). Fleet do chủ sở hữu độc lập quản.

## 1. Ràng buộc đã bị loại bỏ (5 tầng, mỗi tầng có bằng chứng)

| # | Tầng | Trước | Sau | Bằng chứng |
|---|------|-------|-----|------------|
| 1 | Process | Workbench `Popen` Chrome, không detached/breakaway → Chrome mồ côi khi BAM chết | Spawn detached (`DETACHED_PROCESS\|CREATE_NEW_PROCESS_GROUP` + breakaway khi được) do chủ sở hữu fleet; BAM **không có đường spawn** (fail toasts `CHROME_FLEET_OWNER_UNAVAILABLE`) | `ensure bamruntime` → pid 25968, **parent = owner pid**, không phải BAM |
| 2 | Window | `embed_hwnd` → `SetParent(chrome, qt_viewport)`; BAM chết ⇒ cửa sổ Chrome bị hủy | Overlay không sở hữu (`attach_overlay/move_overlay/detach_overlay`); `embed_hwnd` **raise** `CHROME_EMBED_RETIRED_USE_OVERLAY` cho Chrome | 5/5 cửa sổ hiện tại `parent=0` |
| 3 | Identity (MCP) | `connection_registry.json` của BAM MCP liệt kê `chatgpt_web_acc01..05` + `chatgpt-manager` là connection của BAM | 6 kết nối Chrome chuyển sang `G:\AgentBusProfiles\fleet\chrome_connection_registry.json`; sổ BAM chỉ còn kết nối không-Chrome | `BAM_AFTER = ['GPT-5.6-Sol-skill-router-specialist']` |
| 4 | Close/kill | `close_profile_runtime` → `_terminate_profile_process_tree` do BAM UI gọi | Teardown là hành vi của owner (`op_close`), identity theo user-data-dir+CDP port; BAM chỉ yêu cầu | `close bamruntime` → `CLOSED graceful`, 9223 tắt |
| 5 | Guard/hide | Guard thread `bam-chrome-hide-*` + cờ `--window-position=-32000,-32000` | Đã xoá (guard + cờ) | grep: 0 call-site còn lại |

## 2. Kiến trúc mới

- **Chủ sở hữu:** `rock_man_executor\chrome_fleet_supervisor.py` — task `LivingOS-Chrome-Profile-Fleet`
  (XML tại `G:\AgentBusProfiles\fleet\LivingOS-Chrome-Profile-Fleet.xml`), control socket
  `127.0.0.1:9411`, heartbeat `fleet_state.json` mỗi 10s (scan ~1.2s, TTL 30s).
  Ops: `status`, `ensure`, `reveal`, `close`, `adopt`.
- **Nguyên thuỷ spawn:** `rock_man_executor\chrome_profile_runtime.py` — `spawn_detached`,
  `wait_cdp`, `ensure_visible_window` (CDP `Target.createTarget(newWindow=True)`), `fleet_request`.
- **Profile bổ sung do fleet sở hữu:** `G:\AgentBusProfiles\fleet\fleet_profiles.json`
  (schema `livingos.chrome-fleet-profiles.v1`) — `bamruntime` = CDP 9223
  (user-data-dir giữ nguyên `_state\bam\chrome_stable_profile`), `default_url: about:blank`.
- **BAM:** `browser_runtime.py` → attach-only qua `_fleet_owner_ensure()`;
  `chrome_profiles_ui.launch_profile` → `fleet_request('ensure')`;
  `workbench_ui` → overlay + `_hwnd_matches_profile` yêu cầu **top-level (parent==0)**;
  `bam_profile_open.py` (AgentBus) → `contained:False, host:CHROME_FLEET, bam_role:CDP_CLIENT`.
- **KPI tách:** `coupled_to_bam` trong `fleet_state.json` = true khi một profile sống còn bị
  parent bởi tiến trình workbench/supervisor. Hiện tại: **False**.

## 3. Bằng chứng thực tại (A/B)

- **Mã cũ (trước tách):** workbench 10132 (code `SetParent`) bị dừng ⇒ 4 Chrome
  (70040/65848/66380/81728) **chết** — cửa sổ bị hủy theo cha, Chrome thoát vì hết cửa sổ; CDP 9324–9327 tắt.
- **Mã mới (sau tách):** 5 Chrome fleet-owned (23768/35736/48036/15856/22900) **sống sót qua
  ≥2 lần workbench đổi pid** (53632 → 78240, do supervisor tự hồi sinh) — cùng pid, cùng CDP,
  cùng HWND, `parent=0`. `VERDICT_CHROME_SURVIVED_RESTART = True`.
- **Phục hồi mồ côi:** 4 phiên acc02–acc05 (mất cửa sổ từ khi 47512 chết) được mở lại cửa sổ
  qua CDP (giữ nguyên account/cookie), sau đó do fleet đảm nhận (`ensure` → `OWNED`).
- **BAM MCP 8766:** vẫn LISTENING (pid 69368) — không bị ảnh hưởng.

## 4. Rollback

Mọi file đã sửa có backup `.bak-separation-20261008` (SHA256 ghi trong log):
`chrome_profiles_ui.py`, `chrome_embed.py`, `workbench_ui.py`, `browser_runtime.py`
(cùng `connection_registry.json`). Task: `Unregister-ScheduledTask LivingOS-Chrome-Profile-Fleet`.

## 5. Bề mặt điều khiển còn lại (chủ ý, không phải sở hữu)

BAM vẫn **đặt/vị trí cửa sổ** profile đang chọn qua overlay (geometry + z-order, không
`SetParent`, không sở hữu) — giống UX cũ nhưng không có quyền sở hữu vòng đời. Chrome giữ
cửa sổ của mình: BAM crash không thể hủy/ẩn vĩnh viễn nó.

## 6. Kênh 4 — auto bật BAM UI: ĐÃ BỎ (2026-10-08, theo yêu cầu của chủ — trước di dời kiến trúc lớn)

* Task `LivingOS-BAM-WorkbenchSupervisorV1` → **Disabled** (không logon-trigger, không
  restart-on-failure, `nextRun` trống, result=0). Backup XML:
  `LivingOS-BAM-WorkbenchSupervisorV1.backup-pre-disable-20261008.xml`.
* Tiến trình supervisor thường trú (pid 78876) → **đã dừng**; xác minh không hồi sinh sau 12s+.
* Workbench instance cuối (pid 78240) **vẫn sống** — khi nó thoát, không gì hồi sinh nó nữa.
* Chrome fleet 5/5 vẫn sống (`parent=0`), MCP 8766 vẫn LISTENING.
* Re-enable khi cần: `Enable-ScheduledTask LivingOS-BAM-WorkbenchSupervisorV1; Start-ScheduledTask LivingOS-BAM-WorkbenchSupervisorV1`.

**Tổng kết các kênh auto sau cùng:** BAM UI không tự bật Chrome (spawn đã loại), không
tự sống lại (kênh 4 đã loại); còn: poller request-file governed (chỉ mở khi có yêu cầu
tường minh có receipt), click tab/placeholder (user), auto-attach overlay không sở hữu.

