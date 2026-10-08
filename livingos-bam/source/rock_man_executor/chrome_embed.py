from __future__ import annotations
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_MINIMIZE = 0x20000000
WS_MAXIMIZE = 0x01000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_APPWINDOW = 0x00040000
NATIVE_FRAME_STYLE_MASK = WS_CAPTION|WS_THICKFRAME|WS_SYSMENU|WS_MINIMIZEBOX|WS_MAXIMIZEBOX
NATIVE_FRAME_EXSTYLE_MASK = WS_EX_DLGMODALFRAME|WS_EX_WINDOWEDGE|WS_EX_CLIENTEDGE|WS_EX_APPWINDOW
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
_ORIGINAL: dict[int, tuple[int,int,int]] = {}

def _top_level_windows(*, visible_only: bool = True) -> list[tuple[int,int]]:
    rows=[]; callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    @callback_type
    def callback(hwnd,_):
        if user32.GetParent(hwnd): return True
        if visible_only and not user32.IsWindowVisible(hwnd): return True
        pid=wintypes.DWORD(); user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid)); rows.append((int(hwnd),int(pid.value))); return True
    user32.EnumWindows(callback,0); return rows

def _visible_top_level_windows() -> list[tuple[int,int]]:
    return _top_level_windows(visible_only=True)

def _window_class_name(hwnd: int) -> str:
    try:
        buf=ctypes.create_unicode_buffer(256)
        return buf.value if not user32.GetClassNameW(int(hwnd),buf,len(buf)) else str(buf.value)
    except (AttributeError, OSError, ValueError):
        return ""

def browser_hwnd_for_pid(pid: int) -> int | None:
    visible = [(hwnd,owner_pid) for hwnd,owner_pid in _visible_top_level_windows() if int(owner_pid)==int(pid)]
    visible_browser=[hwnd for hwnd,_ in visible if _window_class_name(hwnd)=="Chrome_WidgetWin_1"]
    if visible_browser: return visible_browser[0]
    all_rows=[(hwnd,owner_pid) for hwnd,owner_pid in _top_level_windows(visible_only=False) if int(owner_pid)==int(pid)]
    all_browser=[hwnd for hwnd,_ in all_rows if _window_class_name(hwnd)=="Chrome_WidgetWin_1"]
    if all_browser: return all_browser[0]
    return None

def managed_pid_has_bound_hwnd(pid: int) -> bool:
    for hwnd in tuple(_ORIGINAL):
        owner_pid = wintypes.DWORD()
        if user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(owner_pid)):
            if int(owner_pid.value) == int(pid) and int(user32.GetParent(int(hwnd)) or 0) != 0:
                return True
    return False


def suppress_top_level_browser_for_pid(pid: int) -> int | None:
    primary=browser_hwnd_for_pid(pid)
    candidates=[] if not primary else [int(primary)]
    try:
        candidates.extend(int(hwnd) for hwnd,owner_pid in _top_level_windows(visible_only=False) if int(owner_pid)==int(pid) and int(hwnd) not in candidates)
    except AttributeError:
        pass
    first=None
    for hwnd in candidates:
        if int(user32.GetParent(hwnd) or 0): continue
        user32.ShowWindow(hwnd,SW_HIDE)
        if first is None: first=hwnd
    return first

def embed_hwnd(hwnd: int, parent_hwnd: int, *, show: bool = True) -> None:
    # Separation (2026-10-08): BAM must never own a Chrome window.  Reparenting made the
    # browser window a child of the Qt workbench, so a workbench crash destroyed the window
    # of a still-running Chrome (4 live ChatGPT sessions lost their windows this way).
    if _window_class_name(hwnd) == "Chrome_WidgetWin_1":
        raise RuntimeError('CHROME_EMBED_RETIRED_USE_OVERLAY')
    style=int(user32.GetWindowLongPtrW(hwnd,GWL_STYLE))
    exstyle=int(user32.GetWindowLongPtrW(hwnd,GWL_EXSTYLE))
    parent=int(user32.GetParent(hwnd) or 0)
    _ORIGINAL.setdefault(int(hwnd),(parent,style,exstyle))
    # A Chrome window must never be shown as a top-level transition while BAM owns it.
    user32.ShowWindow(hwnd,SW_HIDE)
    kernel32.SetLastError(0); user32.SetParent(hwnd,parent_hwnd); err=kernel32.GetLastError()
    if err: raise ctypes.WinError(err)
    child_style=(style|WS_CHILD|WS_VISIBLE)&~(WS_POPUP|WS_MINIMIZE|WS_MAXIMIZE|NATIVE_FRAME_STYLE_MASK)
    child_exstyle=exstyle & ~NATIVE_FRAME_EXSTYLE_MASK
    user32.SetWindowLongPtrW(hwnd,GWL_EXSTYLE,child_exstyle)
    user32.SetWindowLongPtrW(hwnd,GWL_STYLE,child_style)
    user32.SetWindowPos(hwnd,0,0,0,1,1,0x0020|0x0004|0x0010)
    if int(user32.GetParent(hwnd) or 0) != int(parent_hwnd):
        raise RuntimeError('CHROME_EMBED_PARENT_NOT_BOUND')

