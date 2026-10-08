from __future__ import annotations
import json, socket, subprocess, threading, time, urllib.request
from pathlib import Path
from typing import Callable
import psutil
from .chrome_profile_runtime import fleet_available, fleet_request, wait_cdp

DEFAULT_REGISTRY=Path(r'G:\AgentBusProfiles\chrome_profile_registry.json')
DEFAULT_SNAPSHOT=Path(r'G:\AgentBusProfiles\manager_snapshot_cache.json')
DEFAULT_RETENTION_DIR=Path(r'G:\AgentBusProfiles\profile_retention')
CHROME=Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')

def _read_json(path: Path) -> dict:
    try: return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except Exception: return {}

def _cdp_live(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1',int(port)),timeout=0.12): return True
    except OSError: return False

def _profile_pid_map(rows: list[dict]) -> dict[str, int]:
    targets={_normalized_user_data_dir(str(row['user_data_dir'])):str(row['id']) for row in rows}
    matches: dict[str,set[int]]={profile_id:set() for profile_id in targets.values()}
    if not targets:
        return {}
    for proc in psutil.process_iter(['pid','name']):
        try:
            if str(proc.info.get('name') or '').casefold() != 'chrome.exe': continue
            args=[str(arg) for arg in (proc.cmdline() or [])]
            if any(arg.casefold().startswith('--type=') for arg in args): continue
            user_dir=next((arg.split('=',1)[1] for arg in args if arg.casefold().startswith('--user-data-dir=')),None)
            if user_dir is None: continue
            profile_id=targets.get(_normalized_user_data_dir(user_dir))
            if profile_id:
                matches[profile_id].add(int(proc.info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError): continue
    return {profile_id:next(iter(pids)) for profile_id,pids in matches.items() if len(pids)==1}

def _profile_pid(row: dict) -> int | None:
    return _profile_pid_map([row]).get(str(row['id']))

def profile_cards(*, registry_path: Path=DEFAULT_REGISTRY, snapshot_path: Path=DEFAULT_SNAPSHOT,
                  live_probe: Callable[[int],bool]=_cdp_live, pid_resolver: Callable[[dict],int|None]=_profile_pid) -> list[dict]:
    reg=_read_json(Path(registry_path))
    if reg.get('schema') != 'livingos.chrome-profile-registry.v2': raise RuntimeError('CHROME_PROFILE_REGISTRY_SCHEMA')
    rows=list(reg.get('profiles') or [])
    ids=[str(r.get('id') or '') for r in rows]
    if not rows or any(not x for x in ids) or len(set(ids))!=len(ids): raise RuntimeError('CHROME_PROFILE_REGISTRY_INVALID')
    snap=_read_json(Path(snapshot_path)); reality={r.get('id'):r for r in list(snap.get('accounts') or [])}
    live_by_id={str(row['id']):bool(live_probe(int(row['cdp_port']))) for row in rows}
    default_pid_map=_profile_pid_map([row for row in rows if live_by_id[str(row['id'])]]) if pid_resolver is _profile_pid else {}
    out=[]
    for row in rows:
        snap_row=reality.get(row['id'],{}); live=live_by_id[str(row['id'])]; pid=default_pid_map.get(str(row['id'])) if pid_resolver is _profile_pid else (pid_resolver(row) if live else None)
        out.append({**row,'runtime_status':str(snap_row.get('status') or 'USABLE') if live else 'OFFLINE','browser_pid':pid,
                    'worker_chat_count':int(snap_row.get('worker_chat_count') or 0) if live else 0,
                    'retained':bool(snap_row.get('retained')),'bam_generic_eligible':row.get('role')=='managed'})
    return out

def apply_profile_dark_content(port: int) -> int:
    from websockets.sync.client import connect
    with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/json',timeout=1.5) as response:
        targets=json.loads(response.read().decode('utf-8'))
    applied=0
    for target in targets:
        ws_url=str(target.get('webSocketDebuggerUrl') or '')
        if target.get('type')!='page' or not ws_url: continue
        try:
            with connect(ws_url,open_timeout=1.5) as ws:
                ws.send(json.dumps({'id':1,'method':'Emulation.setEmulatedMedia','params':{'features':[{'name':'prefers-color-scheme','value':'dark'}]}})); ws.recv(timeout=1.5)
                ws.send(json.dumps({'id':2,'method':'Emulation.setAutoDarkModeOverride','params':{'enabled':True}})); ws.recv(timeout=1.5)
            applied += 1
        except Exception: continue
    return applied

def _page_urls(port: int) -> list[str]:
    with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/json',timeout=1.5) as response:
        rows=json.loads(response.read().decode('utf-8'))
    return [str(row.get('url') or '') for row in rows if row.get('type')=='page']

def _retention_path(row: dict) -> Path:
    return DEFAULT_RETENTION_DIR / f"{row['id']}.last_pages.json"

def _preferred_retained_url(urls: list[str]) -> str | None:
    keep=[u for u in urls if u.startswith('https://chatgpt.com/') or u=='https://chatgpt.com']
    chats=[u for u in keep if '/c/' in u]
    return (chats or keep or [None])[0]

def _write_retained_url(row: dict, urls: list[str]) -> str | None:
    url=_preferred_retained_url(urls)
    if url:
        DEFAULT_RETENTION_DIR.mkdir(parents=True,exist_ok=True)
        _retention_path(row).write_text(json.dumps({'profile':row['id'],'url':url})+'\n',encoding='utf-8')
    return url

def _read_retained_url(row: dict) -> str | None:
    try: return _preferred_retained_url([str(json.loads(_retention_path(row).read_text(encoding='utf-8')).get('url') or '')])
    except Exception: return None

def _listener_pid_for_port(port: int) -> int | None:
    matches=set()
    for conn in psutil.net_connections(kind='tcp'):
        try:
            if (
                getattr(conn,'pid',None)
                and getattr(getattr(conn,'laddr',None),'port',None)==int(port)
                and str(getattr(conn,'status','')).upper()=='LISTEN'
            ):
                matches.add(int(conn.pid))
        except Exception:
            continue
    if len(matches)>1:
        raise RuntimeError('CHROME_PROFILE_CDP_OWNER_AMBIGUOUS')
    return next(iter(matches),None)


def _assert_exact_managed_runtime(row: dict) -> int:
    expected_dir=_normalized_user_data_dir(str(row['user_data_dir']))
    expected_port=int(row['cdp_port'])
    root_pid=_profile_pid(row)
    if not root_pid:
        raise RuntimeError('CHROME_PROFILE_EXACT_ROOT_IDENTITY_REQUIRED')
    try:
        root=psutil.Process(int(root_pid))
        args=[str(x) for x in (root.cmdline() or [])]
        if str(root.name() or '').casefold()!='chrome.exe':
            raise RuntimeError('CHROME_PROFILE_EXACT_EXECUTABLE_MISMATCH')
        actual_dir=next((x.split('=',1)[1] for x in args if x.casefold().startswith('--user-data-dir=')),None)
        actual_port=next((x.split('=',1)[1] for x in args if x.casefold().startswith('--remote-debugging-port=')),None)
        if actual_dir is None or _normalized_user_data_dir(actual_dir)!=expected_dir:
            raise RuntimeError('CHROME_PROFILE_EXACT_USER_DATA_DIR_MISMATCH')
        if actual_port is None or int(actual_port)!=expected_port:
            raise RuntimeError('CHROME_PROFILE_EXACT_CDP_PORT_MISMATCH')
        family={int(root.pid),*(int(x.pid) for x in root.children(recursive=True))}
    except (psutil.NoSuchProcess,psutil.AccessDenied,OSError) as exc:
        raise RuntimeError('CHROME_PROFILE_EXACT_PROCESS_IDENTITY_UNAVAILABLE') from exc
    listener_pid=_listener_pid_for_port(expected_port)
    if not listener_pid or int(listener_pid) not in family:
        raise RuntimeError('CHROME_PROFILE_CDP_OWNER_NOT_IN_EXACT_MANAGED_TREE')
    return int(root_pid)


def _close_browser_via_cdp(port: int) -> None:
    from websockets.sync.client import connect
    with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version",timeout=1.5) as response:
        meta=json.loads(response.read().decode('utf-8'))
    ws_url=str(meta.get('webSocketDebuggerUrl') or '')
    if not ws_url: raise RuntimeError('CHROME_BROWSER_CDP_TARGET_MISSING')
    with connect(ws_url,open_timeout=1.5) as ws:
        ws.send(json.dumps({'id':1,'method':'Browser.close'}))

def _terminate_profile_process_tree(row: dict) -> None:
    pid=_assert_exact_managed_runtime(row)
    try: root=psutil.Process(int(pid))
    except psutil.NoSuchProcess: return
    members=list(reversed(root.children(recursive=True)))+[root]
    for proc in members:
        try: proc.terminate()
        except (psutil.NoSuchProcess,psutil.AccessDenied): pass
    _,alive=psutil.wait_procs(members,timeout=3)
    for proc in alive:
        try: proc.kill()
        except (psutil.NoSuchProcess,psutil.AccessDenied): pass

def close_profile_runtime(row: dict, *, live_probe=_cdp_live, close_browser=_close_browser_via_cdp, sleep=time.sleep, page_urls=_page_urls) -> dict:
    port=int(row['cdp_port'])
    try: _write_retained_url(row,list(page_urls(port)))
    except Exception: pass
    # Separation (2026-10-08): teardown is an owner action.  Production calls delegate to the
    # fleet owner; callers that inject test seams keep the exact-identity local path below.
    if (live_probe is _cdp_live and close_browser is _close_browser_via_cdp
            and page_urls is _page_urls and fleet_available()):
        response=fleet_request('close',profile=str(row['id']),timeout=45.0)
        if not response.get('ok'):
            raise RuntimeError(str(response.get('error') or 'CHROME_FLEET_CLOSE_FAILED'))
        return dict(response.get('result') or {})
    if not live_probe(port): return {'id':row['id'],'status':'ALREADY_CLOSED','cdp_port':port}
    managed_pid=_assert_exact_managed_runtime(row)
    close_browser(port)
    for _ in range(30):
        sleep(0.1)
        if not live_probe(port): return {'id':row['id'],'status':'CLOSED','cdp_port':port}
    _terminate_profile_process_tree(row)
    for _ in range(20):
        sleep(0.1)
        if not live_probe(port): return {'id':row['id'],'status':'CLOSED','cdp_port':port}
    raise RuntimeError('CHROME_PROFILE_CLOSE_TIMEOUT')

def _managed_pid_alive(pid: int) -> bool:
    try:
        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def _managed_pid_bound(pid: int) -> bool:
    try:
        from .chrome_embed import managed_pid_has_bound_hwnd
        return bool(managed_pid_has_bound_hwnd(int(pid)))
    except Exception:
        return False

def _wait_existing_profile_cdp(port: int, pid: int, *, timeout: float=25.0, sleep=None, live_probe=None, alive=None) -> bool:
    sleep=sleep or time.sleep
    live_probe=live_probe or _cdp_live
    alive=alive or _managed_pid_alive
    deadline=time.monotonic()+max(0.0,float(timeout))
    while time.monotonic()<deadline:
        if live_probe(int(port)):
            return True
        if not alive(int(pid)):
            return False
        sleep(0.1)
    return bool(live_probe(int(port)))

def _wait_profile_cdp(port: int, pid: int, *, timeout: float=25.0, sleep=time.sleep, live_probe=_cdp_live, pid_resolver=_profile_pid) -> bool:
    deadline=time.monotonic()+max(0.0,float(timeout))
    while time.monotonic()<deadline:
        if not _managed_pid_alive(int(pid)):
            return False
        if live_probe(int(port)):
            return True
        sleep(0.1)
    return bool(live_probe(int(port)))


# ``_guard_managed_chrome_pid`` (and the window-hiding it performed) was retired on
# 2026-10-08 together with BAM's window ownership: the workbench no longer hides or
# reparents a Chrome window, so there is nothing left to guard.


_PROFILE_LAUNCH_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LAUNCH_LOCKS_GUARD = threading.Lock()

def _normalized_user_data_dir(value: str) -> str:
    return str(Path(str(value).strip('"')).resolve()).rstrip('\\/').casefold()

def _profile_launch_lock(row: dict) -> threading.Lock:
    key = _normalized_user_data_dir(str(row['user_data_dir']))
    with _PROFILE_LAUNCH_LOCKS_GUARD:
        return _PROFILE_LAUNCH_LOCKS.setdefault(key, threading.Lock())

def _existing_profile_runtime_pids(row: dict) -> list[int]:
    target = _normalized_user_data_dir(str(row['user_data_dir']))
    matches: list[int] = []
    for proc in psutil.process_iter(['pid','name']):
        try:
            if str(proc.info.get('name') or '').casefold() != 'chrome.exe': continue
            raw_args = proc.info.get('cmdline') if isinstance(getattr(proc,'info',None),dict) else None
            if raw_args is None:
                raw_args = proc.cmdline() or []
            args = [str(arg) for arg in raw_args]
            if any(arg.casefold().startswith('--type=') for arg in args): continue
            for arg in args:
                if not arg.casefold().startswith('--user-data-dir='): continue
                if _normalized_user_data_dir(arg.split('=',1)[1]) == target:
                    matches.append(int(proc.info['pid'])); break
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError): continue
    return sorted(set(matches))

def launch_profile(row: dict, url: str | None=None) -> dict:
    with _profile_launch_lock(row):
        launch_url=str(url or _read_retained_url(row) or 'https://chatgpt.com/')
        if _cdp_live(int(row['cdp_port'])):
            return {'id':row['id'],'pid':_profile_pid(row),'cdp_port':int(row['cdp_port']),'already_running':True}
        existing = _existing_profile_runtime_pids(row)
        if len(existing) > 1: raise RuntimeError('CHROME_PROFILE_RUNTIME_AMBIGUOUS')
        if len(existing) == 1:
            pid=existing[0]
            if not _wait_existing_profile_cdp(int(row['cdp_port']),pid):
                raise RuntimeError('CHROME_PROFILE_EXISTING_RUNTIME_CDP_TIMEOUT')
            return {'id':row['id'],'pid':pid,'cdp_port':int(row['cdp_port']),'already_running':True}
        # Separation (2026-10-08): BAM never spawns Chrome.  The fleet owner
        # (rock_man_executor.chrome_fleet_supervisor, task LivingOS-Chrome-Profile-Fleet)
        # is the only spawner and the owner of record; BAM stays a CDP client.  There is
        # deliberately no local-spawn fallback: if the owner is down, this fails loudly
        # instead of silently making the workbench Chrome's owner again.
        if not fleet_available():
            raise RuntimeError('CHROME_FLEET_OWNER_UNAVAILABLE')
        response=fleet_request('ensure',profile=str(row['id']),url=launch_url,timeout=45.0)
        if not response.get('ok'):
            raise RuntimeError(str(response.get('error') or 'CHROME_FLEET_ENSURE_FAILED'))
        result=dict(response.get('result') or {})
        pid=int(result.get('pid') or 0)
        if not pid or not wait_cdp(int(row['cdp_port']),pid,timeout=25.0):
            return {**result,'launch_pending':True}
        return {**result,'launch_pending':False}

