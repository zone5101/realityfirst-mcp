"""Independent owner of the AgentBus Chrome profile fleet.

Separation contract (2026-10-08, "tach chrome khoi bam"):

* This process -- started by the scheduled task ``LivingOS-Chrome-Profile-Fleet`` --
  is the **owner of record** for every managed Chrome profile (acc01..acc12).
* Chrome instances are spawned detached (see ``chrome_profile_runtime``) so the
  browser lifecycle is not bound to a BAM workbench job/process.
* BAM (workbench + MCP) is a **CDP client only**: it may ask the fleet owner to
  ensure / reveal / close a profile.  BAM never spawns, reparents, hides or kills
  Chrome itself.
* ``coupled_to_bam`` in the fleet state is the separation KPI: it is true when a
  live profile root is still parented by a BAM workbench process.

Control protocol: newline-delimited JSON on ``127.0.0.1:<control_port>``
(``{"op": "status"}``, ``ensure``, ``reveal``, ``close``, ``adopt``).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

STAGE_ROOT = Path(__file__).resolve().parents[1]
if str(STAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(STAGE_ROOT))

from rock_man_executor.chrome_embed import browser_hwnd_for_pid  # noqa: E402
from rock_man_executor.chrome_profile_runtime import (  # noqa: E402
    DEFAULT_CONTROL_PORT,
    FLEET_CONTROL,
    FLEET_ROOT,
    FLEET_STATE,
    cdp_live,
    ensure_visible_window,
    spawn_detached,
    wait_cdp,
)

REGISTRY = Path(r'G:\AgentBusProfiles\chrome_profile_registry.json')
RETENTION_DIR = Path(r'G:\AgentBusProfiles\profile_retention')
LOCK = FLEET_ROOT / 'fleet.lock'
FLEET_PROFILES = FLEET_ROOT / 'fleet_profiles.json'
RECEIPTS = FLEET_ROOT / 'receipts'
SCHEMA = 'livingos.chrome-fleet-state.v1'
BAM_WORKBENCH_MARKERS = ('run_black_armor_workbench', 'bam_workbench_supervisor')
# The fleet owner itself lives under rock_man_executor, so it must never be counted as a BAM
# coupling; only an actual workbench/supervisor parent is a separation defect.
FLEET_SELF_MARKERS = ('chrome_fleet_supervisor',)
HEARTBEAT_SECONDS = 10.0
STATE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _normalized(value: str) -> str:
    return str(Path(str(value).strip('"')).resolve()).rstrip('\\/').casefold()


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def registry_profiles() -> list[dict]:
    document = json.loads(REGISTRY.read_text(encoding='utf-8-sig'))
    if document.get('schema') != 'livingos.chrome-profile-registry.v2':
        raise RuntimeError('CHROME_PROFILE_REGISTRY_SCHEMA')
    rows = [dict(row) for row in document.get('profiles') or []]
    if not rows or any(not str(row.get('id') or '') for row in rows):
        raise RuntimeError('CHROME_PROFILE_REGISTRY_INVALID')
    return rows


def fleet_owned_profiles() -> list[dict]:
    """Profiles owned by this fleet but outside the ChatGPT account registry.

    Example: ``bamruntime`` -- the CDP endpoint BAM consumes as a client.  Only the fleet
    owner knows how to start it; BAM has no spawn path for it at all.
    """
    if not FLEET_PROFILES.is_file():
        return []
    document = json.loads(FLEET_PROFILES.read_text(encoding='utf-8-sig'))
    if document.get('schema') != 'livingos.chrome-fleet-profiles.v1':
        raise RuntimeError('CHROME_FLEET_PROFILES_SCHEMA')
    rows = [dict(row) for row in document.get('profiles') or []]
    for row in rows:
        if not str(row.get('id') or '') or not int(row.get('cdp_port') or 0) or not str(row.get('user_data_dir') or ''):
            raise RuntimeError('CHROME_FLEET_PROFILES_INVALID')
    return rows


def profiles() -> list[dict]:
    rows = registry_profiles()
    known = {str(row['id']) for row in rows}
    for row in fleet_owned_profiles():
        if str(row['id']) in known:
            raise RuntimeError('CHROME_FLEET_PROFILE_ID_COLLISION')
        rows.append(row)
    return rows


def profile(profile_id: str) -> dict:
    row = next((candidate for candidate in profiles() if str(candidate['id']) == str(profile_id)), None)
    if row is None:
        raise RuntimeError('CHROME_PROFILE_UNKNOWN')
    return row


def root_pids(row: dict) -> list[int]:
    target = _normalized(str(row['user_data_dir']))
    matches: list[int] = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if str(proc.info.get('name') or '').casefold() != 'chrome.exe':
                continue
            args = [str(arg) for arg in (proc.cmdline() or [])]
            if any(arg.casefold().startswith('--type=') for arg in args):
                continue
            for arg in args:
                if arg.casefold().startswith('--user-data-dir=') and _normalized(arg.split('=', 1)[1]) == target:
                    matches.append(int(proc.info['pid']))
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return sorted(set(matches))


def _parent_report(pid: int) -> tuple[int, str, bool]:
    """Return (parent_pid, parent_label, coupled_to_bam)."""
    try:
        parent = psutil.Process(int(pid)).parent()
    except Exception:
        return 0, 'DEAD', False
    if parent is None:
        return 0, 'DEAD', False
    try:
        command = ' '.join(parent.cmdline() or [])
    except Exception:
        command = ''
    label = f"{parent.name()} pid={parent.pid}"
    lowered = command.casefold()
    coupled = (any(marker.casefold() in lowered for marker in BAM_WORKBENCH_MARKERS)
               and not any(marker.casefold() in lowered for marker in FLEET_SELF_MARKERS))
    return int(parent.pid), label, coupled


def retained_url(row: dict) -> str:
    configured = str(row.get('default_url') or '').strip()
    if configured:
        return configured
    try:
        payload = json.loads((RETENTION_DIR / f"{row['id']}.last_pages.json").read_text(encoding='utf-8'))
        value = str(payload.get('url') or '').strip()
        return value or 'https://chatgpt.com/'
    except Exception:
        return 'https://chatgpt.com/'


def _owner_identity() -> tuple[int, float]:
    process = psutil.Process()
    return int(process.pid), float(process.create_time())


def _identity_alive(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(int(pid))
        return process.is_running() and abs(float(process.create_time()) - float(create_time)) < 0.01
    except Exception:
        return False


_ROOT_SCAN_LOCK = threading.Lock()
_ROOT_SCAN_CACHE: tuple[float, tuple, dict[str, list[int]]] = (0.0, (), {})
FLEET_SCAN_TTL_SECONDS = 30.0


def _listener_pids_by_port() -> dict[int, int]:
    """One TCP-table pass: CDP port -> listening pid (the browser root owns that socket)."""
    out: dict[int, int] = {}
    try:
        for conn in psutil.net_connections(kind='tcp'):
            if getattr(conn, 'status', '') != psutil.CONN_LISTEN or not getattr(conn, 'laddr', None):
                continue
            if str(conn.laddr.ip) not in {'127.0.0.1', '0.0.0.0', '::1'}:
                continue
            out[int(conn.laddr.port)] = int(conn.pid or 0)
    except Exception:
        return out
    return out


def _verified_root_pid(row: dict, pid: int) -> int:
    """Prove a listener pid is really this profile's browser root: exe + CDP port + user-data-dir."""
    if not pid:
        return 0
    try:
        proc = psutil.Process(int(pid))
        if str(proc.name() or '').casefold() != 'chrome.exe':
            return 0
        args = [str(arg) for arg in (proc.cmdline() or [])]
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return 0
    if any(arg.casefold().startswith('--type=') for arg in args):
        return 0
    port_flag = f"--remote-debugging-port={int(row['cdp_port'])}".casefold()
    if not any(arg.casefold() == port_flag for arg in args):
        return 0
    expected = _normalized(str(row['user_data_dir']))
    matched = any(arg.casefold().startswith('--user-data-dir=')
                  and _normalized(arg.split('=', 1)[1]) == expected for arg in args)
    return int(pid) if matched else 0