def resize_embedded(hwnd: int, width: int, height: int) -> None:
    user32.MoveWindow(hwnd,0,0,max(1,int(width)),max(1,int(height)),True)

def fill_embedded(hwnd: int, parent_hwnd: int) -> None:
    rect = wintypes.RECT()
    if not user32.GetClientRect(parent_hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    user32.MoveWindow(hwnd, 0, 0, max(1, int(rect.right-rect.left)), max(1, int(rect.bottom-rect.top)), True)

def hide_hwnd(hwnd: int) -> None: user32.ShowWindow(hwnd,SW_HIDE)
def show_hwnd(hwnd: int) -> None: user32.ShowWindow(hwnd,SW_SHOW)


# --- Ownership-free overlay (separation 2026-10-08) -------------------------
# BAM places a Chrome window over its own viewport by geometry + z-order only.  The window
# stays a top-level, unowned window: a BAM crash can no longer destroy, hide or reparent it.

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
STANDARD_TOPLEVEL_STYLE = (WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX
                           | WS_MAXIMIZEBOX | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)

user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]


def client_screen_rect(anchor_hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetClientRect(int(anchor_hwnd), ctypes.byref(rect)):
        raise ctypes.WinError()
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(int(anchor_hwnd), ctypes.byref(origin)):
        raise ctypes.WinError()
    return (int(origin.x), int(origin.y),
            max(1, int(rect.right - rect.left)), max(1, int(rect.bottom - rect.top)))


def unparent_if_embedded(hwnd: int) -> bool:
    """Release a window embedded by the retired SetParent path and restore top-level style."""
    if not int(user32.GetParent(int(hwnd)) or 0):
        return False
    user32.ShowWindow(int(hwnd), SW_HIDE)
    kernel32.SetLastError(0)
    user32.SetParent(int(hwnd), 0)
    error = kernel32.GetLastError()
    if error:
        raise ctypes.WinError(error)
    user32.SetWindowLongPtrW(int(hwnd), GWL_STYLE, STANDARD_TOPLEVEL_STYLE)
    user32.SetWindowLongPtrW(int(hwnd), GWL_EXSTYLE, WS_EX_APPWINDOW | WS_EX_WINDOWEDGE)
    user32.SetWindowPos(int(hwnd), 0, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    _ORIGINAL.pop(int(hwnd), None)
    user32.ShowWindow(int(hwnd), SW_SHOW)
    return True


def attach_overlay(hwnd: int, anchor_hwnd: int, *, show: bool = True) -> None:
    """Place a Chrome window over the anchor viewport without taking ownership of it."""
    unparent_if_embedded(int(hwnd))
    x, y, width, height = client_screen_rect(int(anchor_hwnd))
    user32.SetWindowPos(int(hwnd), 0, x, y, width, height, SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    user32.SetWindowPos(int(anchor_hwnd), int(hwnd), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    user32.ShowWindow(int(hwnd), SW_SHOW if show else SW_HIDE)


def move_overlay(hwnd: int, anchor_hwnd: int, *, restack: bool = True) -> None:
    x, y, width, height = client_screen_rect(int(anchor_hwnd))
    user32.SetWindowPos(int(hwnd), 0, x, y, width, height, SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    if restack:
        user32.SetWindowPos(int(anchor_hwnd), int(hwnd), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def detach_overlay(hwnd: int, *, show: bool = False) -> None:
    """Stop overlaying.  Chrome keeps its own window; only visibility is changed here."""
    if not int(hwnd):
        return
    user32.ShowWindow(int(hwnd), SW_SHOW if show else SW_HIDE)


def restore_hwnd(hwnd: int, *, show: bool = True) -> None:
    if not hwnd: return
    saved=_ORIGINAL.pop(int(hwnd),None)
    if saved is None:
        parent=0; style=int(user32.GetWindowLongPtrW(hwnd,GWL_STYLE)); exstyle=int(user32.GetWindowLongPtrW(hwnd,GWL_EXSTYLE))
    elif len(saved)==2:
        parent,style=saved; exstyle=int(user32.GetWindowLongPtrW(hwnd,GWL_EXSTYLE))
    else:
        parent,style,exstyle=saved
    user32.ShowWindow(hwnd,SW_HIDE)
    kernel32.SetLastError(0); user32.SetParent(hwnd,parent)
    restore_style = style if show else (style & ~WS_VISIBLE)
    user32.SetWindowLongPtrW(hwnd,GWL_STYLE,restore_style)
    user32.SetWindowLongPtrW(hwnd,GWL_EXSTYLE,exstyle)
    if hasattr(user32,'SetWindowPos'):
        user32.SetWindowPos(hwnd,0,0,0,1,1,0x0020|0x0004|0x0010)
    if show:
        user32.ShowWindow(hwnd,SW_RESTORE)
    else:
        user32.ShowWindow(hwnd,SW_HIDE)
