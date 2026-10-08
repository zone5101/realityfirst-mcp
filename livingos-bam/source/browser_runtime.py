from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE = ROOT / "_state" / "bam" / "chrome_stable_profile"
LOCK = ROOT / "_state" / "bam" / "browser_runtime.lock"
# Separation (2026-10-08): the endpoint is owned by the Chrome fleet owner
# (rock_man_executor.chrome_fleet_supervisor, profile "bamruntime").  BAM is a CDP client:
# it may only ASK the owner to ensure/reveal/close this browser, never launch one itself.
CDP_PORT = int(os.environ.get("BAM_BROWSER_CDP_PORT") or 9223)
BAM_RUNTIME_PROFILE_ID = os.environ.get("BAM_BROWSER_RUNTIME_PROFILE") or "bamruntime"
ENDPOINT = f"http://127.0.0.1:{CDP_PORT}"
LAUNCH_ARGS = (
    f"--user-data-dir={PROFILE}", f"--remote-debugging-port={CDP_PORT}",
    "--no-first-run", "--no-default-browser-check", "--disable-sync",
    "--disable-features=SigninPromo,ForceSigninFlowInProfilePicker",
    "--disable-session-crashed-bubble", "--hide-crash-restore-bubble","about:blank",
)


def _http_ready() -> bool:
    try:
        with urlopen(ENDPOINT + "/json/version", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _google_signer_subject(subject: str) -> bool:
    rdns = {part.strip().casefold() for part in str(subject or "").split(",")}
    return "cn=google llc" in rdns and "o=google llc" in rdns


def _chrome_binary_trusted() -> bool:
    if os.name != "nt" or not CHROME.is_file():
        return False
    quoted = str(CHROME).replace("'", "''")
    script = (
        f"$s=Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
        "[pscustomobject]@{Status=[string]$s.Status;SignerSubject=[string]$s.SignerCertificate.Subject} "
        "| ConvertTo-Json -Compress"
    )
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0 or not done.stdout.strip():
        return False
    try:
        value = json.loads(done.stdout)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(value, dict)
        and str(value.get("Status") or "") == "Valid"
        and _google_signer_subject(str(value.get("SignerSubject") or ""))
    )


def _matching_runtime_owner_pids(processes: list[dict[str, object]]) -> set[int]:
    expected_exe = str(CHROME.resolve()).casefold()
    expected_profile_flag = ("--user-data-dir=" + str(PROFILE.resolve())).casefold()
    expected_port_flag = f"--remote-debugging-port={CDP_PORT}".casefold()
    matches: set[int] = set()
    for process in processes:
        executable = str(process.get("ExecutablePath") or "").casefold()
        command = str(process.get("CommandLine") or "").replace('"', '').casefold()
        if executable != expected_exe or "--type=" in command:
            continue
        port_match = re.search(re.escape(expected_port_flag) + r"(?=\s|$)", command)
        profile_match = re.search(re.escape(expected_profile_flag) + r"(?=\s|$)", command)
        if not (port_match and profile_match):
            continue
        try:
            matches.add(int(process.get("ProcessId")))
        except (TypeError, ValueError):
            continue
    return matches


def _runtime_identity_matches(processes: list[dict[str, object]]) -> bool:
    return len(_matching_runtime_owner_pids(processes)) == 1


def _runtime_binding_matches(processes: list[dict[str, object]], listeners: list[dict[str, object]]) -> bool:
    owners = _matching_runtime_owner_pids(processes)
    if len(owners) != 1 or len(listeners) != 1:
        return False
    listener = listeners[0]
    if str(listener.get("LocalAddress") or "") != "127.0.0.1":
        return False
    try:
        listener_pid = int(listener.get("OwningProcess"))
    except (TypeError, ValueError):
        return False
    return owners == {listener_pid}