def _scan_root_pids(rows: list[dict]) -> dict[str, list[int]]:
    """Cheap fleet scan: one TCP pass, then examine only the listening browser roots.

    Scanning every process's command line costs ~12s on this host, so root discovery is
    anchored on the CDP listener (the browser root owns that socket) and then proven by
    exe name, CDP port flag and user-data-dir.  Runtimes without a listener are reported
    OFFLINE by CDP liveness anyway, so nothing is silently omitted.
    """
    listeners = _listener_pids_by_port()
    out: dict[str, list[int]] = {}
    for row in rows:
        pid = _verified_root_pid(row, int(listeners.get(int(row['cdp_port']), 0)))
        out[str(row['id'])] = [pid] if pid else []
    return out


def root_pids(row: dict) -> list[int]:
    return root_pid_map([row], ttl=1.0).get(str(row['id']), [])


def root_pid_map(rows: list[dict], *, ttl: float = FLEET_SCAN_TTL_SECONDS) -> dict[str, list[int]]:
    """Cached fleet scan; the cache stamp is taken after the scan, keyed by registry signature."""
    global _ROOT_SCAN_CACHE
    key = tuple((str(row['id']), int(row['cdp_port'])) for row in rows)
    now = time.monotonic()
    with _ROOT_SCAN_LOCK:
        stamp, cached_key, cached = _ROOT_SCAN_CACHE
        if cached_key != key or (now - stamp) > float(ttl):
            cached = _scan_root_pids(rows)
            _ROOT_SCAN_CACHE = (time.monotonic(), key, cached)
    return {str(row['id']): list(cached.get(str(row['id']), [])) for row in rows}


