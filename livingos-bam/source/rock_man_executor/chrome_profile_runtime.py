"""Chrome profile runtime primitive with NO ownership coupling to the caller.

Separation contract (2026-10-08, "tach chrome khoi bam"):

* Chrome is spawned **detached** from the calling process
  (``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``, plus
  ``CREATE_BREAKAWAY_FROM_JOB`` when the caller's job object allows breakaway).
  The caller therefore never becomes the lifecycle owner of the browser.
* Chrome's native window is **never reparented** (no ``SetParent``).  BAM is a CDP
  client; at most it places the window with an ownership-free overlay.
* Ownership of record lives with ``rock_man_executor.chrome_fleet_supervisor``
  (scheduled task ``LivingOS-Chrome-Profile-Fleet``), reached through
  :func:`fleet_request` on the fleet control socket.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

CHROME = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
FLEET_ROOT = Path(r'G:\AgentBusProfiles\fleet')
FLEET_STATE = FLEET_ROOT / 'fleet_state.json'
FLEET_CONTROL = FLEET_ROOT / 'fleet_control_port.json'
DEFAULT_CONTROL_PORT = 9411

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

SPAWN_MODE_DETACHED_BREAKAWAY = 'DETACHED_BREAKAWAY'
SPAWN_MODE_DETACHED_JOB_BOUND = 'DETACHED_JOB_BOUND'


def detach_creationflags(*, attempt_breakaway: bool = True) -> tuple[int, str]:
    """Return (creationflags, spawn_mode) used for an ownership-free spawn."""
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    breakaway = getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', CREATE_BREAKAWAY_FROM_JOB)
    if attempt_breakaway and breakaway:
        return flags | breakaway, SPAWN_MODE_DETACHED_BREAKAWAY
    return flags, SPAWN_MODE_DETACHED_JOB_BOUND


def launch_args(row: dict, url: str | None) -> list[str]:
    """Build the managed-profile command line.

    No window-position/window-size hijack flags: the browser window belongs to Chrome,
    BAM positions it (if at all) with an ownership-free overlay.
    """
    return [
        str(CHROME),
        f"--remote-debugging-port={int(row['cdp_port'])}",
        f"--user-data-dir={row['user_data_dir']}",
        '--no-first-run', '--no-default-browser-check',
        '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
        str(url or 'https://chatgpt.com/'),
    ]


def cdp_live(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', int(port)), timeout=0.12):
            return True
    except OSError:
        return False


def browser_ws_url(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/json/version', timeout=1.5) as response:
            meta = json.loads(response.read().decode('utf-8'))
        return str(meta.get('webSocketDebuggerUrl') or '') or None
    except Exception:
        return None


def fleet_root() -> Path:
    FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    return FLEET_ROOT


def spawn_detached(row: dict, url: str | None = None, *, owner: str = 'chrome_fleet_supervisor') -> dict:
    """Spawn one managed Chrome profile without becoming its owner of record."""
    flags, mode = detach_creationflags()
    args = launch_args(row, url)
    try:
        child = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags, cwd=str(fleet_root()),
        )
    except OSError:
        if mode != SPAWN_MODE_DETACHED_BREAKAWAY:
            raise
        # ERROR_ACCESS_DENIED: the caller sits in a job object that forbids breakaway.
        flags, mode = detach_creationflags(attempt_breakaway=False)
        child = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags, cwd=str(fleet_root()),
        )
    return {
        'id': str(row['id']), 'pid': int(child.pid), 'cdp_port': int(row['cdp_port']),
        'already_running': False, 'spawn_mode': mode, 'owner': owner, 'launch_pending': True,
    }


def wait_cdp(port: int, pid: int, *, timeout: float = 25.0) -> bool:
    import psutil
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if cdp_live(port):
            return True
        try:
            if not psutil.Process(int(pid)).is_running():
                return False
        except Exception:
            return False
        time.sleep(0.1)
    return cdp_live(port)


def ensure_visible_window(port: int, url: str, *, force_new_window: bool = False) -> dict:
    """Guarantee one real top-level browser window exists for this profile.

    Recovers Chrome runtimes whose window was destroyed with a dead BAM workbench
    (the previous ``SetParent`` host).  Sessions/cookies are untouched.

    ``force_new_window`` is required when the profile still owns live page targets but
    has lost its native window -- the exact orphan signature left behind by a dead
    ``SetParent`` host.
    """
    ws_url = browser_ws_url(port)
    if not ws_url:
        return {'opened': False, 'reason': 'CDP_ENDPOINT_UNAVAILABLE'}
    from websockets.sync.client import connect
    pages: list[dict] = []
    target_id = None
    with connect(ws_url, open_timeout=2.0) as ws:
        ws.send(json.dumps({'id': 1, 'method': 'Target.getTargets'}))
        raw = json.loads(ws.recv(timeout=3.0))
        pages = [row for row in (raw.get('result') or {}).get('targetInfos') or [] if row.get('type') == 'page']
        if force_new_window or not pages:
            ws.send(json.dumps({'id': 2, 'method': 'Target.createTarget',
                                'params': {'url': url, 'newWindow': True}}))
            created = json.loads(ws.recv(timeout=5.0))
            target_id = (created.get('result') or {}).get('targetId')
    return {'opened': bool(target_id), 'target_id': target_id,
            'existing_page_targets': len(pages), 'forced_new_window': bool(force_new_window)}


def fleet_control_port() -> int:
    try:
        row = json.loads(FLEET_CONTROL.read_text(encoding='utf-8'))
        return int(row.get('control_port') or DEFAULT_CONTROL_PORT)
    except Exception:
        return DEFAULT_CONTROL_PORT


def fleet_request(op: str, *, timeout: float = 30.0, **payload) -> dict:
    """Ask the independent fleet owner to act.  Never spawns locally by itself."""
    request = {'op': str(op), **payload}
    with socket.create_connection(('127.0.0.1', fleet_control_port()), timeout=min(5.0, timeout)) as sock:
        sock.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        sock.settimeout(timeout)
        buffer = b''
        while b'\n' not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    line = buffer.split(b'\n', 1)[0].decode('utf-8', 'replace').strip()
    if not line:
        raise RuntimeError('CHROME_FLEET_EMPTY_RESPONSE')
    response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError('CHROME_FLEET_BAD_RESPONSE')
    return response


def fleet_available() -> bool:
    try:
        with socket.create_connection(('127.0.0.1', fleet_control_port()), timeout=0.25):
            return True
    except OSError:
        return False


def fleet_state() -> dict:
    try:
        row = json.loads(FLEET_STATE.read_text(encoding='utf-8'))
        return row if isinstance(row, dict) else {}
    except Exception:
        return {}