def _runtime_owner_present() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$processes=@(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Select-Object ProcessId,ExecutablePath,CommandLine); "
        f"$listeners=@(Get-NetTCPConnection -State Listen -LocalPort {CDP_PORT} -ErrorAction SilentlyContinue | "
        "Select-Object LocalAddress,OwningProcess); "
        "[pscustomobject]@{Processes=$processes;Listeners=$listeners} | ConvertTo-Json -Compress -Depth 5"
    )
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0 or not done.stdout.strip():
        return False
    try:
        value = json.loads(done.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict):
        return False
    raw_processes = value.get("Processes") or []
    raw_listeners = value.get("Listeners") or []
    processes = [raw_processes] if isinstance(raw_processes, dict) else list(raw_processes)
    listeners = [raw_listeners] if isinstance(raw_listeners, dict) else list(raw_listeners)
    return _runtime_binding_matches(
        [dict(row) for row in processes if isinstance(row, dict)],
        [dict(row) for row in listeners if isinstance(row, dict)],
    )


def ready() -> bool:
    return _chrome_binary_trusted() and _http_ready() and _runtime_owner_present()


def _mutex_name() -> str:
    identity = hashlib.sha256(str(PROFILE.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    return "Local\\BAM_BROWSER_RUNTIME_" + identity


@contextmanager
def _platform_lock(timeout_seconds: float):
    timeout_seconds = max(0.0, float(timeout_seconds))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, _mutex_name())
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        wait_ms = min(0xFFFFFFFE, int(timeout_seconds * 1000))
        result = kernel32.WaitForSingleObject(handle, wait_ms)
        if result not in (0x00000000, 0x00000080):
            kernel32.CloseHandle(handle)
            if result == 0x00000102:
                raise RuntimeError("BAM_BROWSER_RUNTIME_BUSY")
            raise OSError(ctypes.get_last_error(), f"WaitForSingleObject failed: {result}")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("a+b")
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("BAM_BROWSER_RUNTIME_BUSY")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def runtime_session_lock(timeout_seconds: float = 10.0):
    """Serialize launch and anchor use across BAM processes."""
    with _platform_lock(timeout_seconds):
        yield


def _fleet_owner_ensure() -> bool:
    """Ask the independent fleet owner to ensure the BAM runtime endpoint.

    Separation (2026-10-08): BAM never launches Chrome any more.  The endpoint on
    ``CDP_PORT`` is owned by ``rock_man_executor.chrome_fleet_supervisor``; this module only
    requests it.  When the owner is unavailable the caller gets a loud failure instead of a
    locally owned browser.
    """
    try:
        from rock_man_executor.chrome_profile_runtime import fleet_available, fleet_request
    except Exception:
        return False
    if not fleet_available():
        return False
    try:
        response = fleet_request("ensure", profile=BAM_RUNTIME_PROFILE_ID, timeout=45.0)
    except Exception:
        return False
    return bool(response.get("ok"))


def _ensure_runtime_unlocked() -> str:
    if not _http_ready():
        _fleet_owner_ensure()
        for _ in range(40):
            if _http_ready():
                break
            time.sleep(0.25)
    if not _http_ready():
        raise RuntimeError("BAM_BROWSER_RUNTIME_ENDPOINT_UNAVAILABLE")
    if not _runtime_owner_present():
        raise RuntimeError("BAM_BROWSER_RUNTIME_IDENTITY_MISMATCH")
    return ENDPOINT


def ensure_runtime() -> str:
    with runtime_session_lock():
        return _ensure_runtime_unlocked()


@contextmanager
def acquire_runtime_session(timeout_seconds: float = 10.0):
    """Own the single persistent anchor for one bounded browser action."""
    with runtime_session_lock(timeout_seconds):
        yield _ensure_runtime_unlocked()


def acquire_target(browser):
    if not browser.contexts:
        raise RuntimeError("BAM_BROWSER_CONTEXT_MISSING")
    context = browser.contexts[0]
    pages = list(context.pages)
    if not pages:
        raise RuntimeError("BAM_BROWSER_ANCHOR_MISSING")
    anchor = pages[0]
    for page in pages[1:]:
        try:
            page.close()
        except Exception:
            pass
    return anchor


def park(page) -> None:
    page.goto("about:blank", wait_until="commit", timeout=5000)