def snapshot() -> dict:
    owner_pid, owner_create_time = _owner_identity()
    registry_rows = profiles()
    pid_map = root_pid_map(registry_rows)
    rows: list[dict] = []
    for row in registry_rows:
        port = int(row['cdp_port'])
        pids = pid_map.get(str(row['id']), [])
        live = cdp_live(port)
        pid = pids[0] if len(pids) == 1 else 0
        parent_pid, parent_label, coupled = _parent_report(pid) if pid else (0, 'NONE', False)
        if live and pid:
            status = 'OWNED' if parent_pid == owner_pid else ('ADOPTED_ORPHAN' if parent_label == 'DEAD' else 'FOREIGN_PARENT')
        elif pid:
            status = 'PID_WITHOUT_CDP'
        else:
            status = 'OFFLINE'
        rows.append({
            'id': str(row['id']), 'cdp_port': port, 'user_data_dir': str(row['user_data_dir']),
            'status': status, 'cdp_live': live, 'browser_pid': pid, 'root_pid_count': len(pids),
            'parent_pid': parent_pid, 'parent_label': parent_label, 'coupled_to_bam': bool(coupled),
            'window_top_level': bool(pid and browser_hwnd_for_pid(pid)),
            'retained_url': retained_url(row),
        })
    return {
        'schema': SCHEMA, 'owner': 'rock_man_executor.chrome_fleet_supervisor',
        'owner_pid': owner_pid, 'owner_create_time': owner_create_time,
        'owner_identity': f'{owner_pid}@{owner_create_time:.3f}',
        'control_port': int(json.loads(FLEET_CONTROL.read_text(encoding='utf-8')).get('control_port'))
        if FLEET_CONTROL.exists() else DEFAULT_CONTROL_PORT,
        'updated_at': _now(), 'profiles': rows,
        'live_count': sum(1 for row in rows if row['cdp_live']),
        'owned_count': sum(1 for row in rows if row['status'] == 'OWNED'),
        'adopted_count': sum(1 for row in rows if row['status'] == 'ADOPTED_ORPHAN'),
        'coupled_to_bam': any(row['coupled_to_bam'] for row in rows),
    }


def _listener_pid(port: int) -> int | None:
    try:
        for conn in psutil.net_connections(kind='tcp'):
            if (getattr(conn, 'pid', None)
                    and getattr(getattr(conn, 'laddr', None), 'port', None) == int(port)
                    and str(getattr(conn, 'status', '')).upper() == 'LISTEN'):
                return int(conn.pid)
    except Exception:
        return None
    return None


def op_status(_payload: dict) -> dict:
    return {'ok': True, **snapshot()}


def op_ensure(payload: dict) -> dict:
    row = profile(str(payload.get('profile') or ''))
    port = int(row['cdp_port'])
    url = str(payload.get('url') or retained_url(row))
    if cdp_live(port):
        return {'ok': True, 'result': {'id': row['id'], 'cdp_port': port, 'already_running': True,
                                       'browser_pid': (root_pids(row) or [0])[0],
                                       'owner': 'chrome_fleet_supervisor'}}
    existing = root_pids(row)
    if len(existing) > 1:
        return {'ok': False, 'error': 'CHROME_PROFILE_RUNTIME_AMBIGUOUS'}
    if len(existing) == 1:
        ready = wait_cdp(port, existing[0], timeout=25.0)
        return {'ok': bool(ready), 'error': '' if ready else 'CHROME_PROFILE_EXISTING_RUNTIME_CDP_TIMEOUT',
                'result': {'id': row['id'], 'cdp_port': port, 'already_running': True, 'browser_pid': existing[0],
                           'owner': 'chrome_fleet_supervisor'}}
    launched = spawn_detached(row, url=url, owner='chrome_fleet_supervisor')
    ready = wait_cdp(port, int(launched['pid']), timeout=25.0)
    return {'ok': bool(ready), 'error': '' if ready else 'CHROME_PROFILE_LAUNCH_CDP_TIMEOUT',
            'result': {**launched, 'launch_pending': not ready}}


def op_reveal(payload: dict) -> dict:
    row = profile(str(payload.get('profile') or ''))
    port = int(row['cdp_port'])
    url = str(payload.get('url') or retained_url(row))
    if not cdp_live(port):
        return {'ok': False, 'error': 'CHROME_PROFILE_OFFLINE'}
    pids = root_pids(row)
    had_window = bool(pids and browser_hwnd_for_pid(pids[0]))
    outcome = ensure_visible_window(port, url, force_new_window=not had_window)
    after = root_pids(row)
    window_now = bool(after and browser_hwnd_for_pid(after[0]))
    return {'ok': True, 'result': {'id': row['id'], 'cdp_port': port, 'url': url,
                                   'window_before': had_window, 'window_after': window_now, **outcome}}


def _close_browser_via_cdp(port: int) -> None:
    from rock_man_executor.chrome_profile_runtime import browser_ws_url
    ws_url = browser_ws_url(port)
    if not ws_url:
        raise RuntimeError('CHROME_BROWSER_CDP_TARGET_MISSING')
    from websockets.sync.client import connect
    with connect(ws_url, open_timeout=1.5) as ws:
        ws.send(json.dumps({'id': 1, 'method': 'Browser.close'}))


def op_close(payload: dict) -> dict:
    """Owner-only teardown.  Identity is proven by user-data-dir + CDP port, never by BAM."""
    row = profile(str(payload.get('profile') or ''))
    port = int(row['cdp_port'])

    def finish(detail: dict) -> dict:
        # Receipts make every teardown auditable (the 2026-10-08 orphan incident showed that
        # a kill path without a receipt is impossible to attribute after the fact).
        _write_receipt('close', {'profile': str(row['id']), 'cdp_port': port, **detail})
        return {'ok': True, 'result': {'id': row['id'], 'cdp_port': port, **detail}}

    if not cdp_live(port):
        return finish({'status': 'ALREADY_CLOSED'})
    pids = root_pids(row)
    if len(pids) != 1:
        return {'ok': False, 'error': 'CHROME_PROFILE_RUNTIME_AMBIGUOUS'}
    root = psutil.Process(pids[0])
    family = [int(root.pid), *(int(child.pid) for child in root.children(recursive=True))]
    if _listener_pid(port) not in family:
        return {'ok': False, 'error': 'CHROME_PROFILE_CDP_OWNER_NOT_IN_EXACT_MANAGED_TREE'}
    _close_browser_via_cdp(port)
    for _ in range(30):
        time.sleep(0.1)
        if not cdp_live(port):
            return finish({'status': 'CLOSED', 'graceful': True, 'pid': pids[0]})
    members = [psutil.Process(pid) for pid in reversed(family)]
    for process in members:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(members, timeout=3)
    for _ in range(20):
        time.sleep(0.1)
        if not cdp_live(port):
            return finish({'status': 'CLOSED', 'graceful': False, 'pid': pids[0]})
    return {'ok': False, 'error': 'CHROME_PROFILE_CLOSE_TIMEOUT'}


def op_adopt(_payload: dict) -> dict:
    """Record ownership of every live profile, including runtimes orphaned by dead BAM hosts."""
    state = snapshot()
    adopted = [row['id'] for row in state['profiles'] if row['status'] in {'ADOPTED_ORPHAN', 'FOREIGN_PARENT'}]
    _write_receipt('adopt', {'adopted': adopted, 'coupled_to_bam': state['coupled_to_bam'],
                             'owner_identity': state['owner_identity']})
    return {'ok': True, 'result': {'adopted': adopted, 'state': state}}


def _write_receipt(kind: str, payload: dict) -> Path:
    path = RECEIPTS / f"{kind}-{int(time.time() * 1000)}.json"
    _atomic_write(path, {'schema': 'livingos.chrome-fleet-receipt.v1', 'kind': kind, 'at': _now(), **payload})
    return path


def _heartbeat() -> None:
    while True:
        try:
            with STATE_LOCK:
                _atomic_write(FLEET_STATE, snapshot())
        except Exception:
            pass
        time.sleep(HEARTBEAT_SECONDS)


def _acquire_ownership(control_port: int) -> bool:
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            prior = json.loads(LOCK.read_text(encoding='utf-8'))
        except Exception:
            prior = {}
        if _identity_alive(int(prior.get('owner_pid') or 0), float(prior.get('owner_create_time') or 0)):
            return False
    owner_pid, owner_create_time = _owner_identity()
    _atomic_write(LOCK, {'schema': 'livingos.chrome-fleet-lock.v1', 'owner_pid': owner_pid,
                         'owner_create_time': owner_create_time, 'at': _now()})
    _atomic_write(FLEET_CONTROL, {'schema': 'livingos.chrome-fleet-control.v1', 'control_port': int(control_port),
                                  'owner_pid': owner_pid, 'owner_create_time': owner_create_time, 'at': _now()})
    return True


def _dispatch(request: dict) -> dict:
    handlers = {'status': op_status, 'ensure': op_ensure, 'reveal': op_reveal,
                'close': op_close, 'adopt': op_adopt}
    handler = handlers.get(str(request.get('op') or 'status'))
    if handler is None:
        return {'ok': False, 'error': 'CHROME_FLEET_UNKNOWN_OP'}
    try:
        return handler(request)
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc)}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def _handle(connection: socket.socket) -> None:
    with connection:
        try:
            buffer = b''
            while b'\n' not in buffer:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer += chunk
            request = json.loads(buffer.split(b'\n', 1)[0].decode('utf-8', 'replace'))
            response = _dispatch(request if isinstance(request, dict) else {})
        except Exception as exc:
            response = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        try:
            connection.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        except OSError:
            pass


def _serve(control_port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', int(control_port)))
        server.listen(16)
        while True:
            connection, _ = server.accept()
            threading.Thread(target=_handle, args=(connection,), name='chrome-fleet-request', daemon=True).start()


def main() -> int:
    control_port = int(os.environ.get('CHROME_FLEET_CONTROL_PORT') or DEFAULT_CONTROL_PORT)
    if not _acquire_ownership(control_port):
        return 0
    threading.Thread(target=_heartbeat, name='chrome-fleet-heartbeat', daemon=True).start()
    _write_receipt('owner-start', {'control_port': control_port})
    try:
        _serve(control_port)
    except KeyboardInterrupt:
        pass
    finally:
        _write_receipt('owner-stop', {'control_port': control_port})
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

