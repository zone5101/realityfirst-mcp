from __future__ import annotations

import ctypes
from ctypes import wintypes
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psutil

from websockets.sync.client import connect as websocket_connect
from typing import Any

from PySide6.QtCore import QObject, QDir, QEvent, QRegularExpression, QSettings, Qt, QSize, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QCursor,
    QIcon,
    QKeySequence,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .capability_registry import capability_spec, capabilities_by_family, human_capabilities
from .external_capability_contract import external_service_hands
from .plugin_dock import plugin_cards
from .chrome_embed import browser_hwnd_for_pid, attach_overlay, move_overlay, detach_overlay
from .chrome_profiles_ui import profile_cards, launch_profile, apply_profile_dark_content
from .chrome_profile_tabs import ProfileTabsModel, choose_visible_profile
from .profile_open_route import publish_profile_result
from .chrome_profile_reality import read_profile_reality
from .reality_typewriter import RealityTimelineModel


CONSOLE_BIRTH_LOG = Path(r"G:\AgentBusProfiles\livingos_console_births.jsonl")
CONSOLE_GUARD_MARKERS = (
    "g:\\livingos",
    "g:\\agentbusprofiles",
    "black armor",
    "_rockman",
    "rock_man",
    "livingos",
    "desktop-commander",
    "@wonderwhy-er",
)
# The console-birth log used to be append-only with no cap (it reached 752 MB on
# 2026-10-08 while growing ~1 KB/s). Rotate by size, gzip the rotated segments and keep
# a bounded history, so this never needs manual intervention again.
CONSOLE_BIRTH_LOG_MAX_BYTES = 64 * 1024 * 1024
CONSOLE_BIRTH_LOG_KEEP_ARCHIVES = 2
CONSOLE_BIRTH_LOG_HOUSEKEEPING_SECONDS = 300.0
_CONSOLE_BIRTH_HOUSEKEEPING_LOCK = threading.Lock()
_CONSOLE_BIRTH_LAST_HOUSEKEEPING = 0.0
_CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE = False
_CONSOLE_BIRTH_SEGMENT_SEQ = 0


def _console_birth_append(record: dict) -> None:
    """Append one console-birth record, rotating the log so it cannot grow without bound.

    The size check runs immediately before every append (the handle is opened and closed
    per write), so the live file cannot exceed CONSOLE_BIRTH_LOG_MAX_BYTES by more than a
    single record. Old segments are renamed beside the live file and only the newest
    CONSOLE_BIRTH_LOG_KEEP_ARCHIVES of them are kept.
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        if CONSOLE_BIRTH_LOG.exists() and CONSOLE_BIRTH_LOG.stat().st_size >= CONSOLE_BIRTH_LOG_MAX_BYTES:
            archive = _console_birth_next_archive_path()
            os.replace(CONSOLE_BIRTH_LOG, archive)
            segments = sorted(
                CONSOLE_BIRTH_LOG.parent.glob(f"{CONSOLE_BIRTH_LOG.stem}.*{CONSOLE_BIRTH_LOG.suffix}"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for stale in segments[CONSOLE_BIRTH_LOG_KEEP_ARCHIVES:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
            _console_birth_housekeeping_async()
    except OSError:
        pass
    with CONSOLE_BIRTH_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _console_birth_next_archive_path() -> Path:
    """Unique archive name. The sequence suffix prevents same-second collisions, which
    otherwise make os.replace hit an archive that housekeeping still has open."""
    global _CONSOLE_BIRTH_SEGMENT_SEQ
    _CONSOLE_BIRTH_SEGMENT_SEQ += 1
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return CONSOLE_BIRTH_LOG.with_name(
        f"{CONSOLE_BIRTH_LOG.stem}.{stamp}-{_CONSOLE_BIRTH_SEGMENT_SEQ:04d}{CONSOLE_BIRTH_LOG.suffix}"
    )


def _console_birth_compress_segment(segment: Path) -> None:
    """Gzip one rotated segment, verify the archive round-trip, then drop the plain file.

    The plain file is removed only after the .gz reads back with the exact same byte
    count, so a failed compression can never lose a segment.
    """
    target = segment.with_name(segment.name + ".gz")
    if target.exists() or not segment.exists():
        return
    partial = segment.with_name(segment.name + ".gz.part")
    chunk = 4 * 1024 * 1024
    try:
        with segment.open("rb") as source, gzip.open(partial, "wb", compresslevel=6) as sink:
            shutil.copyfileobj(source, sink, chunk)
        restored = 0
        with gzip.open(partial, "rb") as verify_handle:
            while True:
                block = verify_handle.read(chunk)
                if not block:
                    break
                restored += len(block)
        if restored != segment.stat().st_size:
            partial.unlink(missing_ok=True)
            return
        os.replace(partial, target)
        segment.unlink(missing_ok=True)
    except OSError:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def _console_birth_housekeeping(force: bool = False) -> None:
    """Compress rotated segments and prune the history. Safe to call from any thread."""
    global _CONSOLE_BIRTH_LAST_HOUSEKEEPING
    now = time.time()
    if not force and now - _CONSOLE_BIRTH_LAST_HOUSEKEEPING < CONSOLE_BIRTH_LOG_HOUSEKEEPING_SECONDS:
        return
    if not _CONSOLE_BIRTH_HOUSEKEEPING_LOCK.acquire(blocking=False):
        return
    try:
        _CONSOLE_BIRTH_LAST_HOUSEKEEPING = now
        parent = CONSOLE_BIRTH_LOG.parent
        stem, suffix = CONSOLE_BIRTH_LOG.stem, CONSOLE_BIRTH_LOG.suffix
        for segment in sorted(parent.glob(f"{stem}.*{suffix}"), key=lambda path: path.stat().st_mtime):
            _console_birth_compress_segment(segment)
        history = sorted(
            list(parent.glob(f"{stem}.*{suffix}")) + list(parent.glob(f"{stem}.*{suffix}.gz")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in history[CONSOLE_BIRTH_LOG_KEEP_ARCHIVES:]:
            for _ in range(3):
                try:
                    stale.unlink()
                    break
                except OSError:
                    time.sleep(0.15)
    finally:
        _CONSOLE_BIRTH_HOUSEKEEPING_LOCK.release()


def _console_birth_housekeeping_async() -> None:
    """Run housekeeping off the polling thread; at most one run at a time."""
    global _CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE
    if _CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE:
        return
    _CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE = True

    def _run() -> None:
        global _CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE
        try:
            _console_birth_housekeeping(force=True)
        finally:
            _CONSOLE_BIRTH_HOUSEKEEPING_ACTIVE = False

    threading.Thread(target=_run, name="console-birth-housekeeping", daemon=True).start()


def _console_guard_hide_pid(pid: int) -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    windows: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) == int(pid) and user32.IsWindowVisible(hwnd):
            windows.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    for hwnd in windows:
        user32.ShowWindow(hwnd, 0)


def _console_guard_loop() -> None:
    seen: set[tuple[int, float]] = set()
    CONSOLE_BIRTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            for proc in psutil.process_iter(["pid", "create_time"]):
                try:
                    pid = int(proc.info["pid"])
                    create_time = float(proc.info.get("create_time") or 0.0)
                    key = (pid, create_time)
                    if key in seen:
                        continue
                    seen.add(key)
                    name = str(proc.name() or "").lower()
                    if name not in {"powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe"}:
                        continue
                    cmd = " ".join(str(x) for x in (proc.cmdline() or []))
                    ppid = int(proc.ppid() or 0)
                    parent_name = ""
                    parent_cmd = ""
                    try:
                        parent = psutil.Process(ppid)
                        parent_name = parent.name()
                        parent_cmd = " ".join(parent.cmdline())
                    except Exception:
                        pass
                    haystack = f"{cmd}\n{parent_name}\n{parent_cmd}".lower()
                    livingos_owned = any(marker in haystack for marker in CONSOLE_GUARD_MARKERS)
                    record = {
                        "observed_at": time.time(),
                        "pid": pid,
                        "name": name,
                        "ppid": ppid,
                        "cmdline": cmd,
                        "parent_name": parent_name,
                        "parent_cmdline": parent_cmd,
                        "livingos_owned": livingos_owned,
                    }
                    _console_birth_append(record)
                    if livingos_owned:
                        _console_guard_hide_pid(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            if len(seen) > 4000:
                seen = set(sorted(seen, key=lambda item: item[1])[-1000:])
        except Exception:
            pass
        if time.time() - _CONSOLE_BIRTH_LAST_HOUSEKEEPING >= CONSOLE_BIRTH_LOG_HOUSEKEEPING_SECONDS:
            _console_birth_housekeeping_async()
        # Sleep mode (2026-10-08, audit round 2): console hiding is not latency critical;
        # a 1.5s cadence keeps the same guarantee with ~12x less psutil enumeration burn.
        time.sleep(1.5)



def _external_hand_runtime_status(hand, cards) -> str:
    tokens = {str(hand.service_id).casefold(), str(hand.label).casefold()}
    matches = [card for card in cards if any(token and token in (str(card.get("id", "")) + " " + str(card.get("name", ""))).casefold() for token in tokens)]
    if any(card.get("status") == "CONNECTED" for card in matches):
        return "CONNECTED"
    if matches:
        return str(matches[0].get("status") or "NOT_OBSERVED")
    return "NOT_OBSERVED"


from .workbench_session import open_human_workbench
from .bam_execution_projection import read_bam_execution_snapshot
from .mcp_control import BamMcpProcessController
from .mcp_runtime import BamMcpRuntimeProfile, adaptive_runtime_status_projection
from .mcp_boundary import McpSourceSystem

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))
from _SharedUI.global_typography import DEFAULT_SIZE as GLOBAL_FONT_BASE_SIZE, read_state as read_global_typography, normalize_family as normalize_global_font_family, normalize_size as normalize_global_font_size

APP_NAME = "BlackArmorWorkbench"
ORG_NAME = "LivingOS"
LAYOUT_VERSION = 25
BAM_PROFILE_UI_REQUEST_DIR = Path(os.environ.get("BAM_PROFILE_UI_REQUEST_DIR") or r"G:\AgentBusProfiles\_bam_ui_profile_requests")
REGISTRY_PATH = Path(r"G:\AgentBusProfiles\chrome_profile_registry.json")
PROFILE_ROOT = Path(r"G:\AgentBusProfiles")
BROWSER_REQUEST_IDLE_INTERVAL_MS = 1000
BROWSER_REQUEST_ACTIVE_INTERVAL_MS = 100

BG = "#070A0F"
PANEL = "#0B0F15"
PANEL_2 = "#10161E"
BORDER = "#1B2530"
TEXT = "#EDF2F7"
MUTED = "#7F8A98"
CYAN = "#5AA9E6"
BLUE = "#5AA9E6"
GREEN = "#55C58A"
RED = "#E96C75"
AMBER = "#D7A85C"
PURPLE = "#9D89C9"


_STATE_VI = {
    "PASS": "đạt",
    "FAIL": "không đạt",
    "ACTIVE": "đang hoạt động",
    "PENDING": "đang chờ",
    "LEASED": "worker đang giữ",
    "ACKNOWLEDGED": "đã có biên nhận",
    "CANCELLED": "đã hủy",
    "LEASE_EXPIRED_REQUIRES_RECOVERY": "cần khôi phục có quyền",
    "EXPIRED_REQUIRES_REASSIGNMENT": "cần gán host lại",
    "WORKER_DIED_OR_FAILED": "worker chết/lỗi",
    "UNKNOWN": "chưa rõ",
}


def _vi_state(value: Any) -> str:
    text = str(value or "UNKNOWN")
    return _STATE_VI.get(text, text.replace("_", " ").lower())


TOOL_PRESENTATION: dict[str, tuple[str, str, str]] = {
    "filesystem.inspect": (
        "Inspect folders",
        "See what files and folders exist without changing them.",
        "Inspecting workspace…",
    ),
    "filesystem.read_text": (
        "Read files",
        "Read text safely from files inside the allowed workspace.",
        "Reading file…",
    ),
    "filesystem.write_text": (
        "Write files safely",
        "Create or update text files inside the permitted workspace.",
        "Writing file…",
    ),
    "filesystem.hash": (
        "Verify file identity",
        "Calculate a stable fingerprint for a workspace file.",
        "Calculating file hash…",
    ),
    "filesystem.snapshot": (
        "Create safety snapshot",
        "Capture an immutable recovery point for workspace files.",
        "Creating snapshot…",
    ),
    "filesystem.restore": (
        "Restore snapshot",
        "Restore workspace files from a verified safety snapshot.",
        "Restoring snapshot…",
    ),
    "filesystem.atomic_replace": (
        "Replace file atomically",
        "Replace one file only when its current fingerprint matches.",
        "Replacing file safely…",
    ),
    "archive.pack": (
        "Create ZIP archive",
        "Package workspace files into a controlled ZIP archive.",
        "Creating ZIP archive…",
    ),
    "archive.unpack": (
        "Extract ZIP archive",
        "Extract a ZIP archive into a new controlled destination.",
        "Extracting ZIP archive…",
    ),
    "artifact.stage": (
        "Stage verified artifact",
        "Prepare an artifact after its independent Reality admission.",
        "Staging artifact…",
    ),
    "media.audio.measure_level": (
        "Measure audio level",
        "Measure technical RMS and peak levels from supported audio.",
        "Measuring audio level…",
    ),
    "media.audio.measure_pitch": (
        "Measure pitch",
        "Estimate dominant periodic frequency from supported audio.",
        "Measuring pitch…",
    ),
    "media.audio.convert": (
        "Convert media",
        "Convert supported audio through the fixed bounded toolchain.",
        "Converting media…",
    ),
    "media.inspect": (
        "Inspect media",
        "Read technical metadata from audio or video without changing it.",
        "Inspecting media…",
    ),
    "media.generate_tone_wav": (
        "Create test tone",
        "Generate a bounded WAV tone and verify the resulting file.",
        "Creating test tone…",
    ),
    "gui.text.replace_and_save": (
        "Desktop control",
        "Perform one bounded approved text edit in a bound desktop window.",
        "Controlling desktop…",
    ),
    "ui.qml_validate": (
        "Validate UI",
        "Check supported QML interface source with fixed validation tools.",
        "Validating UI…",
    ),
    "process.python_compile": (
        "Check Python syntax",
        "Verify one Python source file can compile before execution.",
        "Checking Python syntax…",
    ),
    "process.python_test": (
        "Run Python tests",
        "Run a bounded approved Python test and return its result.",
        "Running Python tests…",
    ),
    "process.python_isolated_root_arg": (
        "Run isolated Python tool",
        "Run one bounded Python tool with an isolated workspace argument.",
        "Running bounded Python tool…",
    ),
    "process.python_compile_tree": (
        "Check Python source tree",
        "Compile a bounded Python source tree to find syntax errors.",
        "Checking Python source tree…",
    ),
    "motor.compose": (
        "Compose tool plans",
        "Combine already-open motor plans without executing them.",
        "Composing tool plans…",
    ),
}


SKILL_GROUP_DEFINITIONS = (
    (
        "file_operations",
        "File Operations",
        "Read, inspect, modify and recover workspace files safely.",
        tuple(key for key in TOOL_PRESENTATION if key.startswith("filesystem.")),
    ),
    (
        "archive_handling",
        "Archive Handling",
        "Pack and unpack controlled ZIP archives.",
        ("archive.pack", "archive.unpack"),
    ),
    (
        "artifact_handling",
        "Artifact Handling",
        "Prepare artifacts through required Reality admission boundaries.",
        ("artifact.stage",),
    ),
    (
        "media_inspection",
        "Media Inspection",
        "Read technical metadata from supported audio and video.",
        ("media.inspect",),
    ),
    (
        "audio_analysis",
        "Audio Analysis",
        "Measure level and pitch facts from supported audio.",
        ("media.audio.measure_level", "media.audio.measure_pitch"),
    ),
    (
        "media_conversion",
        "Media Creation & Conversion",
        "Create or convert media through bounded installed providers.",
        ("media.audio.convert", "media.generate_tone_wav"),
    ),
    (
        "python_execution",
        "Python Execution",
        "Compile and test Python safely as one tool family among many.",
        tuple(key for key in TOOL_PRESENTATION if key.startswith("process.python_")),
    ),
    (
        "ui_validation",
        "UI Validation",
        "Validate supported QML interface files before runtime.",
        ("ui.qml_validate",),
    ),
    (
        "desktop_control",
        "Desktop Control",
        "Perform bounded approved actions in a bound desktop application.",
        ("gui.text.replace_and_save",),
    ),
    (
        "tool_composition",
        "Tool Composition",
        "Combine existing motor plans through their dedicated surface.",
        ("motor.compose",),
    ),
)


def tool_presentation(capability_id: str, fallback: str = "") -> tuple[str, str, str]:
    return TOOL_PRESENTATION.get(
        capability_id,
        (
            capability_id.replace("_", " ").replace(".", " · ").title(),
            fallback or "Installed BAM capability.",
            "Working…",
        ),
    )


def installed_skill_groups() -> tuple[dict[str, Any], ...]:
    """Derive the human skill map from the current live registry."""
    specs = tuple(human_capabilities())
    by_family: dict[str, list[str]] = {}
    for spec in specs:
        by_family.setdefault(str(spec.family or "other"), []).append(spec.capability_id)

    family_copy = {
        "media": ("Media", "Audio/video/image execution tools."),
        "structured_data": ("Structured Data", "JSON/CSV/data transformation and inspection."),
        "filesystem": ("Files & Workspace", "Inspect, read, write, hash and recover files."),
        "spreadsheets": ("Spreadsheets", "Workbook and tabular automation."),
        "documents": ("Documents", "Document creation, extraction and conversion."),
        "process": ("Python & Process", "Compile, test and run bounded processes."),
        "archive": ("Archives", "Package and extract controlled archives."),
        "teacher": ("Teacher Hand", "Instruction-only external Teacher execution."),
        "web_http": ("Web / HTTP", "Bounded web and HTTP technical execution."),
        "gui": ("Desktop Control", "Bounded desktop interaction tools."),
        "ui": ("UI Validation", "Interface validation and UI technical tools."),
        "artifact": ("Artifacts", "Verified artifact staging and handling."),
        "meta_tool": ("Tool Composition", "Compose existing bounded tool plans."),
    }
    rows: list[dict[str, Any]] = []
    for family in sorted(by_family, key=lambda key: (-len(by_family[key]), key)):
        ids = tuple(sorted(by_family[family]))
        name, description = family_copy.get(
            family,
            (family.replace("_", " ").title(), "Installed tools reported by the live BAM registry."),
        )
        rows.append({
            "group_id": "family:" + family,
            "name": name,
            "description": description,
            "capability_ids": ids,
        })
    rows.append({
        "group_id": "external_services",
        "name": "External Services",
        "description": "Connect service hands through one semantic BAM contract.",
        "capability_ids": tuple(),
        "external_hands": external_service_hands(),
    })
    return tuple(rows)

class ExecutionSignals(QObject):
    running = Signal(str)
    finished = Signal(object, float)
    failed = Signal(str, float)


class PythonSyntaxHighlighter(QSyntaxHighlighter):
    """Muted Python syntax palette for the workspace editor."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._add_rule(
            r"\b(?:False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b",
            "#7eb6d8",
            bold=True,
        )
        self._add_rule(r"\b(?:self|super)\b", "#c29bd8")
        self._add_rule(r"\b\d+(?:\.\d+)?\b", "#d7ae78")
        self._add_rule(r"@[A-Za-z_][A-Za-z0-9_.]*", "#91b98c")
        self._add_rule(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", "#d6c184")
        self._add_rule(r"(?:\"[^\"\\]*(?:\\.[^\"\\]*)*\"|'[^'\\]*(?:\\.[^'\\]*)*')", "#9fbe8f")
        self._add_rule(r"#[^\n]*", "#667789", italic=True)

    def _add_rule(
        self, pattern: str, color: str, *, bold: bool = False, italic: bool = False
    ) -> None:
        text_format = QTextCharFormat()
        text_format.setForeground(QColor(color))
        if bold:
            text_format.setFontWeight(QFont.DemiBold)
        text_format.setFontItalic(italic)
        self._rules.append((QRegularExpression(pattern), text_format))

    def highlightBlock(self, text: str) -> None:
        for expression, text_format in self._rules:
            match_iterator = expression.globalMatch(text)
            while match_iterator.hasNext():
                match = match_iterator.next()
                start = match.capturedStart(1) if match.lastCapturedIndex() else match.capturedStart()
                length = match.capturedLength(1) if match.lastCapturedIndex() else match.capturedLength()
                self.setFormat(start, length, text_format)


class ResponsiveImageLabel(QLabel):
    """Keep character art present without imposing a fixed dock width."""

    def __init__(self, source: Path | QPixmap, crop: tuple[int, int, int, int] | None = None) -> None:
        super().__init__()
        pixmap = source if isinstance(source, QPixmap) else QPixmap(str(source))
        self._source = pixmap.copy(*crop) if crop is not None else pixmap
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(120, 160)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def sizeHint(self) -> QSize:
        return QSize(220, 300)

    def resizeEvent(self, event) -> None:
        if not self._source.isNull():
            self.setPixmap(
                self._source.scaled(
                    max(1, self.width()),
                    max(1, self.height()),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        super().resizeEvent(event)


class WorkspacePane(QWidget):
    def __init__(self, owner: "BlackArmorWorkbench") -> None:
        super().__init__()
        self.owner = owner
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workspaceTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda _: self.owner._sync_active_editor())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

        welcome = QTextEdit()
        welcome.setObjectName("workspaceWelcome")
        welcome.setReadOnly(True)
        welcome.setHtml(
            "<h1>BLACK ARMOR WORKSPACE</h1>"
            "<p>Workspace-first control surface.</p>"
            "<p>Open a workspace, then double-click a file or select a capability.</p>"
        )
        self.tabs.addTab(welcome, "Welcome")

    def open_text_file(self, path: Path) -> None:
        path = path.resolve()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if widget.property("filePath") == str(path):
                self.tabs.setCurrentIndex(i)
                return

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self.owner.log(f"Cannot open non-text file: {path}")
            return
        except OSError as exc:
            self.owner.log(f"Open failed: {type(exc).__name__}: {exc}")
            return

        editor = QPlainTextEdit()
        editor.setObjectName("codeEditor")
        editor.setProperty("filePath", str(path))
        editor.setPlainText(content)
        editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        editor_font = QFont(self.owner._global_font_family)
        editor_font.setPointSize(self.owner._global_font_size)
        editor_font.setStyleHint(QFont.Monospace)
        editor_font.setFixedPitch(True)
        editor.setFont(editor_font)
        editor.setTabStopDistance(editor.fontMetrics().horizontalAdvance(" ") * 4)
        editor._syntax_highlighter = PythonSyntaxHighlighter(editor.document())
        editor.document().modificationChanged.connect(
            lambda modified, e=editor: self._mark_modified(e, modified)
        )
        index = self.tabs.addTab(editor, path.name)
        self.tabs.setCurrentIndex(index)

    def save_current(self) -> bool:
        widget = self.tabs.currentWidget()
        if not isinstance(widget, QPlainTextEdit):
            return False
        raw = str(widget.property("filePath") or "")
        if not raw:
            return False
        path = Path(raw)
        try:
            path.write_text(widget.toPlainText(), encoding="utf-8", newline="\n")
        except OSError as exc:
            self.owner.log(f"Save failed: {type(exc).__name__}: {exc}")
            return False
        widget.document().setModified(False)
        self.owner.log(f"Saved: {path}")
        return True

    def current_file(self) -> Path | None:
        widget = self.tabs.currentWidget()
        if not isinstance(widget, QPlainTextEdit):
            return None
        raw = str(widget.property("filePath") or "")
        return Path(raw) if raw else None

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, QPlainTextEdit) and widget.document().isModified():
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "Close this editor without saving?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.tabs.removeTab(index)
        widget.deleteLater()

    def _mark_modified(self, editor: QPlainTextEdit, modified: bool) -> None:
        index = self.tabs.indexOf(editor)
        if index < 0:
            return
        path = Path(str(editor.property("filePath") or ""))
        title = path.name if path.name else "Editor"
        self.tabs.setTabText(index, title + (" •" if modified else ""))


class CapabilityPanel(QWidget):
    def __init__(self, owner: "BlackArmorWorkbench") -> None:
        super().__init__()
        self.owner = owner
        self.spec = None
        self.inputs: dict[str, QWidget] = {}
        self._tool_items: dict[str, QListWidgetItem] = {}
        self._tool_states: dict[str, str] = {}
        self._active_capability_id = ""
        self._pulse_phase = False
        self._execution_signals: ExecutionSignals | None = None
        self._execution_thread: threading.Thread | None = None

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(850)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        self._terminal_timer = QTimer(self)
        self._terminal_timer.setSingleShot(True)
        self._terminal_timer.setInterval(2200)
        self._terminal_timer.timeout.connect(self._restore_completed_tool)

        self.setObjectName("commandPanel")

        eyebrow = QLabel("LIVE TOOL CABINET")
        eyebrow.setObjectName("sectionLabel")

        self.title = QLabel("Choose a tool")
        self.title.setObjectName("commandTitle")

        self.meta = QLabel(
            "Installed tools from the live BAM registry. Select one to see what it does."
        )
        self.meta.setObjectName("commandMeta")
        self.meta.setWordWrap(True)

        self.search = QLineEdit()
        self.search.setObjectName("capabilitySearch")
        self.search.setPlaceholderText("Search tools by purpose...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_capabilities)

        self.list = QListWidget()
        self.list.setObjectName("capabilityList")
        self.list.setMaximumHeight(270)
        self.list.setWordWrap(True)
        self.list.setSpacing(3)
        self.list.currentItemChanged.connect(self._selected)

        self.form_host = QWidget()
        self.form_host.setObjectName("parameterSurface")
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 8, 0, 8)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(10)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        scroll = QScrollArea()
        scroll.setObjectName("parameterScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_host)

        self.run_button = QPushButton("RUN")
        self.run_button.setObjectName("primaryRunButton")
        self.run_button.clicked.connect(self._execute)
        self.run_button.setEnabled(False)
        self.run_button.setMinimumHeight(46)

        self.last_result = QLabel("IDLE · Tools are ready")
        self.last_result.setObjectName("lastResult")
        self.last_result.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(eyebrow)
        layout.addWidget(self.title)
        layout.addWidget(self.meta)
        layout.addSpacing(4)
        layout.addWidget(self.search)
        layout.addWidget(self.list)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.run_button)
        layout.addWidget(self.last_result)

        self.reload()

    def reload(self) -> None:
        self.list.clear()
        self._tool_items.clear()
        self._tool_states.clear()
        for spec in human_capabilities():
            name, purpose, action = tool_presentation(
                spec.capability_id,
                spec.description,
            )
            item = QListWidgetItem(f"{name}\n{purpose}")
            item.setSizeHint(QSize(0, 58))
            item.setToolTip(
                f"{name}\n{purpose}\n\nTechnical ID · {spec.capability_id}\n"
                f"Family · {spec.family}"
            )
            item.setData(Qt.UserRole, spec.capability_id)
            item.setData(Qt.UserRole + 1, spec.family)
            item.setData(Qt.UserRole + 2, name)
            item.setData(Qt.UserRole + 3, purpose)
            item.setData(Qt.UserRole + 4, action)
            self.list.addItem(item)
            self._tool_items[spec.capability_id] = item
            self._tool_states[spec.capability_id] = "IDLE"

    def _filter_capabilities(self, value: str) -> None:
        query = value.strip().casefold()
        for row in range(self.list.count()):
            item = self.list.item(row)
            haystack = (
                item.text()
                + " "
                + str(item.data(Qt.UserRole + 1) or "")
                + " "
                + str(item.data(Qt.UserRole) or "")
            ).casefold()
            item.setHidden(bool(query) and query not in haystack)

    def _clear_form(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs.clear()

    def _selected(self, current: QListWidgetItem | None, _: QListWidgetItem | None) -> None:
        self._clear_form()

        if current is None:
            self.spec = None
            self.title.setText("Choose a tool")
            self.run_button.setEnabled(False)
            return

        capability_id = str(current.data(Qt.UserRole))
        self.spec = capability_spec(capability_id)
        name, purpose, _ = tool_presentation(
            capability_id,
            self.spec.description,
        )

        self.title.setText(name)
        self.meta.setText(
            f"{purpose}\n"
            f"Technical · {self.spec.capability_id}\n"
            f"{self.spec.family.upper()}  ·  Replay {self.spec.replay_policy}  ·  "
            f"Confirmation {self.spec.required_confirmation}"
        )

        for key, kind in dict(self.spec.input_schema).items():
            if key == "root":
                continue

            if kind == "text":
                widget = QTextEdit()
                widget.setMinimumHeight(92)
                widget.setMaximumHeight(150)
            else:
                widget = QLineEdit()
                if kind.startswith("optional_"):
                    widget.setPlaceholderText("Optional")

            self.form.addRow(key, widget)
            self.inputs[key] = widget

        supported = self.spec.implementation_surface == "execution_controller_adapter"
        reality_blocked = "software_reality_verified_binding" in self.spec.reality_hooks

        self.run_button.setEnabled(
            supported
            and not reality_blocked
            and not self._active_capability_id
        )

        if reality_blocked:
            self.run_button.setText("REALITY PATH REQUIRED")
        elif not supported:
            self.run_button.setText("DEDICATED SURFACE REQUIRED")
        else:
            self.run_button.setText("RUN")

    def _execute(self) -> None:
        if self.spec is None:
            return
        if self.owner.session is None:
            QMessageBox.information(self, "Workspace required", "Open a workspace first.")
            return

        params: dict[str, Any] = {}
        for key, widget in self.inputs.items():
            if isinstance(widget, QTextEdit):
                value = widget.toPlainText()
            else:
                value = widget.text().strip()
            if value == "":
                continue
            if key == "timeout_seconds":
                try:
                    value = int(value)
                except ValueError:
                    QMessageBox.warning(self, "Invalid input", "timeout_seconds must be an integer.")
                    return
            params[key] = value

        confirmation = False
        if self.spec.required_confirmation != "NONE":
            answer = QMessageBox.question(
                self,
                "Confirm execution",
                f"Execute {self.spec.capability_id} in the active workspace?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._set_tool_state(
                    self.spec.capability_id,
                    "REFUSED",
                    "Confirmation declined",
                )
                self.last_result.setText("REFUSED · Confirmation declined")
                return
            confirmation = True

        execute_kwargs: dict[str, Any] = {
            "capability_id": self.spec.capability_id,
            "parameters": params,
            "confirmation": confirmation,
        }
        if self.owner.storage_root is not None:
            execute_kwargs["storage_root"] = self.owner.storage_root

        capability_id = self.spec.capability_id
        session = self.owner.session
        if session is None:
            return

        signals = ExecutionSignals(self)
        signals.running.connect(self._execution_started)
        signals.finished.connect(self._execution_finished)
        signals.failed.connect(self._execution_failed)
        self._execution_signals = signals
        self.run_button.setEnabled(False)

        def execute_in_background() -> None:
            started = time.monotonic()
            signals.running.emit(capability_id)
            try:
                result = session.execute(**execute_kwargs)
            except Exception as exc:
                signals.failed.emit(
                    f"{type(exc).__name__}: {exc}",
                    time.monotonic() - started,
                )
                return
            signals.finished.emit(result, time.monotonic() - started)

        self._execution_thread = threading.Thread(
            target=execute_in_background,
            name=f"bam-tool-{capability_id}",
            daemon=True,
        )
        self._execution_thread.start()

    def _execution_started(self, capability_id: str) -> None:
        self._set_tool_state(capability_id, "WORKING")
        name, _, action = tool_presentation(capability_id)
        self.last_result.setText(f"WORKING · {name}\n{action}")
        self.owner.log(f"{capability_id} -> WORKING")

    def _refresh_reality_after_execution(self) -> None:
        panel = getattr(self.owner, "browser_profiles_panel", None)
        if panel is not None:
            panel.reload()

    def _execution_finished(self, result: Any, duration: float) -> None:
        capability_id = str(result.capability_id)
        state = str(result.state)
        if state == "COMPLETED":
            self._set_tool_state(capability_id, "COMPLETED", duration=duration)
            self.last_result.setText(
                f"COMPLETED · {duration:.2f} s\nExecution {result.execution_id}"
            )
            self._terminal_timer.start()
        else:
            reason = str(dict(result.receipt).get("raw_error") or state)
            self._set_tool_state(capability_id, "FAILED", reason, duration)
            self.last_result.setText(f"{state} · {self._short_reason(reason)}")
        self.owner.log(
            f"{capability_id} -> {state}  execution={result.execution_id}"
        )
        self.owner.show_receipt(result.receipt)
        self._refresh_reality_after_execution()
        self._execution_thread = None
        self._execution_signals = None
        self._update_run_availability()

    def _execution_failed(self, reason: str, duration: float) -> None:
        capability_id = self._active_capability_id or (
            self.spec.capability_id if self.spec is not None else ""
        )
        if capability_id:
            self._set_tool_state(capability_id, "REFUSED", reason, duration)
        self.last_result.setText(f"REFUSED · {self._short_reason(reason)}")
        self.owner.log(f"{capability_id} refused: {reason}")
        self._refresh_reality_after_execution()
        self._execution_thread = None
        self._execution_signals = None
        self._update_run_availability()

    def _set_tool_state(
        self,
        capability_id: str,
        state: str,
        detail: str = "",
        duration: float | None = None,
    ) -> None:
        item = self._tool_items.get(capability_id)
        if item is None:
            return
        normalized = state.strip().upper()
        self._tool_states[capability_id] = normalized
        name = str(item.data(Qt.UserRole + 2) or capability_id)
        purpose = str(item.data(Qt.UserRole + 3) or "")
        action = str(item.data(Qt.UserRole + 4) or "Working…")

        if normalized == "WORKING":
            if self._active_capability_id and self._active_capability_id != capability_id:
                self._set_tool_state(self._active_capability_id, "IDLE")
            self._active_capability_id = capability_id
            item.setText(f"{name}\n{action}")
            self.list.clearSelection()
            self._pulse_phase = False
            self._pulse_tick()
            self._pulse_timer.start()
            return

        if self._active_capability_id == capability_id:
            self._active_capability_id = ""
            self._pulse_timer.stop()

        if normalized == "COMPLETED":
            suffix = f" · {duration:.2f} s" if duration is not None else ""
            item.setText(f"{name}\nCompleted{suffix}")
            item.setBackground(QBrush(QColor("#153025")))
            item.setForeground(QBrush(QColor("#DDF4E7")))
        elif normalized in {"FAILED", "REFUSED"}:
            label = "Refused" if normalized == "REFUSED" else "Failed"
            item.setText(f"{name}\n{label} · {self._short_reason(detail)}")
            item.setBackground(QBrush(QColor("#321A20")))
            item.setForeground(QBrush(QColor("#F0C7CB")))
        else:
            self._tool_states[capability_id] = "IDLE"
            item.setText(f"{name}\n{purpose}")
            item.setBackground(QBrush())
            item.setForeground(QBrush())

    def _pulse_tick(self) -> None:
        capability_id = self._active_capability_id
        item = self._tool_items.get(capability_id)
        if item is None or self._tool_states.get(capability_id) != "WORKING":
            self._pulse_timer.stop()
            return
        self._pulse_phase = not self._pulse_phase
        item.setBackground(
            QBrush(QColor("#172A36" if self._pulse_phase else "#11202A"))
        )
        item.setForeground(QBrush(QColor("#EDF5FA")))

    def _restore_completed_tool(self) -> None:
        for capability_id, state in tuple(self._tool_states.items()):
            if state == "COMPLETED":
                self._set_tool_state(capability_id, "IDLE")

    def _update_run_availability(self) -> None:
        if self.spec is None or self._active_capability_id:
            self.run_button.setEnabled(False)
            return
        supported = self.spec.implementation_surface == "execution_controller_adapter"
        reality_blocked = "software_reality_verified_binding" in self.spec.reality_hooks
        self.run_button.setEnabled(supported and not reality_blocked)

    @staticmethod
    def _short_reason(reason: str) -> str:
        clean = " ".join(str(reason or "Unavailable").split())
        return clean if len(clean) <= 86 else clean[:83] + "…"


class SkillCatalogPanel(QWidget):
    """Human-purpose view derived from the live BAM capability registry."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("skillCatalogPanel")

        self.groups: tuple[dict[str, Any], ...] = ()

        title = QLabel("SKILL LIBRARY")
        title.setObjectName("sectionLabel")
        self.summary = QLabel("Reading installed BAM tools…")
        self.summary.setObjectName("skillSummary")

        self.search = QLineEdit()
        self.search.setObjectName("skillSearch")
        self.search.setPlaceholderText("Search kinds of work…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search)

        self.list = QListWidget()
        self.list.setObjectName("skillList")
        self.list.setWordWrap(True)
        self.list.setSpacing(4)
        self.list.currentItemChanged.connect(self._selected)

        self.detail = QTextEdit()
        self.detail.setObjectName("skillDetail")
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText(
            "Select a skill to see its installed tools and technical boundaries."
        )

        refresh = QPushButton("Refresh from registry")
        refresh.setObjectName("skillRefresh")
        refresh.clicked.connect(self.reload)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)
        layout.addWidget(title)
        layout.addWidget(self.summary)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 2)
        layout.addWidget(self.detail, 3)
        layout.addWidget(refresh)

        self.reload()

    def reload(self, checked: bool = False) -> None:
        del checked
        self.groups = installed_skill_groups()
        tool_count = sum(len(group["capability_ids"]) for group in self.groups)
        self.summary.setText(
            f"{len(self.groups)} skills  ·  {tool_count} installed tools  ·  live registry"
        )
        self._render(self.groups)

    def _search(self, value: str) -> None:
        query = value.strip().casefold()
        if not query:
            self._render(self.groups)
            return
        matches = []
        for group in self.groups:
            capability_text = " ".join(group["capability_ids"])
            haystack = (
                f"{group['name']} {group['description']} {capability_text}"
            ).casefold()
            if query in haystack:
                matches.append(group)
        self._render(tuple(matches))

    def _render(self, groups) -> None:
        selected_id = ""
        current = self.list.currentItem()
        if current is not None:
            selected_id = str(current.data(Qt.UserRole) or "")
        self.list.clear()
        selected_row = -1
        for row, group in enumerate(groups):
            count = len(group["capability_ids"])
            item = QListWidgetItem(
                f"{group['name']}\n{group['description']}\n{count} "
                f"{'tool' if count == 1 else 'tools'}"
            )
            item.setSizeHint(QSize(0, 76))
            item.setData(Qt.UserRole, group["group_id"])
            item.setToolTip(
                f"{group['name']}\n{group['description']}\n"
                f"Installed tools: {count}"
            )
            self.list.addItem(item)
            if group["group_id"] == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.list.setCurrentRow(selected_row)

    def _selected(self, current: QListWidgetItem | None, _: QListWidgetItem | None) -> None:
        if current is None:
            self.detail.clear()
            return
        group_id = str(current.data(Qt.UserRole))
        group = next(
            (item for item in self.groups if item["group_id"] == group_id),
            None,
        )
        if group is None:
            self.detail.clear()
            return

        lines = [
            group["name"],
            group["description"],
            "",
            "TECHNICAL DETAILS",
        ]
        if group_id == "external_services":
            lines = [group["name"], group["description"], "", "AVAILABLE CONNECTIONS"]
            runtime_cards = plugin_cards(Path.home() / ".black_armor_workbench")
            for hand in group["external_hands"]:
                operations = ", ".join(hand.example_operations)
                runtime_status = _external_hand_runtime_status(hand, runtime_cards)
                lines.extend(("", hand.label, hand.purpose,
                              f"Status · {runtime_status.replace('_', ' ').title()}",
                              f"Hand · {hand.semantic_prefix}.*",
                              f"Examples · {operations}"))
            lines.extend(("", "Connect a provider to activate a service. Unconnected services stay visible so the user knows what BAM can be extended to use."))
            self.detail.setPlainText("\n".join(lines))
            return
        for capability_id in group["capability_ids"]:
            spec = capability_spec(capability_id)
            name, purpose, _ = tool_presentation(capability_id, spec.description)
            if "software_reality_verified_binding" in spec.reality_hooks:
                availability = "Reality admission required"
            elif spec.implementation_surface != "execution_controller_adapter":
                availability = "Dedicated surface required"
            else:
                availability = "Available through BAM execution"
            lines.extend((
                "",
                name,
                purpose,
                f"ID · {capability_id}",
                f"Status · {availability}",
                f"Confirmation · {spec.required_confirmation}",
                "Reality · " + ", ".join(spec.reality_hooks),
            ))
        self.detail.setPlainText("\n".join(lines))


class ChromeViewport(QWidget):
    resized = Signal()

    def resizeEvent(self, event) -> None:
        self.resized.emit()
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.resized.emit)


class ChromeBrowserHost(QWidget):
    profile_selected = Signal(str)
    profile_open_failed = Signal(str, str)
    profile_open_finished = Signal(str)

    def __init__(self, owner: "BlackArmorWorkbench") -> None:
        super().__init__(owner)
        self.owner = owner
        self._hwnds: dict[str, int] = {}
        self._cards: dict[str, dict] = {}
        self._active_id = ""
        self._model: ProfileTabsModel | None = None
        self._last_embed_geometry: dict[int, tuple[int, int]] = {}
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(16)
        self._resize_timer.timeout.connect(self._resize_active)
        self.setObjectName("chromeBrowserHost")

        self.profile_tabs = QTabBar(self)
        self.profile_tabs.setObjectName("chromeProfileTabs")
        self.profile_tabs.setExpanding(False)
        self.profile_tabs.setUsesScrollButtons(True)
        self.profile_tabs.setElideMode(Qt.ElideNone)
        self.profile_tabs.setTabsClosable(False)
        self.profile_tabs.currentChanged.connect(self._tab_changed)
        self.profile_tabs.tabBarClicked.connect(self._request_profile_open)
        self.profile_open_failed.connect(self._show_profile_open_failure)
        self.profile_open_finished.connect(lambda profile: self._opening_profiles.discard(profile))
        self._opening_profiles = set()
        self.profile_tabs.setToolTip("Bấm tab để mở profile qua BAM; giữ nguyên dữ liệu và phiên đăng nhập.")

        self.viewport = ChromeViewport(self)
        self.viewport.setObjectName("chromeViewport")
        self.viewport.setAttribute(Qt.WA_NativeWindow, True)
        self.viewport.resized.connect(self._schedule_resize)
        self.placeholder = QLabel("Chọn profile; Chrome đang chạy sẽ hiện nguyên trạng ở đây", self.viewport)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setCursor(Qt.PointingHandCursor)
        self.placeholder.installEventFilter(self)
        self.viewport.installEventFilter(self)
        viewport_layout = QVBoxLayout(self.viewport)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.addWidget(self.placeholder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.profile_tabs)
        layout.addWidget(self.viewport, 1)
        self.set_profiles(profile_cards())

    def set_profiles(self, cards: list[dict]) -> None:
        previous = self._active_id
        self._model = ProfileTabsModel(cards)
        self._cards = {str(card["id"]): dict(card) for card in cards}
        self.profile_tabs.blockSignals(True)
        while self.profile_tabs.count():
            self.profile_tabs.removeTab(0)
        for card in cards:
            profile_id = str(card["id"])
            display_name = str(card.get("display_name") or profile_id)
            index = self.profile_tabs.addTab(display_name)
            self.profile_tabs.setTabData(index, profile_id)
            state = "Đang chạy" if card.get("browser_pid") else "Đã tắt"
            self.profile_tabs.setTabToolTip(index, f"{display_name} · {profile_id} · {state}")
        active = previous if previous in self._cards else self._model.active_id
        self._active_id = active
        self.profile_tabs.setCurrentIndex(self._index_for(active))
        self.profile_tabs.blockSignals(False)
        self._prebind_running_profiles(cards)
        active = choose_visible_profile(cards, active, self._hwnds.keys())
        self._active_id = active
        self.profile_tabs.setCurrentIndex(self._index_for(active))
        if active in self._hwnds:
            self.placeholder.hide()
            ctypes.windll.user32.ShowWindow(self._hwnds[active], 5)
            self._resize_active()
        else:
            active_card = self._cards.get(active, {})
            if active_card.get("browser_pid"):
                self.placeholder.setText(f"{active} · chọn profile để hiển thị Chrome hiện có")
            else:
                self.placeholder.setText(f"{active} đã tắt · Bấm vào đây để mở lại qua BAM")
            self.placeholder.show()

    def _hwnd_matches_profile(self, profile_id: str, hwnd: int) -> bool:
        card = self._cards.get(str(profile_id), {})
        expected_pid = int(card.get("browser_pid") or 0)
        if not expected_pid or not hwnd:
            return False
        owner_pid = wintypes.DWORD()
        if not ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(owner_pid)):
            return False
        if int(owner_pid.value) != expected_pid:
            return False
        # Separation (2026-10-08): a valid profile window is a TOP-LEVEL (unowned) Chrome
        # window placed over the viewport.  Workbench ownership of the window is now the
        # defect condition, not the acceptance test (SetParent made BAM failures destroy
        # live ChatGPT windows).
        return int(ctypes.windll.user32.GetParent(int(hwnd)) or 0) == 0

    def _prebind_running_profiles(self, cards: list[dict]) -> None:
        for profile_id, hwnd in tuple(self._hwnds.items()):
            if not self._hwnd_matches_profile(profile_id, hwnd):
                self._hwnds.pop(profile_id, None)
        for card in cards:
            profile_id = str(card.get("id") or "")
            pid = int(card.get("browser_pid") or 0)
            if not profile_id or not pid or profile_id in self._hwnds:
                continue
            hwnd = browser_hwnd_for_pid(pid)
            if not hwnd:
                continue
            try:
                attach_overlay(hwnd, int(self.viewport.winId()), show=False)
            except Exception:
                # Overlay placement must never take the workbench down with it.
                continue
            self._hwnds[profile_id] = hwnd

    def _index_for(self, profile_id: str) -> int:
        for index in range(self.profile_tabs.count()):
            if str(self.profile_tabs.tabData(index) or "") == profile_id:
                return index
        return -1

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (watched is self.placeholder or watched is self.viewport) and not self.placeholder.isHidden():
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self._request_profile_open(self._index_for(self._active_id))
                return True
        return super().eventFilter(watched, event)

    def _request_profile_open(self, index: int) -> None:
        profile_id = str(self.profile_tabs.tabData(index) or "")
        if profile_id not in self._cards or profile_id in self._opening_profiles:
            return
        hwnd = self._hwnds.get(profile_id)
        if hwnd and self._hwnd_matches_profile(profile_id, hwnd):
            return
        self._opening_profiles.add(profile_id)
        self.placeholder.setText(f"{profile_id} · đang gửi yêu cầu mở qua BAM…")
        def run():
            try:
                from .profile_open_route import open_profile_via_bam, wait_profile_result
                launch = open_profile_via_bam(profile_id)
                wait_profile_result(profile_id, launch)
            except Exception as exc:
                self.profile_open_failed.emit(profile_id, str(exc))
            finally:
                self.profile_open_finished.emit(profile_id)
        threading.Thread(target=run, daemon=True, name=f"bam-profile-open-{profile_id}").start()

    def _show_profile_open_failure(self, profile_id: str, error: str) -> None:
        if self._active_id == profile_id:
            self.placeholder.setText(f"{profile_id} · BAM mở thất bại: {error} · Bấm vào đây để thử lại")

    def _tab_changed(self, index: int) -> None:
        if index < 0:
            return
        profile_id = str(self.profile_tabs.tabData(index) or "")
        if profile_id:
            self.select_profile(profile_id)

    def select_profile(self, profile_id: str) -> dict:
        if self._model is None:
            raise RuntimeError("CHROME_PROFILE_TABS_NOT_READY")
        card = self._model.select(profile_id)
        previous_id = self._active_id
        if previous_id == profile_id and profile_id in self._hwnds:
            self.profile_selected.emit(profile_id)
            self.owner.on_browser_profile_selected(profile_id)
            return card
        previous_hwnd = self._hwnds.get(previous_id)
        if previous_hwnd and previous_id != profile_id:
            ctypes.windll.user32.ShowWindow(previous_hwnd, 0)
        self._active_id = profile_id
        index = self._index_for(profile_id)
        if index >= 0 and self.profile_tabs.currentIndex() != index:
            self.profile_tabs.blockSignals(True)
            self.profile_tabs.setCurrentIndex(index)
            self.profile_tabs.blockSignals(False)
        hwnd = self._hwnds.get(profile_id)
        if hwnd and not self._hwnd_matches_profile(profile_id, hwnd):
            self._hwnds.pop(profile_id, None)
            hwnd = None
        if hwnd:
            self.placeholder.hide()
            self._resize_active()
            ctypes.windll.user32.ShowWindow(hwnd, 5)
        elif card.get("browser_pid"):
            self.attach_profile(card)
        else:
            self.placeholder.setText(f"{profile_id} đã tắt · Bấm vào đây để mở lại qua BAM")
            self.placeholder.show()
        self.profile_selected.emit(profile_id)
        self.owner.on_browser_profile_selected(profile_id)
        return card

    def attach_profile(self, card: dict) -> bool:
        profile_id = str(card.get("id") or "")
        pid = int(card.get("browser_pid") or 0)
        hwnd = self._hwnds.get(profile_id)
        if hwnd and not self._hwnd_matches_profile(profile_id, hwnd):
            self._hwnds.pop(profile_id, None)
            hwnd = None
        hwnd = hwnd or browser_hwnd_for_pid(pid)
        if not hwnd:
            return False
        for other_id, other_hwnd in self._hwnds.items():
            if other_id != profile_id:
                ctypes.windll.user32.ShowWindow(other_hwnd, 0)
        if profile_id not in self._hwnds:
            try:
                attach_overlay(hwnd, int(self.viewport.winId()))
            except Exception:
                return False
            self._hwnds[profile_id] = hwnd
        self._cards[profile_id] = dict(card)
        if self._model is not None:
            self._model.set_cards(self._cards.values())
        self._active_id = profile_id
        index = self._index_for(profile_id)
        if index >= 0 and self.profile_tabs.currentIndex() != index:
            self.profile_tabs.blockSignals(True)
            self.profile_tabs.setCurrentIndex(index)
            self.profile_tabs.blockSignals(False)
        self.placeholder.hide()
        ctypes.windll.user32.ShowWindow(hwnd, 5)
        self._resize_active()
        port=int(card.get('cdp_port') or 0)
        if port:
            threading.Thread(target=apply_profile_dark_content,args=(port,),name=f'bam-dark-{profile_id}',daemon=True).start()
        return True

    def _schedule_resize(self) -> None:
        if not self._resize_timer.isActive():
            self._resize_timer.start()

    def _resize_active(self) -> None:
        hwnd = self._hwnds.get(self._active_id)
        if not hwnd:
            return
        width = max(1, int(self.viewport.width()))
        height = max(1, int(self.viewport.height()))
        geometry = (width, height)
        if self._last_embed_geometry.get(int(hwnd)) == geometry:
            return
        try:
            move_overlay(hwnd, int(self.viewport.winId()))
        except Exception:
            return
        self._last_embed_geometry[int(hwnd)] = geometry

    def restore_all(self, *, show: bool = False) -> None:
        errors = []
        for profile_id, hwnd in tuple(self._hwnds.items()):
            try:
                detach_overlay(hwnd, show=show)
            except Exception as exc:
                errors.append(f"{profile_id}: {type(exc).__name__}: {exc}")
            finally:
                self._hwnds.pop(profile_id, None)
        if errors:
            raise RuntimeError("CHROME_RESTORE_ALL_FAILED: " + " | ".join(errors))



class ChromeProfilesPanel(QWidget):
    """Profile inventory plus verified agent Reality timeline; never launches on selection."""
    def __init__(self, owner: "BlackArmorWorkbench") -> None:
        super().__init__(owner)
        self.owner = owner
        self.cards: list[dict[str, Any]] = []
        self._reality_profile_id = ""
        self._reality_signature = ""
        self._reality_model = RealityTimelineModel(chars_per_tick=1, max_queue=64)
        self._selection_guard = False
        self.setObjectName("chromeProfilesPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)
        title = QLabel("HỒ SƠ CHROME")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)
        self.summary = QLabel("Đang đọc hồ sơ Chrome…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.list = QListWidget()
        self.list.setObjectName("chromeProfilesList")
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)

        reality_title = QLabel("HOẠT ĐỘNG THỰC TẾ")
        reality_title.setObjectName("sectionLabel")
        layout.addWidget(reality_title)
        self.reality_timeline = QTextEdit()
        self.reality_timeline.setObjectName("profileRealityTimeline")
        self.reality_timeline.setReadOnly(True)
        self.reality_timeline.setMinimumHeight(180)
        layout.addWidget(self.reality_timeline, 2)

        actions = QGridLayout()
        self.refresh_button = QPushButton("Làm mới")
        self.refresh_button.setToolTip("Đọc lại Reality do BAM quản lý; không khởi chạy hoặc sửa profile.")
        self.refresh_button.clicked.connect(self.reload)
        actions.addWidget(self.refresh_button, 0, 0)
        projection_note = QLabel("Chỉ đọc · mở, đổi tên và tạo profile phải đi qua BAM/Manager.")
        projection_note.setWordWrap(True)
        actions.addWidget(projection_note, 0, 1)
        layout.addLayout(actions)

        # Reality refresh is action/state driven: init, manual refresh, selection,
        # and operation-completion paths call reload()/refresh_reality() directly.
        self.reload()

    def reload(self, checked: bool=False) -> None:
        del checked
        selected = self._reality_profile_id
        try:
            self.cards = profile_cards()
        except Exception as exc:
            self.cards = []
            self.summary.setText(f"Profile registry unavailable: {type(exc).__name__}: {exc}")
        if self.cards:
            self.owner.browser_host.set_profiles(self.cards)
        self.list.blockSignals(True)
        self.list.clear()
        selected_row = 0
        for row, card in enumerate(self.cards):
            state = "Đang chạy" if card.get("browser_pid") else "Đã tắt"
            display_name = str(card.get("display_name") or card["id"])
            raw_role = str(card.get("role") or "unknown").upper()
            role = "Được bảo vệ" if raw_role == "PROTECTED" else ("BAM quản lý" if raw_role == "MANAGED" else "Chưa phân loại")
            retained = "Được giữ lại" if card.get("retained") else "Chờ ghi nhận"
            item = QListWidgetItem(f"{display_name}  ·  {state}  ·  {role}\n{card['id']}  ·  {retained}  ·  {card.get('worker_chat_count',0)} cuộc chat")
            item.setData(Qt.UserRole, card['id'])
            item.setSizeHint(QSize(0, 58))
            self.list.addItem(item)
            if card['id'] == selected:
                selected_row = row
        self.list.blockSignals(False)
        running = sum(1 for card in self.cards if card.get('browser_pid'))
        self.summary.setText(f"{len(self.cards)} hồ sơ · {running} đang chạy · {len(self.cards)-running} đã tắt")
        if self.list.count():
            self.list.setCurrentRow(selected_row)
        self.refresh_reality()

    def update_card(self, card: dict) -> None:
        profile_id = str(card.get("id") or "")
        if not profile_id:
            return
        updated = dict(card)
        for index, existing in enumerate(self.cards):
            if str(existing.get("id") or "") == profile_id:
                self.cards[index] = updated
                break
        else:
            self.cards.append(updated)
        for row in range(self.list.count()):
            item = self.list.item(row)
            if str(item.data(Qt.UserRole) or "") != profile_id:
                continue
            state = "Đang chạy" if updated.get("browser_pid") else "Đã tắt"
            display_name = str(updated.get("display_name") or profile_id)
            raw_role = str(updated.get("role") or "unknown").upper()
            role = "Được bảo vệ" if raw_role == "PROTECTED" else ("BAM quản lý" if raw_role == "MANAGED" else "Chưa phân loại")
            retained = "Được giữ lại" if updated.get("retained") else "Chờ ghi nhận"
            item.setText(f"{display_name}  ·  {state}  ·  {role}\n{profile_id}  ·  {retained}  ·  {updated.get('worker_chat_count',0)} cuộc chat")
            item.setToolTip(f"{display_name} · {profile_id} · {state}")
            break
        running = sum(1 for row in self.cards if row.get("browser_pid"))
        self.summary.setText(f"{len(self.cards)} hồ sơ · {running} đang chạy · {len(self.cards)-running} đã tắt")
        if self._reality_profile_id == profile_id:
            self._render_detail()

    def _card(self) -> dict | None:
        item = self.list.currentItem()
        if item is None:
            return None
        profile_id = str(item.data(Qt.UserRole) or '')
        return next((card for card in self.cards if card.get('id') == profile_id), None)

    def select_profile(self, profile_id: str) -> None:
        profile_id = str(profile_id or "")
        if profile_id == self._reality_profile_id:
            return
        self._reality_profile_id = profile_id
        for row in range(self.list.count()):
            item = self.list.item(row)
            if str(item.data(Qt.UserRole) or "") == self._reality_profile_id:
                if self.list.currentRow() != row:
                    self._selection_guard = True
                    self.list.setCurrentRow(row)
                    self._selection_guard = False
                break
        if self.isVisible():
            self._render_detail()

    def _selected(self, current, previous) -> None:
        del current, previous
        card = self._card()
        if card is None:
            self.detail.clear()
            return
        self._reality_profile_id = str(card['id'])
        self._render_detail()
        self.refresh_reality()
        browser_visible = self.owner.central_stack.currentWidget() is self.owner.browser_page
        if browser_visible and not self._selection_guard and self.owner.browser_host._active_id != card['id']:
            self.owner.browser_host.select_profile(str(card['id']))

    def _render_detail(self) -> None:
        card = next((row for row in self.cards if row.get('id') == self._reality_profile_id), None)
        if card is None:
            self.detail.clear()
            return
        eligible = "BAM dùng được" if card.get('bam_generic_eligible') else "Được bảo vệ · BAM không tự dùng"
        state = "Đang chạy" if card.get('browser_pid') else "Đã tắt"
        self.detail.setText(f"{card['id']} · {state} · {eligible}")

    def refresh_reality(self, checked: bool=False) -> None:
        del checked
        profile_id = self._reality_profile_id
        if not profile_id:
            return
        events = read_profile_reality(profile_id)[-80:]
        change = self._reality_model.sync(profile_id, events)
        if change != "unchanged":
            if change == "queued":
                self._reality_model.flush()
            self.reality_timeline.setPlainText(self._reality_model.text)
            bar = self.reality_timeline.verticalScrollBar()
            bar.setValue(bar.maximum())


class PluginDockPanel(QWidget):
    """Provider evidence and BAM routing remain separate concepts."""
    def __init__(self, *, storage_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pluginDockPanel")
        self.storage_root = Path(storage_root)
        self.cards = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18,18,18,18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Your connections")
        title.setObjectName("connectionTitle")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setMinimumHeight(34)
        self.refresh_button.setToolTip("Read provider evidence; does not connect or enable a provider.")
        self.refresh_button.clicked.connect(self.reload)
        header.addWidget(title,1)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        self.summary = QLabel()
        self.summary.setObjectName("pluginSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a connection…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(36)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.status_filter = QComboBox()
        self.status_filter.addItem("All provider states", "")
        for state in ("CONNECTED", "AVAILABLE", "INSTALLED", "INSTALLABLE", "NOT_OBSERVED"):
            self.status_filter.addItem(state.replace("_", " ").title(), state)
        self.status_filter.currentIndexChanged.connect(self._filter)
        layout.addWidget(self.status_filter)
        self.list = QListWidget()
        self.list.setObjectName("pluginList")
        self.list.setWordWrap(True)
        self.list.setSpacing(3)
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list,1)
        self.empty = QLabel("No matching connections. Try another name or provider state.")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.selection_title = QLabel("Choose a connection")
        self.selection_title.setObjectName("connectionSelectionTitle")
        self.selection_title.setWordWrap(True)
        layout.addWidget(self.selection_title)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.routing = QLabel()
        layout.addWidget(self.routing)
        self.details_button = QToolButton()
        self.details_button.setText("Provider details")
        self.details_button.setCheckable(True)
        self.details_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.details_button.setArrowType(Qt.RightArrow)
        self.details_button.setMinimumHeight(30)
        layout.addWidget(self.details_button)
        self.detail = QTextEdit()
        self.detail.setObjectName("pluginDetail")
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(120)
        self.detail.hide()
        self.details_button.toggled.connect(self.detail.setVisible)
        self.details_button.toggled.connect(lambda opened: self.details_button.setArrowType(Qt.DownArrow if opened else Qt.RightArrow))
        layout.addWidget(self.detail)
        note = QLabel("Read-only projection. Routing changes require a governed BAM/Manager job.")
        note.setObjectName("connectionFootnote")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.reload()

    def reload(self, checked: bool=False) -> None:
        del checked
        self.cards = plugin_cards(self.storage_root)
        connected = sum(1 for c in self.cards if c['status'] == 'CONNECTED')
        enabled = sum(1 for c in self.cards if c['enabled'])
        self.summary.setText(f"{connected} connected · {enabled} routed through BAM · {len(self.cards)} discovered")
        self._filter()

    def _filter(self, *_args) -> None:
        selected = self._card()
        selected_id = selected['id'] if selected else None
        query = self.search.text().strip().casefold()
        state = self.status_filter.currentData()
        self.list.clear()
        selected_row = 0
        for card in self.cards:
            haystack = ' '.join(str(card.get(k,'')) for k in ('name','provider','description')).casefold()
            if (query and query not in haystack) or (state and card['status'] != state):
                continue
            item = QListWidgetItem(f"{card['name']}\n{card['status'].replace('_',' ').title()} · {card['tool_count']} tools")
            item.setData(Qt.UserRole,card['id'])
            item.setToolTip(f"Provider: {card['provider']}\nEvidence: {card['status']}")
            item.setSizeHint(QSize(0,64))
            self.list.addItem(item)
            if card['id'] == selected_id:
                selected_row = self.list.count()-1
        has_rows = bool(self.list.count())
        self.empty.setVisible(not has_rows)
        self.list.setVisible(has_rows)
        if has_rows:
            self.list.setCurrentRow(selected_row)
        else:
            self._selected(None,None)

    def _card(self):
        current = self.list.currentItem()
        if current is None: return None
        cid = str(current.data(Qt.UserRole) or '')
        return next((c for c in self.cards if c['id'] == cid),None)

    def _selected(self,current,previous) -> None:
        del current,previous
        card = self._card()
        if card is None:
            self.selection_title.setText("Choose a connection")
            self.description.clear()
            self.routing.clear()
            self.detail.clear()
            self.details_button.setEnabled(False)
            return
        self.selection_title.setText(str(card['name']))
        self.description.setText(str(card['description']))
        route = 'Enabled' if card['enabled'] else 'Disabled'
        self.routing.setText(f"BAM routing · {route}")
        self.detail.setPlainText(f"Provider · {card['provider']}\nSource · {card['source_type']}\nReality status · {card['status']}\nTools · {card['tool_count']}\nBAM routing · {route}")
        self.details_button.setEnabled(True)



class McpControlPanel(QWidget):
    """Human-facing MCP transport control. UI owns no request authority."""

    status_changed = Signal(dict)
    probe_succeeded = Signal(dict)
    probe_failed = Signal(str)

    def __init__(self, *, storage_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("mcpPanel")
        self.controller = BamMcpProcessController(storage_root)
        self.storage_root = Path(storage_root).resolve()
        self._probe_thread: threading.Thread | None = None
        self.probe_succeeded.connect(self._probe_completed)
        self.probe_failed.connect(self._probe_failed_ui)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        title = QLabel("MCP CONNECTION")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        purpose = QLabel("RockMan / Task Forge  →  BAM")
        purpose.setObjectName("mcpPurpose")
        layout.addWidget(purpose)

        boundary = QLabel("Transport only · MCP không tạo quyền cho request.")
        boundary.setToolTip(
            "Chỉ bật/tắt cầu MCP và kiểm tra đường kết nối. Quyền phải có sẵn từ external authority trước khi BAM nhận việc."
        )
        boundary.setWordWrap(True)
        boundary.setObjectName("mcpBoundary")
        layout.addWidget(boundary)

        self.status_label = QLabel("MCP · đang kiểm tra runtime…")
        self.status_label.setObjectName("mcpStatusValue")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        coordination_heading = QLabel("ĐIỀU PHỐI RUNTIME")
        coordination_heading.setObjectName("mcpSectionLabel")
        layout.addWidget(coordination_heading)
        chip_grid = QGridLayout()
        chip_grid.setHorizontalSpacing(5)
        chip_grid.setVerticalSpacing(5)
        self.coordination_chips = []
        chip_positions = ((0, 0, 1, 2), (0, 2, 1, 2), (0, 4, 1, 2), (1, 0, 1, 3), (1, 3, 1, 3))
        for text, position in zip(("RUN 0", "DEP 0", "COLL 0", "RES 0", "LEASE 0"), chip_positions):
            chip = QLabel(text)
            chip.setObjectName("coordinationChip")
            chip.setAlignment(Qt.AlignCenter)
            chip.setMinimumHeight(26)
            chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            chip.setStyleSheet("QLabel#coordinationChip { background:#101820; border:1px solid #223142; border-radius:6px; padding:3px 5px; color:#B7C7D9; font-weight:600; }")
            chip_grid.addWidget(chip, *position)
            self.coordination_chips.append(chip)
        layout.addLayout(chip_grid)
        self.coordination_list = QListWidget()
        self.coordination_list.setObjectName("coordinationList")
        self.coordination_list.setWordWrap(True)
        self.coordination_list.setSpacing(3)
        self.coordination_list.setMaximumHeight(150)
        self.coordination_list.setStyleSheet("QListWidget#coordinationList { background:#0B1118; border:1px solid #1B2A38; border-radius:8px; padding:4px; } QListWidget#coordinationList::item { padding:6px 8px; border-bottom:1px solid #15212C; } QListWidget#coordinationList::item:selected { background:#132331; color:#EDF2F7; }")
        self.coordination_list.addItem("No active coordination state")
        layout.addWidget(self.coordination_list)

        connection_heading = QLabel("1 · KẾT NỐI")
        connection_heading.setObjectName("mcpSectionLabel")
        layout.addWidget(connection_heading)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("mcpField")
        self.source_combo.addItems(["RockMan", "Task Forge"])
        self.stream_combo = QComboBox()
        self.stream_combo.setObjectName("mcpField")
        self.stream_combo.setEditable(True)
        self.queue_root_edit = QLineEdit(str(self.storage_root))
        self.queue_root_edit.setObjectName("mcpField")
        self.ledger_edit = QLineEdit(str(self.storage_root / "authority_ledger.sqlite3"))
        self.ledger_edit.setObjectName("mcpField")
        self.port_spin = QSpinBox()
        self.port_spin.setObjectName("mcpField")
        self.port_spin.setRange(1024, 65535)
        # RockMan scored-Reality MCP owns 8765 in the current product runtime.
        self.port_spin.setValue(8766)
        form.addRow("Nguồn gọi", self.source_combo)
        form.addRow("Stream", self.stream_combo)
        form.addRow("Kho queue", self.queue_root_edit)
        form.addRow("Sổ quyền BAM", self.ledger_edit)
        form.addRow("Port local", self.port_spin)
        layout.addLayout(form)

        self._advanced_form = form
        for field in (self.queue_root_edit, self.ledger_edit):
            label = form.labelForField(field)
            if label is not None:
                label.hide()
            field.hide()
        self.advanced_button = QToolButton()
        self.advanced_button.setObjectName("mcpAdvancedButton")
        self.advanced_button.setText("Cấu hình kỹ thuật ▸")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)

        stream_actions = QHBoxLayout()
        self.find_stream_button = QPushButton("Tìm stream")
        self.find_stream_button.clicked.connect(self._discover_streams)
        self.copy_url_button = QPushButton("Sao chép URL")
        self.copy_url_button.clicked.connect(self._copy_url)
        stream_actions.addWidget(self.find_stream_button)
        stream_actions.addWidget(self.copy_url_button)
        layout.addLayout(stream_actions)

        controls_heading = QLabel("2 · KIỂM TRA KẾT NỐI")
        controls_heading.setObjectName("mcpSectionLabel")
        layout.addWidget(controls_heading)

        controls = QGridLayout()
        controls.setHorizontalSpacing(6)
        controls.setVerticalSpacing(6)
        self.test_button = QPushButton("Kiểm tra kết nối")
        self.test_button.setObjectName("mcpPrimaryAction")
        self.test_button.setToolTip("Chỉ kiểm tra MCP hiện hữu; bật/tắt runtime phải đi qua BAM/Manager.")
        self.test_button.clicked.connect(self._probe)
        controls.addWidget(self.test_button, 0, 0, 1, 2)
        layout.addLayout(controls)

        self.diagnostic_button = QToolButton()
        self.diagnostic_button.setObjectName("mcpAdvancedButton")
        self.diagnostic_button.setText("Nhật ký / chẩn đoán ▸")
        self.diagnostic_button.setCheckable(True)
        self.diagnostic_button.toggled.connect(self._toggle_diagnostics)
        layout.addWidget(self.diagnostic_button)
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("mcpDetail")
        self.detail.setReadOnly(True)
        self.detail.setMaximumBlockCount(500)
        self.detail.setMinimumHeight(84)
        self.detail.setMaximumHeight(130)
        self.detail.hide()
        layout.addWidget(self.detail)
        layout.addStretch(1)

        self._discover_streams()
        self.refresh_status()

    def _toggle_diagnostics(self, checked: bool) -> None:
        self.detail.setVisible(bool(checked))
        self.diagnostic_button.setText(
            "Nhật ký / chẩn đoán ▾" if checked else "Nhật ký / chẩn đoán ▸"
        )

    def _toggle_advanced(self, checked: bool) -> None:
        visible = bool(checked)
        for field in (self.queue_root_edit, self.ledger_edit):
            label = self._advanced_form.labelForField(field)
            if label is not None:
                label.setVisible(visible)
            field.setVisible(visible)
        self.advanced_button.setText(
            "Cấu hình kỹ thuật ▾" if visible else "Cấu hình kỹ thuật ▸"
        )

    def _source(self) -> McpSourceSystem:
        return McpSourceSystem.ROCKMAN if self.source_combo.currentIndex() == 0 else McpSourceSystem.TASK_FORGE

    def _profile(self) -> BamMcpRuntimeProfile:
        return BamMcpRuntimeProfile(
            source_system=self._source(),
            stream_id=self.stream_combo.currentText().strip(),
            queue_root=Path(self.queue_root_edit.text().strip()),
            authority_ledger_path=Path(self.ledger_edit.text().strip()),
            port=int(self.port_spin.value()),
        )

    def _discover_streams(self) -> None:
        current = self.stream_combo.currentText().strip()
        rows = self.controller.discover_streams(Path(self.queue_root_edit.text().strip()))
        self.stream_combo.clear()
        self.stream_combo.addItems(list(rows))
        if current and current not in rows:
            self.stream_combo.addItem(current)
            self.stream_combo.setCurrentText(current)
        elif rows:
            self.stream_combo.setCurrentIndex(len(rows)-1)
        self.refresh_status()

    def _copy_url(self) -> None:
        url = f"http://127.0.0.1:{self.port_spin.value()}/mcp"
        QApplication.clipboard().setText(url)
        self.detail.setPlainText("Đã sao chép URL MCP:\n" + url)

    def _probe(self) -> None:
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("Đang kiểm tra…")
        self.detail.setPlainText("Đang kiểm tra MCP ở background; UI vẫn hoạt động.")
        self._probe_thread = threading.Thread(target=self._probe_worker, name="bam-mcp-probe", daemon=True)
        self._probe_thread.start()

    def _probe_worker(self) -> None:
        try:
            self.probe_succeeded.emit(self.controller.probe())
        except BaseException as exc:
            self.probe_failed.emit(type(exc).__name__ + ": " + str(exc))

    def _probe_completed(self, row: dict) -> None:
        self.detail.setPlainText(
            "KẾT NỐI MCP: ĐẠT\n"
            f"Protocol: {row.get('protocol_version')}\n"
            f"Server: {row.get('server_name')}\n"
            f"Tools: {', '.join(row.get('tools') or [])}\n"
            f"Resources: {', '.join(row.get('resources') or [])}"
        )
        self._finish_probe_ui()

    def _probe_failed_ui(self, detail: str) -> None:
        self.detail.setPlainText("KẾT NỐI MCP: CHƯA ĐẠT\n" + detail)
        self._finish_probe_ui()

    def _finish_probe_ui(self) -> None:
        self._probe_thread = None
        self.test_button.setText("Kiểm tra kết nối")
        self.refresh_status()

    def _refresh_coordination(self) -> dict:
        stream_id = self.stream_combo.currentText().strip()
        row = adaptive_runtime_status_projection(stream_id) if stream_id else {
            "runnable_queue_depth": 0, "blocked_dependency_count": 0,
            "blocked_collision_count": 0, "blocked_resource_count": 0,
            "lease_race_count": 0, "coordination_projection": {"requests": []},
        }
        values = (("RUN", row.get("runnable_queue_depth", 0)), ("DEP", row.get("blocked_dependency_count", 0)), ("COLL", row.get("blocked_collision_count", 0)), ("RES", row.get("blocked_resource_count", 0)), ("LEASE", row.get("lease_race_count", 0)))
        for chip, (label, value) in zip(self.coordination_chips, values):
            chip.setText(f"{label} {int(value or 0)}")
        requests = list(dict(row.get("coordination_projection") or {}).get("requests") or ())
        self.coordination_list.clear()
        if not requests:
            self.coordination_list.addItem("No active coordination state")
            return row
        for request in requests[:24]:
            resource = str(request.get("resource_class") or "unknown").replace("_", " ").upper()
            active = int(request.get("resource_active") or 0); capacity = int(request.get("resource_capacity") or 0)
            decision = str(request.get("decision") or "UNKNOWN").replace("_", " ")
            blocked = str(request.get("blocked_reason") or "").replace("_", " ")
            state = decision + (f" / {blocked}" if blocked else "")
            capability = str(request.get("capability_id") or "unknown"); request_id = str(request.get("request_id") or "")
            deps = len(request.get("unresolved_dependency_ids") or ()); collisions = len(request.get("collision_request_ids") or ())
            item = QListWidgetItem(f"{capability} · {resource} {active}/{capacity} · {state}\n{request_id[:18]} · DEP {deps} · COLL {collisions}")
            item.setToolTip(request_id); item.setSizeHint(QSize(0, 54)); self.coordination_list.addItem(item)
        return row

    def refresh_status(self) -> dict:
        self._refresh_coordination()
        row = self.controller.status()
        if row.get("running"):
            self.status_label.setText(f"MCP · ĐANG BẬT · {row.get('source_system')} · {row.get('url')}")
        elif row.get("sdk_runtime_available"):
            self.status_label.setText("MCP · ĐÃ CÓ RUNTIME · server đang tắt")
        else:
            self.status_label.setText("MCP · CHƯA CÓ RUNTIME SDK trên máy này")
        running = bool(row.get("running"))
        config_enabled = not running
        for widget in (
            self.source_combo,
            self.stream_combo,
            self.queue_root_edit,
            self.ledger_edit,
            self.port_spin,
            self.find_stream_button,
            self.copy_url_button,
        ):
            widget.setEnabled(config_enabled)
        self.test_button.setEnabled(running)
        self.status_changed.emit(dict(row))
        return row

class OperatorDashboard(QWidget):
    """Read-only human cockpit projected from BAM-owned durable state."""

    def __init__(self, owner: "BlackArmorWorkbench") -> None:
        super().__init__()
        self.owner = owner
        self.setObjectName("operatorDashboard")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        eyebrow = QLabel("NOW · OPERATOR COCKPIT")
        eyebrow.setObjectName("operatorEyebrow")
        title = QLabel("Black Armor technical control")
        title.setObjectName("operatorTitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        header.addLayout(title_box)
        header.addStretch(1)
        self.source_state = QLabel("STATE · CHECKING")
        self.source_state.setObjectName("operatorStateChip")
        header.addWidget(self.source_state)
        root.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        for label, surface in (
            ("REFRESH", "refresh"),
            ("FILES / EDITOR", "files"),
            ("LIVE TOOLS", "tools"),
            ("REALITY", "reality"),
            ("RECEIPT", "receipt"),
        ):
            button = QPushButton(label)
            button.setObjectName("operatorQuickAction")
            if surface == "refresh":
                button.clicked.connect(owner._refresh_execution_view)
            else:
                button.clicked.connect(lambda checked=False, key=surface: owner._show_surface(key))
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)

        scroll = QScrollArea()
        scroll.setObjectName("operatorScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("operatorBody")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 4, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        request_card, self.request_value = self._card("CURRENT REQUEST", "No verified external request")
        exec_card, self.execution_value = self._card("EXECUTION", "No durable execution observed")
        queue_card, self.queue_value = self._card("QUEUE / HOST", "No verified queue state")
        infra_card, self.infra_value = self._card("INFRA / STORAGE", "Projection not refreshed")
        proof_card, self.proof_value = self._card("PROOF / REALITY", "No durable proof projection")
        attention_card, self.attention_value = self._card("ATTENTION", "Projection not refreshed")
        capability_card, self.capability_value = self._card("CAPABILITY MAP", "Reading live registry")

        grid.addWidget(request_card, 0, 0)
        grid.addWidget(exec_card, 0, 1)
        grid.addWidget(queue_card, 1, 0)
        grid.addWidget(infra_card, 1, 1)
        grid.addWidget(proof_card, 2, 0)
        grid.addWidget(attention_card, 2, 1)
        grid.addWidget(capability_card, 3, 0, 1, 2)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        self._refresh_capabilities()

    def _card(self, title: str, initial: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("operatorCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("operatorCardTitle")
        value = QLabel(initial)
        value.setObjectName("operatorCardValue")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(heading)
        layout.addWidget(value, 1)
        return card, value

    def _refresh_capabilities(self) -> None:
        families: dict[str, int] = {}
        specs = tuple(human_capabilities())
        for spec in specs:
            family = str(spec.family or "other")
            families[family] = families.get(family, 0) + 1
        groups = sorted(installed_skill_groups(), key=lambda group: (-len(group["capability_ids"]), group["name"]))
        top = [f"{group['name']} {len(group['capability_ids'])}" for group in groups[:4]]
        remaining = max(0, len(groups) - len(top))
        if remaining:
            top.append(f"+{remaining} more families")
        preview = "  ·  ".join(top)
        self.capability_value.setText(f"{len(specs)} installed tools · {len(families)} live families\n{preview}")

    @staticmethod
    def _human_state(value: Any) -> str:
        raw = str(value or "NOT_AVAILABLE").strip()
        aliases = {
            "NOT_AVAILABLE": "Not available",
            "STORAGE_HEALTH_UNKNOWN": "Unknown",
            "SYNC_STATE_UNKNOWN": "Unknown",
            "SYNCED": "Synced",
            "CLEAR": "Clear",
        }
        return aliases.get(raw, raw.replace("_", " ").title())

    def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._refresh_capabilities()
        source = str(snapshot.get("source") or "durable projection")
        self.source_state.setText("STATE · OBSERVED")
        self.source_state.setToolTip("Read-only BAM projection · " + source)

        agent = dict(snapshot.get("agent_projection") or {})
        if agent.get("verified") is True:
            request_id = str(agent.get("request_id") or "unknown")
            requester = str(agent.get("requester_id") or "unknown")
            capability = str(agent.get("capability_id") or "unknown")
            authority = str(agent.get("authority_id") or "unknown")
            self.request_value.setText(
                f"{request_id}\nRequester · {requester}\nCapability · {capability}\nAuthority · {authority}"
            )
        else:
            self.request_value.setText(
                "No verified external request in BAM projection.\n"
                f"Reason · {agent.get('reason') or 'requester binding unavailable'}"
            )

        latest_execution = dict(snapshot.get("latest_execution") or {})
        latest_event = dict(snapshot.get("latest_event") or {})
        latest_receipt = dict(snapshot.get("latest_receipt") or {})
        execution_id = str(latest_execution.get("execution_id") or latest_event.get("execution_id") or latest_receipt.get("execution_id") or "none")
        execution_state = str(latest_receipt.get("state") or latest_event.get("to_state") or latest_execution.get("state") or "NOT_AVAILABLE")
        self.execution_value.setText(
            f"Execution · {execution_id}\nState · {execution_state}\n"
            f"Events · {snapshot.get('event_count', 0)} · Receipts · {snapshot.get('receipt_count', 0)}"
        )

        stream = dict(snapshot.get("stream_queue") or {})
        host = dict(snapshot.get("host_shards") or {})
        counts = dict(stream.get("counts") or {})
        host_counts = dict(host.get("counts") or {})
        if stream.get("verified") is True:
            self.queue_value.setText(
                f"Pending · {counts.get('PENDING', 0)}   Leased · {counts.get('LEASED', 0)}   Ack · {counts.get('ACKNOWLEDGED', 0)}\n"
                f"Recovery · {counts.get('LEASE_EXPIRED_REQUIRES_RECOVERY', 0)}   "
                f"Hosts · {host.get('active_host_count', 0) if host.get('verified') is True else 'NOT_AVAILABLE'}   "
                f"Assigned shards · {host_counts.get('ACTIVE', 0) if host.get('verified') is True else 'NOT_AVAILABLE'}"
            )
        else:
            self.queue_value.setText("Queue projection NOT_AVAILABLE · no healthy-state fallback")

        backend = dict(snapshot.get("backend_status") or {})
        storage = dict(backend.get("storage_health") or {})
        sync = dict(backend.get("projection_sync") or {})
        quarantine = dict(backend.get("storage_quarantine") or {})
        self.infra_value.setText(
            f"Storage · {self._human_state(storage.get('state'))}\n"
            f"Projection sync · {self._human_state(sync.get('state'))}\n"
            f"Quarantine · {self._human_state(quarantine.get('state'))} · "
            f"{quarantine.get('quarantined_shards', 'NOT_AVAILABLE')} shard"
        )

        self.proof_value.setText(
            f"Durable source · {source}\n"
            f"Executions · {snapshot.get('execution_count', 0)}   Events · {snapshot.get('event_count', 0)}   Receipts · {snapshot.get('receipt_count', 0)}\n"
            "UI is projection only · no business-success inference"
        )

        attention: list[str] = []
        dirty = int(sync.get("dirty_shards", 0) or 0)
        if sync.get("state") not in (None, "SYNCED"):
            attention.append(f"Projection sync · {self._human_state(sync.get('state'))} · dirty {dirty}")
        failed_storage = int(storage.get("failed_shards", 0) or 0)
        if failed_storage:
            attention.append(f"Storage contract failures · {failed_storage}")
        quarantined = int(quarantine.get("quarantined_shards", 0) or 0)
        if quarantined:
            attention.append(f"Quarantined shards · {quarantined}")
        recovery = dict(backend.get("recovery") or {})
        recovery_count = int(recovery.get("requires_explicit_recovery", 0) or 0)
        if recovery_count:
            attention.append(f"Explicit recovery required · {recovery_count}")
        maintenance = dict(backend.get("maintenance_evidence") or {})
        if maintenance.get("state") == "MAINTENANCE_EVIDENCE_TAMPER_DETECTED":
            attention.append("Maintenance evidence tamper detected")
        self.attention_value.setText(
            "\n".join(attention)
            if attention
            else "No explicit alert in the current snapshot. This does not imply PASS."
        )

    def update_error(self, detail: str) -> None:
        self.source_state.setText("STATE · UNAVAILABLE")
        message = "Projection unavailable · " + detail
        for label in (
            self.request_value, self.execution_value, self.queue_value,
            self.infra_value, self.proof_value, self.attention_value,
        ):
            label.setText(message)


class BlackArmorWorkbench(QMainWindow):
    native_redock_requested = Signal(int, int)
    def __init__(
        self,
        initial_workspace: Path | None = None,
        *,
        settings: QSettings | None = None,
        storage_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("BlackArmorWorkbench")
        self.setWindowTitle("BLACK ARMOR — Universal Capability Workbench")
        self.resize(1600, 980)
        self.setMinimumSize(1100, 700)
        self.setDockNestingEnabled(True)
        self.setDockOptions(
            QMainWindow.AllowNestedDocks
            | QMainWindow.AllowTabbedDocks
            | QMainWindow.AnimatedDocks
        )

        self.settings = settings if settings is not None else QSettings(ORG_NAME, APP_NAME)
        self.storage_root = Path(storage_root) if storage_root is not None else None
        self.workspace: Path | None = None
        self.session = None
        self._all_docks: list[QDockWidget] = []
        self._dock_home_areas: dict[QDockWidget, Qt.DockWidgetArea] = {}
        self._dock_base_titles: dict[QDockWidget, str] = {}
        self._dock_snap_margin = 42
        self._dock_snap_pending: set[QDockWidget] = set()
        self._native_redock_stops: dict[int, threading.Event] = {}
        self.native_redock_requested.connect(self._handle_native_redock_request)
        threading.Thread(target=_console_guard_loop, daemon=True, name="bam-console-guard").start()
        self._panes: list[WorkspacePane] = []
        self._active_workspace_pane: WorkspacePane | None = None
        typography = read_global_typography()
        self._global_font_family = str(typography["family"])
        self._global_font_size = int(typography["size"])
        self._global_text_timer = QTimer(self)
        self._global_text_timer.setInterval(700)
        self._global_text_timer.timeout.connect(self._poll_global_typography)
        self._global_text_timer.start()
        BAM_PROFILE_UI_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        self._browser_profile_requests_pending: set[str] = set()
        self._browser_profile_request_timer = QTimer(self)
        self._browser_profile_request_timer.setInterval(BROWSER_REQUEST_IDLE_INTERVAL_MS)
        self._browser_profile_request_timer.timeout.connect(self._poll_browser_profile_requests)
        self._browser_profile_request_timer.start()

        self._build_central_workspace()
        self._build_top_toolbar()
        self._build_docks()
        self._protect_dock_contents()
        self._build_bottom_toolbar()
        self._build_menus()
        self._apply_global_typography(self._global_font_family, self._global_font_size)

        layout_is_current = (
            int(
                self.settings.value(
                    "layout/version",
                    0,
                )
                or 0
            )
            == LAYOUT_VERSION
        )

        if layout_is_current:
            self._restore_workspace_layout()

            restored = self.restoreGeometry(
                self.settings.value(
                    "geometry",
                    b"",
                )
            )

            restored_state = self.restoreState(
                self.settings.value(
                    "windowState",
                    b"",
                )
            )

            if not (
                restored
                and restored_state
            ):
                self._default_layout()

        else:
            # New visual architecture owns a clean one-pane baseline.
            # Never import stale V3 dock/pane geometry into V4.
            self.settings.remove(
                "workspace/paneCount"
            )
            self.settings.remove(
                "workspace/orientation"
            )
            self.settings.remove(
                "workspace/splitterState"
            )
            self.settings.remove(
                "windowState"
            )
            self._default_layout()

        self._group_dock_panels()

        # Agent monitoring is always opt-in per launch. A prior saved window
        # state must never consume the human owner's workspace unexpectedly.
        self.agent_dock.hide()
        self.mcp_dock.hide()
        self._show_surface("now")

        if initial_workspace is not None and initial_workspace.is_dir():
            self.set_workspace(initial_workspace)

    def _build_central_workspace(self) -> None:
        shell = QWidget()
        shell.setObjectName("operatorShell")
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        rail = QFrame()
        rail.setObjectName("navRail")
        rail.setFixedWidth(108)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(8, 12, 8, 10)
        rail_layout.setSpacing(5)
        nav_brand = QLabel("BAM")
        nav_brand.setObjectName("navBrand")
        nav_caption = QLabel("CONTROL")
        nav_caption.setObjectName("navCaption")
        rail_layout.addWidget(nav_brand)
        rail_layout.addWidget(nav_caption)
        rail_layout.addSpacing(8)
        self._nav_buttons: dict[str, QToolButton] = {}
        for key, label in (("now", "NOW"), ("files", "FILES"), ("tools", "TOOLS"), ("skills", "SKILLS"), ("browsers", "BROWSERS"), ("reality", "REALITY"), ("receipt", "RECEIPT"), ("console", "CONSOLE"), ("mcp", "MCP")):
            button = self._nav_button(label, key)
            self._nav_buttons[key] = button
            rail_layout.addWidget(button)
        rail_layout.addStretch(1)
        boundary = QLabel("HUMAN\nOPERATOR")
        boundary.setObjectName("navBoundary")
        boundary.setAlignment(Qt.AlignCenter)
        rail_layout.addWidget(boundary)
        shell_layout.addWidget(rail)

        self.central_stack = QStackedWidget()
        self.central_stack.setObjectName("centralStack")
        self.operator_dashboard = OperatorDashboard(self)
        self.central_stack.addWidget(self.operator_dashboard)

        editor_page = QWidget()
        editor_page.setObjectName("editorPage")
        editor_layout = QVBoxLayout(editor_page)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        editor_layout.addWidget(self.workspace_splitter)
        self.central_stack.addWidget(editor_page)
        self.browser_page = QWidget()
        self.browser_page.setObjectName("browserPage")
        browser_layout = QVBoxLayout(self.browser_page)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.browser_host = ChromeBrowserHost(self)
        browser_layout.addWidget(self.browser_host, 1)
        self.central_stack.addWidget(self.browser_page)
        shell_layout.addWidget(self.central_stack, 1)
        self.setCentralWidget(shell)
        self._add_workspace_pane()

    def _add_workspace_pane(self) -> WorkspacePane:
        pane = WorkspacePane(self)
        self._panes.append(pane)
        if len(self._panes) == 1:
            self._active_workspace_pane = pane
        self.workspace_splitter.addWidget(pane)
        return pane

    def _active_pane(self) -> WorkspacePane:
        focus = QApplication.focusWidget()
        while focus is not None:
            for pane in self._panes:
                if focus is pane:
                    self._active_workspace_pane = pane
                    return pane
            focus = focus.parentWidget()
        if self._active_workspace_pane is not None:
            return self._active_workspace_pane
        if self._panes:
            return self._panes[0]
        raise RuntimeError("workspace pane is not initialized")

    def _nav_button(self, label: str, key: str) -> QToolButton:
        button = QToolButton()
        button.setText(label)
        button.setObjectName("navButton")
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setMinimumHeight(38)
        button.clicked.connect(lambda checked=False, name=key: self._show_surface(name))
        return button

    def _show_surface(self, key: str) -> None:
        docks = (
            getattr(self, "files_dock", None), getattr(self, "capability_dock", None),
            getattr(self, "skills_dock", None), getattr(self, "execution_dock", None),
            getattr(self, "receipt_dock", None), getattr(self, "console_dock", None),
            getattr(self, "mcp_dock", None), getattr(self, "agent_dock", None),
            getattr(self, "plugins_dock", None), getattr(self, "browser_profiles_dock", None),
        )
        for dock in docks:
            if dock is not None:
                dock.hide()
        if key == "files":
            self.central_stack.setCurrentIndex(1)
            self.files_dock.show()
        elif key == "browsers":
            self.central_stack.setCurrentWidget(self.browser_page)
            self.browser_profiles_dock.show()
            self.browser_profiles_dock.raise_()
            self.browser_profiles_panel.reload()
            profile_id = self.browser_profiles_panel._reality_profile_id or self.browser_host._active_id
            if profile_id:
                self.browser_host.select_profile(profile_id)
        else:
            self.central_stack.setCurrentIndex(0)
            target = {
                "tools": self.capability_dock,
                "skills": self.skills_dock,
                "browsers": self.browser_profiles_dock,
                "reality": self.execution_dock,
                "receipt": self.receipt_dock,
                "console": self.console_dock,
                "mcp": self.mcp_dock,
            }.get(key)
            if target is not None:
                target.show()
                target.raise_()
            if key == "mcp" and getattr(self, "mcp_panel", None) is not None:
                # Opening MCP from the nav rail must re-read live gateway state, not just
                # re-show the dock, so the pill/label never presents stale status.
                self.mcp_panel.refresh_status()
        button = getattr(self, "_nav_buttons", {}).get(key)
        if button is not None:
            button.setChecked(True)
        if key in ("now", "reality"):
            self._refresh_execution_view()

    @staticmethod
    def _scale_style_sheet(style_sheet: str, size: int) -> str:
        scale = normalize_global_font_size(size) / float(GLOBAL_FONT_BASE_SIZE)
        return re.sub(
            r"font-size:\s*(\d+(?:\.\d+)?)px",
            lambda match: f"font-size: {max(1, round(float(match.group(1)) * scale))}px",
            style_sheet,
        )

    def _apply_global_typography(self, family: str, size: int) -> None:
        clean_family = normalize_global_font_family(family)
        clean_size = normalize_global_font_size(size)
        if clean_family == self._global_font_family and clean_size == self._global_font_size and self.styleSheet():
            return
        self._global_font_family, self._global_font_size = clean_family, clean_size
        app = QApplication.instance()
        if app is not None:
            font = QFont(app.font())
            font.setFamily(clean_family)
            font.setPointSize(clean_size)
            app.setFont(font)
        self._apply_style()
        for widget in QApplication.allWidgets():
            if widget.objectName() == "codeEditor":
                font = QFont(widget.font())
                font.setFamily(clean_family)
                font.setPointSize(clean_size)
                widget.setFont(font)

    def _poll_global_typography(self) -> None:
        state = read_global_typography()
        if str(state["family"]) != self._global_font_family or int(state["size"]) != self._global_font_size:
            self._apply_global_typography(str(state["family"]), int(state["size"]))

    def _build_top_toolbar(self) -> None:
        bar = QToolBar("Command Bar")
        bar.setObjectName("commandToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        self.addToolBar(Qt.TopToolBarArea, bar)

        brand = QWidget()
        brand.setObjectName("brandBlock")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(14, 0, 18, 0)
        brand_layout.setSpacing(10)

        title = QLabel("BLACK ARMOR")
        title.setObjectName("appTitle")
        subtitle = QLabel("WORKBENCH")
        subtitle.setObjectName("brandSubtitle")

        brand_layout.addWidget(title)
        brand_layout.addWidget(subtitle)
        bar.addWidget(brand)

        self.workspace_label = QLabel("No workspace")
        self.workspace_label.setObjectName("workspacePath")
        self.workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.workspace_label.setMaximumWidth(170)
        self.workspace_label.setToolTip("Current workspace")
        bar.addWidget(self.workspace_label)

        self.live_state_label = QLabel("STATE · CHECKING")
        self.live_state_label.setObjectName("liveStatePill")
        bar.addWidget(self.live_state_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        self.show_all_panels_action = QAction("Mở tất cả panel", self)
        self.show_all_panels_action.triggered.connect(self.show_all_panels)
        self.hide_all_panels_action = QAction("Ẩn tất cả panel", self)
        self.hide_all_panels_action.triggered.connect(self.focus_mode)
        for action in (self.show_all_panels_action, self.hide_all_panels_action):
            button = QToolButton()
            button.setObjectName("panelVisibilityButton")
            button.setDefaultAction(action)
            button.setMinimumHeight(36)
            bar.addWidget(button)

        self.agent_button = QToolButton()
        self.agent_button.setObjectName("agentsButton")
        self.agent_button.setText("AGENTS · 0")
        self.agent_button.setToolTip("Open secondary agent monitoring")
        self.agent_button.clicked.connect(self._toggle_agent_drawer)
        bar.addWidget(self.agent_button)

        self.mcp_button = QToolButton()
        self.mcp_button.setObjectName("mcpButton")
        self.mcp_button.setText("MCP · OFF")
        self.mcp_button.setToolTip("Mở bảng kết nối MCP — không tạo quyền request")
        self.mcp_button.clicked.connect(self._toggle_mcp_drawer)
        bar.addWidget(self.mcp_button)

    def _build_docks(self) -> None:
        self.files_dock = self._dock("Files", Qt.LeftDockWidgetArea)
        self.files_dock.setObjectName("FilesDock")
        self.files_dock.setMinimumWidth(220)
        self.files_dock.setMaximumWidth(285)

        self.file_model = QFileSystemModel()
        self.file_model.setRootPath("")

        self.file_tree = QTreeView()
        self.file_tree.setObjectName("fileTree")
        self.file_tree.setModel(self.file_model)
        self.file_tree.setHeaderHidden(False)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setIndentation(15)
        self.file_tree.setAnimated(False)
        self.file_tree.doubleClicked.connect(self._file_activated)

        files_host = QWidget()
        files_host.setObjectName("filesSurface")
        files_layout = QVBoxLayout(files_host)
        files_layout.setContentsMargins(14, 14, 10, 14)
        files_layout.setSpacing(10)

        files_heading = QLabel("FILES")
        files_heading.setObjectName("sectionLabel")
        files_layout.addWidget(files_heading)

        file_search = QLineEdit()
        file_search.setObjectName("fileSearch")
        file_search.setPlaceholderText("Search files...")
        file_search.setClearButtonEnabled(True)
        files_layout.addWidget(file_search)
        files_layout.addWidget(self.file_tree, 1)

        self.files_dock.setWidget(files_host)

        self.capability_dock = self._dock("Live Tools", Qt.RightDockWidgetArea)
        self.capability_dock.setObjectName("CommandDock")
        self.capability_dock.setMinimumWidth(300)
        self.capability_dock.setMaximumWidth(390)
        self.capability_panel = CapabilityPanel(self)
        self.capability_dock.setWidget(self.capability_panel)

        self.skills_dock = self._dock("Skill Library", Qt.RightDockWidgetArea)
        self.skills_dock.setMinimumWidth(300)
        self.skills_dock.setMaximumWidth(420)
        self.skill_catalog_panel = SkillCatalogPanel()
        self.skills_dock.setWidget(self.skill_catalog_panel)

        self.browser_profiles_dock = self._dock("Hồ sơ Chrome", Qt.RightDockWidgetArea)
        self.browser_profiles_dock.setObjectName("ChromeProfilesDock")
        self.browser_profiles_dock.setMinimumWidth(330)
        self.browser_profiles_dock.setMaximumWidth(460)
        self.browser_profiles_panel = ChromeProfilesPanel(self)
        self.browser_profiles_dock.setWidget(self.browser_profiles_panel)

        plugin_storage = self.storage_root if self.storage_root is not None else (Path.home() / ".black_armor_workbench")
        self.plugins_dock = self._dock("Plugins", Qt.RightDockWidgetArea)
        self.plugins_dock.setObjectName("PluginDock")
        self.plugins_dock.setMinimumWidth(360)
        self.plugins_dock.setMaximumWidth(480)
        self.plugin_dock_panel = PluginDockPanel(storage_root=plugin_storage, parent=self)
        self.plugins_dock.setWidget(self.plugin_dock_panel)

        self.mcp_dock = self._dock("MCP", Qt.RightDockWidgetArea)
        self.mcp_dock.setObjectName("McpDock")
        self.mcp_dock.setMinimumWidth(390)
        self.mcp_dock.setMaximumWidth(500)
        mcp_storage = self.storage_root if self.storage_root is not None else (Path.home() / ".black_armor_workbench")
        self.mcp_panel = McpControlPanel(storage_root=mcp_storage, parent=self)
        self.mcp_panel.status_changed.connect(self._on_mcp_status_changed)
        self.mcp_dock.setWidget(self.mcp_panel)
        # McpControlPanel.__init__ runs refresh_status() before the status_changed handler
        # above is connected, so its first emit is dropped and the top-bar pill would stay
        # frozen on the static "MCP · OFF" default even while a canonical gateway is live.
        # Re-project the status the panel already read now that the handler is wired, so the
        # pill reflects the real runtime instead of a lost signal.
        self.mcp_panel.refresh_status()

        self.execution_dock = self._dock("Reality", Qt.BottomDockWidgetArea)
        self.execution_dock.setWindowTitle("Reality · CHỈ QUAN SÁT")
        self.execution_dock.setObjectName("RealityDock")

        execution_host = QWidget()
        execution_layout = QVBoxLayout(execution_host)
        execution_layout.setContentsMargins(14, 10, 14, 12)
        execution_layout.setSpacing(8)

        reality_help = QLabel("Bảng này chỉ đọc trạng thái BAM đã ghi nhận; không chạy hay điều khiển worker.")
        reality_help.setObjectName("realityHelp")
        execution_layout.addWidget(reality_help)

        self.execution_cell_summary = QLabel(
            "TẾ BÀO THỰC THI · CHƯA CÓ DỮ LIỆU"
        )
        self.execution_cell_summary.setObjectName("cellPoolSummary")
        self.execution_cell_summary.setToolTip(
            "Execution Cells = các worker kỹ thuật nhỏ. Mỗi cell chỉ được làm đúng request và đúng quyền đã cấp."
        )
        execution_layout.addWidget(self.execution_cell_summary)

        self.shard_admission_summary = QLabel(
            "CHIA SHARD · CHƯA CÓ DỮ LIỆU"
        )
        self.shard_admission_summary.setObjectName("shardAdmissionSummary")
        self.shard_admission_summary.setToolTip(
            "Shard = chia request thành các ngăn tải. Bộ chia chỉ chia request đã có quyền, không tự chọn việc quan trọng."
        )
        execution_layout.addWidget(self.shard_admission_summary)

        self.stream_queue_summary = QLabel(
            "HÀNG ĐỢI · CHƯA CÓ DỮ LIỆU"
        )
        self.stream_queue_summary.setObjectName("streamQueueSummary")
        self.stream_queue_summary.setToolTip(
            "Queue = hàng đợi giữ request. Lease chỉ là quyền giữ việc tạm thời; hết hạn phải xin quyền khôi phục mới, không tự chạy lại."
        )
        execution_layout.addWidget(self.stream_queue_summary)

        self.prt_summary = QLabel(
            "REMOTE · CHƯA QUAN SÁT thiết bị / phiên"
        )
        self.prt_summary.setObjectName("remotePrtSummary")
        self.prt_summary.setToolTip(
            "Remote tách trạng thái hiện tại khỏi kết quả của lần thử gần nhất. Lỗi RPC không đồng nghĩa thiết bị mất kết nối."
        )
        execution_layout.addWidget(self.prt_summary)

        self.process_supervisor_summary = QLabel(
            "TIẾN TRÌNH WORKER · CHƯA CÓ DỮ LIỆU"
        )
        self.process_supervisor_summary.setObjectName("processSupervisorSummary")
        self.process_supervisor_summary.setToolTip(
            "Supervisor chỉ theo dõi vòng đời process. Worker chết không có nghĩa là được tự restart hay chạy lại việc."
        )
        execution_layout.addWidget(self.process_supervisor_summary)

        self.host_shard_summary = QLabel(
            "HOST / SHARD · chưa có gán shard vào máy"
        )
        self.host_shard_summary.setObjectName("hostShardSummary")
        self.host_shard_summary.setToolTip(
            "Host placement = shard nào được máy nào phục vụ. Gán host không cấp quyền chạy request; hết hạn phải gán lại rõ ràng."
        )
        execution_layout.addWidget(self.host_shard_summary)

        backend_grid = QGridLayout()
        backend_grid.setHorizontalSpacing(18)
        backend_grid.setVerticalSpacing(4)

        self.queue_backend_summary = QLabel(
            "BACKEND HÀNG ĐỢI · mặc định chỉ an toàn trên một máy"
        )
        self.queue_backend_summary.setToolTip(
            "Queue backend = nơi lưu trạng thái hàng đợi. Local SQLite hiện không được coi là backend phân tán."
        )
        backend_grid.addWidget(self.queue_backend_summary, 0, 0)

        self.provider_proof_summary = QLabel(
            "BACKEND PHÂN TÁN · bộ test sẵn · chưa có provider được chứng minh"
        )
        self.provider_proof_summary.setToolTip(
            "Provider proof = 12 bài kiểm tra phá backend thật. Qua hết vẫn chỉ mở gate multi-host, chưa phải production PASS."
        )
        backend_grid.addWidget(self.provider_proof_summary, 0, 1)

        self.recovery_summary = QLabel(
            "KHÔI PHỤC · chưa có việc cần xử lý"
        )
        self.recovery_summary.setToolTip(
            "Recovery = xử lý việc bị gián đoạn. BAM không tự requeue; phải có quyền khôi phục mới."
        )
        backend_grid.addWidget(self.recovery_summary, 1, 0)

        self.resource_summary = QLabel(
            "TÀI NGUYÊN MÁY · chưa có lượt process được cấp tài nguyên"
        )
        self.resource_summary.setToolTip(
            "Resource admission = giới hạn CPU/RAM/process/IO cho lượt chạy. Nó không biết job nào quan trọng hơn."
        )
        backend_grid.addWidget(self.resource_summary, 1, 1)

        self.scale_metrics_summary = QLabel(
            "QUAN SÁT SCALE · metrics phân cấp chưa có dữ liệu"
        )
        self.scale_metrics_summary.setToolTip(
            "Metrics phân cấp = mỗi shard chỉ cập nhật nhánh của nó; màn hình đọc nút tổng ở gốc, không quét tất cả shard. Các số này không nói job nào quan trọng hay thành công."
        )
        backend_grid.addWidget(self.scale_metrics_summary, 2, 0, 1, 2)

        self.progress_checkpoint_summary = QLabel(
            "MỐC TIẾN ĐỘ · chưa có mốc kỹ thuật được ghi"
        )
        self.progress_checkpoint_summary.setToolTip(
            "Mốc tiến độ = dấu mốc kỹ thuật đã được lưu bền theo từng shard. Chỉ để xem hệ thống đã ghi trạng thái tới đâu; không phải nút chạy, khôi phục hay phát lại."
        )
        backend_grid.addWidget(self.progress_checkpoint_summary, 3, 0, 1, 2)

        self.projection_sync_summary = QLabel(
            "ĐỒNG BỘ HIỂN THỊ · chưa đọc được trạng thái"
        )
        self.projection_sync_summary.setToolTip(
            "Đồng bộ hiển thị = queue đã ghi thật nhưng metrics/biên nhận/mốc tiến độ có thể chưa cập nhật xong. BAM phải đánh dấu lệch trước khi commit; mỗi shard chỉ có một lượt sửa projection tại một thời điểm. Chỉ sửa hiển thị, không chạy lại request."
        )
        backend_grid.addWidget(self.projection_sync_summary, 4, 0, 1, 2)

        self.storage_health_summary = QLabel(
            "SỨC KHỎE LƯU TRỮ · chưa đọc được trạng thái"
        )
        self.storage_health_summary.setToolTip(
            "Kiểm tra nhanh kho lưu trữ = kiểm version, cấu trúc bảng/index và counter contract khi mở shard. Không quét sâu toàn bộ DB, không tự sửa dữ liệu và không chạy lại request."
        )
        backend_grid.addWidget(self.storage_health_summary, 5, 0, 1, 2)

        self.deep_storage_integrity_summary = QLabel(
            "KIỂM TRA SÂU DB · chưa chạy"
        )
        self.deep_storage_integrity_summary.setToolTip(
            "Kiểm tra sâu DB = lúc bảo trì mới quét toàn bộ SQLite của đúng một shard. Dòng tổng trên màn hình chỉ đọc nút gốc, không đi lục tất cả shard; phát hiện lỗi chỉ báo cần bảo trì, không tự sửa hay chạy lại request."
        )
        backend_grid.addWidget(self.deep_storage_integrity_summary, 6, 0, 1, 2)

        self.queue_logical_audit_summary = QLabel(
            "KIỂM TRA LOGIC HÀNG ĐỢI · chưa chạy"
        )
        self.queue_logical_audit_summary.setToolTip(
            "Kiểm tra logic hàng đợi = lúc bảo trì mới đọc row thật của đúng một shard để đối chiếu counter, lease, biên nhận và recovery. Dòng tổng chỉ đọc nút gốc; không tự sửa counter và không phát lại request."
        )
        backend_grid.addWidget(self.queue_logical_audit_summary, 7, 0, 1, 2)

        self.storage_quarantine_summary = QLabel(
            "KHÓA BẢO TRÌ · không có shard bị khóa"
        )
        self.storage_quarantine_summary.setToolTip(
            "Khóa bảo trì = shard đang bị chặn nhận thay đổi mới vì kiểm tra phát hiện lỗi. Màn hình chỉ đọc số shard bị khóa từ nút tổng; chỉ clearance có đủ bằng chứng PASS mới mở, không tự sửa hay phát lại request."
        )
        backend_grid.addWidget(self.storage_quarantine_summary, 8, 0, 1, 2)

        self.maintenance_evidence_summary = QLabel(
            "BẰNG CHỨNG BẢO TRÌ · chưa có chuỗi bằng chứng"
        )
        self.maintenance_evidence_summary.setToolTip(
            "Bằng chứng bảo trì = các lần kiểm tra, khóa và mở khóa được nối bằng hash theo thứ tự. Dòng này chỉ đọc nút tổng nên không quét mọi shard. Nếu phát hiện bằng chứng bị sửa, clearance phải dừng."
        )
        backend_grid.addWidget(self.maintenance_evidence_summary, 9, 0, 1, 2)

        self.technical_status_rebuild_summary = QLabel(
            "TỔNG HỢP TRẠNG THÁI · chưa cần dựng lại"
        )
        self.technical_status_rebuild_summary.setToolTip(
            "Tổng hợp trạng thái = cây số liệu chỉ để màn hình đọc nhanh. Nếu cây này mất/hỏng, màn hình phải báo cần dựng lại chứ không được coi số 0 là sạch. Dựng lại là maintenance riêng từ dữ liệu per-shard, không chạy lại công việc."
        )
        backend_grid.addWidget(self.technical_status_rebuild_summary, 10, 0, 1, 2)

        self.receipt_aggregation_summary = QLabel(
            "BIÊN NHẬN SCALE · cây hash biên nhận chưa có dữ liệu"
        )
        self.receipt_aggregation_summary.setToolTip(
            "Cây biên nhận = mỗi ack chỉ cập nhật shard và nhánh hash của nó. Nút gốc chứng minh số lượng/integrity kỹ thuật, không nói công việc thành công về nghiệp vụ."
        )
        backend_grid.addWidget(self.receipt_aggregation_summary, 11, 0, 1, 2)

        self.mcp_summary = QLabel(
            "CẦU MCP · mã server đã có · runtime phụ thuộc máy đang chạy"
        )
        self.mcp_summary.setToolTip(
            "MCP = đường giao tiếp giữa RockMan/Task Forge và BAM. Nó chỉ chuyển request, không suy nghĩ hay cấp quyền."
        )
        backend_grid.addWidget(self.mcp_summary, 12, 0, 1, 2)

        execution_layout.addLayout(backend_grid)

        self.execution_view = QTextEdit()
        self.execution_view.setObjectName("evidenceView")
        self.execution_view.setReadOnly(True)
        self.execution_view.setPlaceholderText(
            "Trạng thái kỹ thuật BAM đã được lưu sẽ hiện ở đây. Đây là dữ liệu quan sát, không tự tạo quyền."
        )
        execution_layout.addWidget(self.execution_view, 1)

        execution_actions = QHBoxLayout()
        self.execution_refresh_button = QPushButton("Làm mới")
        self.execution_refresh_button.clicked.connect(self._refresh_execution_view)

        execution_note = QLabel("Chỉ đọc · quyền duyệt nằm ở requester/Manager, không nằm trong projection này.")
        execution_note.setWordWrap(True)

        execution_actions.addWidget(self.execution_refresh_button)
        execution_actions.addStretch(1)
        execution_actions.addWidget(execution_note)
        execution_layout.addLayout(execution_actions)

        self.execution_dock.setWidget(execution_host)

        self.receipt_dock = self._dock("Receipt", Qt.BottomDockWidgetArea)
        self.receipt_dock.setObjectName("ReceiptDock")

        self.receipt_view = QTextEdit()
        self.receipt_view.setObjectName("evidenceView")
        self.receipt_view.setReadOnly(True)
        self.receipt_view.setPlaceholderText("Durable execution receipt appears here.")
        self.receipt_dock.setWidget(self.receipt_view)

        self.console_dock = self._dock("Console", Qt.BottomDockWidgetArea)
        self.console_dock.setObjectName("ConsoleDock")

        self.console = QPlainTextEdit()
        self.console.setObjectName("consoleView")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(2000)
        self.console_dock.setWidget(self.console)

        self.tabifyDockWidget(self.execution_dock, self.receipt_dock)
        self.tabifyDockWidget(self.execution_dock, self.console_dock)

        # Secondary agent surface. Never part of the default owner composition.
        self.agent_dock = self._dock("Agents", Qt.RightDockWidgetArea)
        self.agent_dock.setObjectName("AgentsDock")
        self.agent_dock.setMinimumWidth(280)
        self.agent_dock.setMaximumWidth(420)
        self.agent_dock.setWidget(self._agent_activity())

        self._refresh_execution_view()

    def _build_bottom_toolbar(self) -> None:
        self.focus_action = QAction("Focus Mode", self)
        self.focus_action.setShortcut(QKeySequence("F11"))
        self.focus_action.triggered.connect(self.focus_mode)

        self.work_action = QAction("Work Mode", self)
        self.work_action.triggered.connect(self.work_mode)

        self.split_right_action = QAction("Split Right", self)
        self.split_right_action.triggered.connect(
            lambda: self.split_workspace(Qt.Horizontal)
        )

        self.split_down_action = QAction("Split Down", self)
        self.split_down_action.triggered.connect(
            lambda: self.split_workspace(Qt.Vertical)
        )

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("Open Workspace...", self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self.choose_workspace)
        file_menu.addAction(open_action)

        save_action = QAction("Save Active File", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.save_active_file)
        file_menu.addAction(save_action)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")

        view_menu.addAction(self.show_all_panels_action)
        view_menu.addAction(self.hide_all_panels_action)
        view_menu.addSeparator()
        view_menu.addAction(self.focus_action)
        view_menu.addAction(self.work_action)
        view_menu.addSeparator()
        view_menu.addAction(self.split_right_action)
        view_menu.addAction(self.split_down_action)
        view_menu.addSeparator()

        for dock in self._all_docks:
            view_menu.addAction(dock.toggleViewAction())

        view_menu.addSeparator()

        dock_back = QAction("Dock all panels back", self)
        dock_back.setShortcut(QKeySequence("Ctrl+Shift+D"))
        dock_back.setToolTip("Ghim tất cả cửa sổ panel đang nổi trở lại Workbench")
        dock_back.triggered.connect(self._redock_all_panels)
        view_menu.addAction(dock_back)

        reset = QAction("Reset Layout", self)
        reset.triggered.connect(self._default_layout)
        view_menu.addAction(reset)

    def _protect_dock_contents(self) -> None:
        """A compressed dock scrolls its content instead of overlapping controls."""
        for dock in self._all_docks:
            content = dock.widget()
            if content is None or isinstance(content, QScrollArea):
                continue
            if content.layout() is not None:
                content.layout().setSizeConstraint(QLayout.SetMinimumSize)
            if dock in (self.capability_dock, self.plugins_dock, self.mcp_dock, self.browser_profiles_dock):
                content.setMinimumHeight(max(500, content.minimumSizeHint().height()))
            scroll = QScrollArea(dock)
            scroll.setObjectName(dock.objectName() + "ContentScroll")
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            scroll.setWidget(content)
            scroll.viewport().setStyleSheet("background: #0B0F15;")
            content.setAutoFillBackground(False)
            content.setAttribute(Qt.WA_StyledBackground, True)
            dock.setWidget(scroll)
        self.files_dock.setMinimumWidth(260)

    def _group_dock_panels(self) -> None:
        """Reconcile saved split stacks into tabs within their existing dock area."""
        for area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea, Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea):
            panels = [d for d in self._all_docks if not d.isFloating() and self.dockWidgetArea(d) == area]
            if panels:
                for dock in panels[1:]:
                    self.tabifyDockWidget(panels[0], dock)

    def _dock(self, title: str, area: Qt.DockWidgetArea) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(title.replace(" ", "") + "Dock")
        dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setToolTip(
            "Kéo thanh tiêu đề sang cạnh Workbench để ghim lại. "
            "Ctrl+Shift+D ghim toàn bộ panel về vị trí mặc định."
        )
        self.addDockWidget(area, dock)
        self._dock_home_areas[dock] = area
        self._dock_base_titles[dock] = title
        dock.topLevelChanged.connect(
            lambda floating, target=dock: self._on_dock_top_level_changed(target, floating)
        )
        dock.installEventFilter(self)

        self._all_docks.append(dock)
        return dock

    def _on_dock_top_level_changed(self, dock: QDockWidget, floating: bool) -> None:
        title = self._dock_base_titles.get(dock, dock.windowTitle())
        if floating:
            dock.setWindowTitle(f"{title} — kéo vào Workbench rồi thả để về vị trí")
            self._start_native_redock_watch(dock)
        else:
            dock.setWindowTitle(title)
            self._stop_native_redock_watch(dock)

    @staticmethod
    def _native_snap_area_for_rect(main_rect: tuple[int, int, int, int], dock_rect: tuple[int, int, int, int]) -> Qt.DockWidgetArea | None:
        ml, mt, mr, mb = main_rect
        dl, dt, dr, db = dock_rect
        ix = max(0, min(mr, dr) - max(ml, dl))
        iy = max(0, min(mb, db) - max(mt, dt))
        if ix < 24 or iy < 24:
            return None
        cx = (dl + dr) // 2
        cy = (dt + db) // 2
        distances = (
            (abs(cx - ml), Qt.LeftDockWidgetArea),
            (abs(cx - mr), Qt.RightDockWidgetArea),
            (abs(cy - mt), Qt.TopDockWidgetArea),
            (abs(cy - mb), Qt.BottomDockWidgetArea),
        )
        return min(distances, key=lambda row: row[0])[1]

    def _start_native_redock_watch(self, dock: QDockWidget) -> None:
        if sys.platform != "win32":
            return
        dock_hwnd = int(dock.winId())
        self._stop_native_redock_watch(dock)
        stop = threading.Event()
        self._native_redock_stops[dock_hwnd] = stop
        main_hwnd = int(self.winId())
        def watch() -> None:
            user32 = ctypes.windll.user32
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            first = RECT()
            if not user32.GetWindowRect(dock_hwnd, ctypes.byref(first)):
                return
            initial = (first.left, first.top, first.right, first.bottom)
            armed = False
            while not stop.wait(0.05):
                main = RECT(); floating = RECT()
                if not user32.IsWindow(dock_hwnd):
                    return
                if not user32.GetWindowRect(main_hwnd, ctypes.byref(main)) or not user32.GetWindowRect(dock_hwnd, ctypes.byref(floating)):
                    continue
                current = (floating.left, floating.top, floating.right, floating.bottom)
                if not armed:
                    if abs(current[0] - initial[0]) + abs(current[1] - initial[1]) < 18:
                        continue
                    armed = True
                area = self._native_snap_area_for_rect((main.left, main.top, main.right, main.bottom), current)
                if area is not None:
                    self.native_redock_requested.emit(dock_hwnd, int(area.value))
                    return
        threading.Thread(target=watch, name=f"bam-redock-{dock_hwnd}", daemon=True).start()

    def _stop_native_redock_watch(self, dock: QDockWidget) -> None:
        hwnd = int(dock.winId())
        stop = self._native_redock_stops.pop(hwnd, None)
        if stop is not None:
            stop.set()

    def _handle_native_redock_request(self, dock_hwnd: int, area_value: int) -> None:
        for dock in self._all_docks:
            if int(dock.winId()) == dock_hwnd and dock.isFloating():
                home = self._dock_home_areas.get(dock, Qt.DockWidgetArea(area_value))
                self._snap_floating_dock(dock, home)
                return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QDockWidget) and watched in self._all_docks and event.type() == QEvent.Move and watched.isFloating():
            area = self._snap_area_for_cursor(QCursor.pos())
            if area is not None and watched not in self._dock_snap_pending:
                self._dock_snap_pending.add(watched)
                QTimer.singleShot(0, lambda d=watched, a=area: self._snap_floating_dock(d, a))
        return super().eventFilter(watched, event)

    def _snap_area_for_cursor(self, cursor_pos) -> Qt.DockWidgetArea | None:
        frame = self.frameGeometry(); x, y = cursor_pos.x(), cursor_pos.y(); m = self._dock_snap_margin
        if frame.top()-m <= y <= frame.bottom()+m:
            if abs(x-frame.left()) <= m: return Qt.LeftDockWidgetArea
            if abs(x-frame.right()) <= m: return Qt.RightDockWidgetArea
        if frame.left()-m <= x <= frame.right()+m:
            if abs(y-frame.top()) <= m: return Qt.TopDockWidgetArea
            if abs(y-frame.bottom()) <= m: return Qt.BottomDockWidgetArea
        return None

    def _snap_floating_dock(self, dock: QDockWidget, area: Qt.DockWidgetArea) -> None:
        try:
            if dock.isFloating():
                dock.setFloating(False); self.addDockWidget(area, dock); dock.show(); dock.raise_()
                dock.setWindowTitle(self._dock_base_titles.get(dock, dock.windowTitle()))
        finally:
            self._dock_snap_pending.discard(dock)

    def _redock_panel(self, dock: QDockWidget) -> None:
        home = self._dock_home_areas.get(dock, Qt.RightDockWidgetArea)
        dock.setFloating(False)
        self.addDockWidget(home, dock)
        dock.setWindowTitle(self._dock_base_titles.get(dock, dock.windowTitle()))

    def _redock_all_panels(self) -> None:
        for dock in self._all_docks:
            self._redock_panel(dock)
        self.tabifyDockWidget(self.execution_dock, self.receipt_dock)
        self.tabifyDockWidget(self.execution_dock, self.console_dock)
        self.tabifyDockWidget(self.capability_dock, self.agent_dock)
        self.tabifyDockWidget(self.capability_dock, self.skills_dock)
        self.tabifyDockWidget(self.capability_dock, self.browser_profiles_dock)
        self._show_surface("now")
        self._group_dock_panels()
        self.log("Panels docked back to their Workbench homes.")

    def _hide_right_drawers(self, except_dock: QDockWidget | None = None) -> None:
        for dock in (
            self.capability_dock,
            self.skills_dock,
            self.browser_profiles_dock,
            self.mcp_dock,
            self.agent_dock,
        ):
            if dock is not except_dock:
                dock.hide()

    def _on_mcp_status_changed(self, row: dict) -> None:
        self.mcp_button.setText("MCP · BẬT" if row.get("running") else "MCP · TẮT")

    def _toggle_mcp_drawer(self) -> None:
        visible = not self.mcp_dock.isVisible()
        if visible:
            self._hide_right_drawers(except_dock=self.mcp_dock)
            self.mcp_dock.show()
            self.mcp_dock.raise_()
        else:
            self.mcp_dock.hide()
        self.mcp_panel.refresh_status()

    def _agent_activity(self) -> QWidget:
        host = QWidget()
        host.setObjectName("agentDrawer")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("AGENTS")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        boundary = QLabel(
            "Secondary external-operator presentation. "
            "BAM does not own agent cognition or requester authority."
        )
        boundary.setObjectName("agentBoundary")
        boundary.setWordWrap(True)
        layout.addWidget(boundary)

        identities = QHBoxLayout()
        identities.setSpacing(8)
        identities.addWidget(
            self._agent_identity("RockMan", "Requester surface", "rockman_head.png")
        )
        identities.addWidget(
            self._agent_identity("Zero", "External operator surface", "zero_head.png")
        )
        layout.addLayout(identities)

        projection = QFrame()
        projection.setObjectName("agentProjection")
        projection_layout = QVBoxLayout(projection)
        projection_layout.setContentsMargins(12, 12, 12, 12)
        projection_layout.setSpacing(0)

        projection_title = QLabel("BAM TECHNICAL PROJECTION")
        projection_title.setObjectName("activityTitle")
        projection_layout.addWidget(projection_title)

        self.agent_active_request = QLabel(
            "Unavailable — requester authority is outside BAM projection"
        )
        self.agent_capability = QLabel("Unavailable in BAM projection")
        self.agent_execution = QLabel("No technical execution")
        self.agent_activity_value = QLabel("No verified agent activity source")
        self.agent_reality = QLabel("Projection not refreshed")

        for heading, value in (
            ("ACTIVE REQUEST", self.agent_active_request),
            ("CURRENT CAPABILITY", self.agent_capability),
            ("TECHNICAL EXECUTION LINK", self.agent_execution),
            ("AGENT ACTIVITY", self.agent_activity_value),
            ("REALITY", self.agent_reality),
        ):
            projection_layout.addWidget(self._section(heading, value))

        layout.addWidget(projection, 1)

        refresh = QPushButton("Refresh technical projection")
        refresh.clicked.connect(self._refresh_execution_view)
        layout.addWidget(refresh)
        return host

    def _agent_identity(self, name: str, role: str, asset: str) -> QFrame:
        card = QFrame()
        card.setObjectName("agentIdentity")
        row = QHBoxLayout(card)
        row.setContentsMargins(9, 9, 9, 9)
        row.setSpacing(9)

        image = QLabel()
        image.setObjectName("agentIdentityImage")
        image.setFixedSize(52, 52)
        pixmap = QPixmap(str(self.asset_path(asset)))
        image.setPixmap(
            pixmap.scaled(50, 50, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        row.addWidget(image)

        copy = QVBoxLayout()
        copy.setSpacing(1)
        name_label = QLabel(name)
        name_label.setObjectName("agentIdentityName")
        role_label = QLabel(role)
        role_label.setObjectName("agentIdentityRole")
        state = QLabel("DETACHED · no verified gateway binding")
        state.setObjectName("agentDetachedState")
        state.setWordWrap(True)
        if not hasattr(self, "_agent_state_labels"):
            self._agent_state_labels = {}
        self._agent_state_labels[name.casefold()] = state
        copy.addWidget(name_label)
        copy.addWidget(role_label)
        copy.addWidget(state)
        row.addLayout(copy, 1)
        return card

    def _section(self, title: str, value: QLabel) -> QFrame:
        frame = QFrame()
        frame.setObjectName("activitySection")
        section_layout = QVBoxLayout(frame)
        section_layout.setContentsMargins(0, 7, 0, 7)
        section_layout.setSpacing(4)
        heading = QLabel(title)
        heading.setObjectName("sectionLabel")
        value.setObjectName("sectionValue")
        value.setWordWrap(True)
        section_layout.addWidget(heading)
        section_layout.addWidget(value)
        return frame

    def asset_path(self, name: str) -> Path:
        return Path(__file__).with_name("workbench_assets") / name

    def choose_workspace(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Open Black Armor Workspace",
            str(self.workspace or Path.home()),
        )
        if chosen:
            self.set_workspace(Path(chosen))

    def set_workspace(self, workspace: Path) -> None:
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise NotADirectoryError(workspace)
        if self.session is not None:
            self.session.close()
        self.workspace = workspace
        self.session = open_human_workbench(workspace)
        self.workspace_label.setText(f"WORKSPACE · {workspace}")
        self.workspace_label.setToolTip(str(workspace))
        self.file_tree.setRootIndex(self.file_model.index(str(workspace)))
        self.setWindowTitle(f"BLACK ARMOR — {workspace.name}")
        self.log(f"Workspace opened: {workspace}")
        default_editor = workspace / "rock_man_executor" / "workbench_ui.py"
        if default_editor.is_file() and self._panes[0].tabs.count() == 1:
            self._panes[0].open_text_file(default_editor)

    def _file_activated(self, index) -> None:
        path = Path(self.file_model.filePath(index))
        if path.is_file():
            self._active_pane().open_text_file(path)

    def save_active_file(self) -> None:
        if not self._active_pane().save_current():
            self.log("No editable file is active.")

    def split_workspace(self, orientation: Qt.Orientation) -> None:
        if self.workspace_splitter.orientation() != orientation and len(self._panes) > 1:
            QMessageBox.information(
                self,
                "Split layout",
                "Current split uses the other orientation. Reset layout or close extra panes first.",
            )
            return
        self.workspace_splitter.setOrientation(orientation)
        pane = self._add_workspace_pane()
        current = self._panes[0].current_file()
        if current is not None and current.is_file():
            pane.open_text_file(current)
        self.log("Workspace split created.")

    def _restore_workspace_layout(self) -> None:
        orientation_name = str(
            self.settings.value("workspace/orientation", "horizontal")
        ).casefold()
        orientation = Qt.Vertical if orientation_name == "vertical" else Qt.Horizontal
        try:
            pane_count = int(self.settings.value("workspace/paneCount", 1))
        except (TypeError, ValueError):
            pane_count = 1
        pane_count = max(1, min(pane_count, 8))
        self.workspace_splitter.setOrientation(orientation)
        while len(self._panes) < pane_count:
            self._add_workspace_pane()
        splitter_state = self.settings.value("workspace/splitterState", b"")
        if splitter_state:
            self.workspace_splitter.restoreState(splitter_state)

    def show_all_panels(self) -> None:
        # Visibility only: never starts tools, connections, or agent work.
        self._group_dock_panels()
        for dock in self._all_docks:
            dock.show()
        self.capability_dock.raise_()
        self.execution_dock.raise_()

    def focus_mode(self) -> None:
        for dock in self._all_docks:
            dock.hide()
        self.log("Focus Mode: workspace owns the screen.")

    def work_mode(self) -> None:
        self._show_surface("files")
        self.capability_dock.show()
        self.capability_dock.raise_()
        self.log("Work Mode: files/editor + live tools.")

    def _default_layout(self) -> None:
        for dock in self._all_docks:
            self._redock_panel(dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.files_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.capability_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.skills_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.browser_profiles_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.mcp_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.execution_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.receipt_dock)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.console_dock)
        self.tabifyDockWidget(self.execution_dock, self.receipt_dock)
        self.tabifyDockWidget(self.execution_dock, self.console_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.agent_dock)
        self.tabifyDockWidget(self.capability_dock, self.agent_dock)
        self.tabifyDockWidget(self.capability_dock, self.skills_dock)
        self.tabifyDockWidget(self.capability_dock, self.browser_profiles_dock)
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        self.resizeDocks([self.files_dock, self.capability_dock], [260, 360], Qt.Horizontal)
        self.resizeDocks([self.execution_dock], [260], Qt.Vertical)
        self._show_surface("now")

    def _toggle_dock(self, dock: QDockWidget) -> None:
        dock.setVisible(not dock.isVisible())
        if dock.isVisible():
            dock.raise_()

    def _toggle_agent_drawer(self) -> None:
        if self.agent_dock.isVisible():
            self.agent_dock.hide()
            return
        self._refresh_execution_view()
        self._hide_right_drawers(except_dock=self.agent_dock)
        self.agent_dock.show()
        self.agent_dock.raise_()

    def on_browser_profile_selected(self, profile_id: str) -> None:
        panel = getattr(self, "browser_profiles_panel", None)
        if panel is not None:
            panel.select_profile(profile_id)

    def _set_browser_request_poll_active(self, active: bool) -> None:
        interval = BROWSER_REQUEST_ACTIVE_INTERVAL_MS if active else BROWSER_REQUEST_IDLE_INTERVAL_MS
        if self._browser_profile_request_timer.interval() != interval:
            self._browser_profile_request_timer.setInterval(interval)

    def _poll_browser_profile_requests(self) -> None:
        found_request = False
        for request_path in sorted(BAM_PROFILE_UI_REQUEST_DIR.glob("*.json")):
            if request_path.name.endswith(".result.json"):
                continue
            found_request = True
            request_id = request_path.stem
            if request_id in self._browser_profile_requests_pending:
                continue
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                profile_id = str(payload.get("profile") or "")
                action = str(payload.get("action") or "").strip().lower()
                card = next((row for row in profile_cards() if row.get("id") == profile_id), None)
                if card is None:
                    raise ValueError("UNKNOWN_PROFILE")
                self._browser_profile_requests_pending.add(request_id)
                if action == "select":
                    self._show_surface("browsers")
                    self.browser_host.select_profile(profile_id)
                    hwnd = self.browser_host._hwnds.get(profile_id)
                    if not hwnd or not self.browser_host._hwnd_matches_profile(profile_id, int(hwnd)):
                        raise RuntimeError("SELECT_PROFILE_NOT_ATTACHED")
                    result_path = request_path.with_suffix(".result.json")
                    publish_profile_result(result_path, {"status":"SELECTED","profile":profile_id,"hwnd":int(hwnd),"top_level":int(ctypes.windll.user32.GetParent(int(hwnd)) or 0)==0,"window_owner":"chrome"})
                    request_path.unlink(missing_ok=True)
                    self._browser_profile_requests_pending.discard(request_id)
                    continue
                if action != "open":
                    raise ValueError("UNSUPPORTED_PROFILE_UI_ACTION")
                if payload.get("interactive") is not True or str(payload.get("surface_class") or "").upper() not in {"HUMAN_OPERATOR", "UTILITY_ADMIN"}:
                    raise PermissionError("PROFILE_OPEN_REQUIRES_EXPLICIT_INTERACTIVE_UI_INTENT")
                self.open_browser_profile(card)
                QTimer.singleShot(100, lambda rid=request_id, pid=profile_id, rp=request_path: self._complete_browser_profile_request(rid, pid, rp, 0))
            except Exception as exc:
                result_path = request_path.with_suffix(".result.json")
                publish_profile_result(result_path, {"status":"FAILED","error":f"{type(exc).__name__}: {exc}"})
                request_path.unlink(missing_ok=True)
        self._set_browser_request_poll_active(found_request)

    def _complete_browser_profile_request(self, request_id: str, profile_id: str, request_path: Path, attempt: int) -> None:
        hwnd = self.browser_host._hwnds.get(profile_id)
        if hwnd and self.browser_host._hwnd_matches_profile(profile_id, int(hwnd)):
            result_path = request_path.with_suffix(".result.json")
            publish_profile_result(result_path, {"status":"ATTACHED","profile":profile_id,"hwnd":int(hwnd),"top_level":int(ctypes.windll.user32.GetParent(int(hwnd)) or 0)==0,"window_owner":"chrome"})
            request_path.unlink(missing_ok=True)
            self._browser_profile_requests_pending.discard(request_id)
            return
        if attempt >= 60:
            result_path = request_path.with_suffix(".result.json")
            publish_profile_result(result_path, {"status":"FAILED","profile":profile_id,"error":"ATTACH_TIMEOUT"})
            request_path.unlink(missing_ok=True)
            self._browser_profile_requests_pending.discard(request_id)
            return
        QTimer.singleShot(100, lambda: self._complete_browser_profile_request(request_id, profile_id, request_path, attempt+1))

    def open_browser_profile(self, card: dict) -> None:
        """Execute a governed BAM profile request inside the containment UI."""
        self._show_surface("browsers")
        current=next((row for row in profile_cards() if row.get("id")==card.get("id")),card)
        if current.get("browser_pid") and self.browser_host.attach_profile(current):
            self.log(f"Attached Chrome profile {current['id']} as a CDP client (BAM owns no browser window)."); return
        result=launch_profile(current); profile_id=str(current.get("id")); launch_pid=int(result.get('pid') or 0)
        # Separation (2026-10-08): the flash-guard that hid the browser window is retired.
        # The window belongs to Chrome / the fleet owner; BAM only places it by overlay once
        # it exists, so nothing about the browser's visibility is BAM-owned any more.
        def retry(attempt=0):
            fresh=next((row for row in profile_cards() if row.get("id")==profile_id),current)
            if fresh.get("browser_pid") and self.browser_host.attach_profile(fresh):
                self.browser_profiles_panel.update_card(fresh); self.log(f"Attached Chrome profile {profile_id} as a CDP client (BAM owns no browser window)."); return
            if attempt < 20: QTimer.singleShot(150, lambda: retry(attempt+1))
            else: QMessageBox.warning(self,"Chrome profile",f"Không attach được {profile_id} vào BAM.")
        QTimer.singleShot(150, retry)

    def _sync_active_editor(self) -> None:
        if self._panes:
            self._active_pane()

    def _canonical_execution_snapshot(self) -> dict[str, Any]:
        return read_bam_execution_snapshot(
            self.storage_root
        )

    def _refresh_execution_view(self) -> None:
        """Render BAM-owned durable technical execution state."""
        try:
            snapshot = (
                self._canonical_execution_snapshot()
            )

            self._execution_projection = dict(
                snapshot
            )

            cell_pool = dict(snapshot.get("cell_pool") or {})
            if cell_pool.get("verified") is True:
                self.execution_cell_summary.setText(
                    "TẾ BÀO THỰC THI · "
                    f"{cell_pool.get('cell_count', 0)} cell · "
                    f"{_vi_state(cell_pool.get('state', 'UNKNOWN'))} · "
                    f"xong {cell_pool.get('completed_requests', 0)}/"
                    f"{cell_pool.get('total_requests', 0)} request"
                )
            else:
                self.execution_cell_summary.setText(
                    "TẾ BÀO THỰC THI · CHƯA CÓ DỮ LIỆU"
                )

            shard_admission = dict(
                snapshot.get("shard_admission") or {}
            )
            if shard_admission.get("verified") is True:
                audit = dict(shard_admission.get("audit") or {})
                self.shard_admission_summary.setText(
                    "CHIA SHARD · "
                    f"guard {_vi_state(audit.get('decision', 'UNKNOWN'))} · "
                    f"{shard_admission.get('shard_count', 0)} shard · "
                    f"{shard_admission.get('request_count', 0)} request"
                )
            else:
                self.shard_admission_summary.setText(
                    "CHIA SHARD · CHƯA CÓ DỮ LIỆU"
                )

            stream_queue = dict(
                snapshot.get("stream_queue") or {}
            )
            if stream_queue.get("verified") is True:
                counts = dict(stream_queue.get("counts") or {})
                backend_binding = dict(
                    stream_queue.get("queue_backend") or {}
                )
                backend_id = str(
                    backend_binding.get("backend_id")
                    or backend_binding.get("state")
                    or "chưa bind"
                )
                backend_mode = (
                    "dùng được multi-host"
                    if backend_binding.get("distributed_safe") is True
                    else "chỉ local / chưa phân tán"
                )
                self.stream_queue_summary.setText(
                    "HÀNG ĐỢI · "
                    f"{stream_queue.get('active_shards', 0)}/"
                    f"{stream_queue.get('configured_shards', 0)} shard hoạt động · "
                    f"{counts.get('PENDING', 0)} chờ · "
                    f"{counts.get('LEASED', 0)} đang giữ · "
                    f"{counts.get('ACKNOWLEDGED', 0)} đã có receipt · "
                    f"{counts.get('CANCELLED', 0)} hủy · "
                    f"{counts.get('LEASE_EXPIRED_REQUIRES_RECOVERY', 0)} cần khôi phục · "
                    f"{backend_id} ({backend_mode})"
                )
            else:
                self.stream_queue_summary.setText(
                    "HÀNG ĐỢI · CHƯA CÓ DỮ LIỆU"
                )

            remote_transport = dict(snapshot.get("remote_transport") or {})
            device_state = str(remote_transport.get("device_state") or "NOT_OBSERVED")
            device_session_id = str(remote_transport.get("device_session_id") or "NOT_OBSERVED")
            rpc_state = str(remote_transport.get("rpc_transport_state") or "NOT_OBSERVED")
            execution_state = str(remote_transport.get("execution_state") or "NOT_OBSERVED")
            last_rpc_error = str(remote_transport.get("last_rpc_error") or "")
            remote_currently_observed = (
                device_state != "NOT_OBSERVED" or device_session_id != "NOT_OBSERVED"
            )
            last_attempt_parts = []
            if rpc_state != "NOT_OBSERVED":
                last_attempt_parts.append(f"RPC {rpc_state}")
            if execution_state != "NOT_OBSERVED":
                last_attempt_parts.append(f"execution {execution_state}")
            if remote_currently_observed:
                remote_text = f"REMOTE · device {device_state} · session {device_session_id}"
            else:
                remote_text = "REMOTE · CHƯA QUAN SÁT thiết bị / phiên"
            if last_attempt_parts:
                remote_text += " · lần thử gần nhất: " + " · ".join(last_attempt_parts)
            self.prt_summary.setText(remote_text)
            self.prt_summary.setToolTip(
                "Remote Commander PRT\n"
                f"device_state={device_state}\n"
                f"device_session_id={device_session_id}\n"
                f"rpc_transport_state={rpc_state}\n"
                f"last_rpc_error={last_rpc_error or '—'}\n"
                f"execution_state={execution_state}\n"
                "RPC failure is transport evidence only; it does not prove device disconnect."
            )

            process_resource = dict(snapshot.get("process_resource") or {})
            process_supervisor = dict(snapshot.get("process_supervisor") or {})
            if process_resource.get("observed") is True:
                peak_mb = float(process_resource.get("peak_rss_bytes", 0) or 0) / (1024 * 1024)
                limit_mb = float(process_resource.get("rss_limit_bytes", 0) or 0) / (1024 * 1024)
                self.process_supervisor_summary.setText(
                    "TÀI NGUYÊN PROCESS · "
                    f"PID {process_resource.get('pid', '—')} · "
                    f"đỉnh {peak_mb:.1f} MB / {limit_mb:.0f} MB · "
                    f"{_vi_state(process_resource.get('resource_state', 'UNKNOWN'))}"
                )
            elif process_supervisor.get("verified") is True:
                resource_admission = dict(
                    process_supervisor.get("resource_admission") or {}
                )
                self.process_supervisor_summary.setText(
                    "TIẾN TRÌNH WORKER · "
                    f"{process_supervisor.get('process_count', 0)} process · "
                    f"{_vi_state(process_supervisor.get('state', 'UNKNOWN'))} · "
                    f"{process_supervisor.get('failed_process_count', 0)} lỗi · "
                    f"máy {resource_admission.get('host_id', 'chưa bind')}"
                )
            else:
                self.process_supervisor_summary.setText(
                    "TIẾN TRÌNH WORKER · CHƯA CÓ DỮ LIỆU"
                )

            host_shards = dict(
                snapshot.get("host_shards") or {}
            )
            if host_shards.get("verified") is True:
                counts = dict(host_shards.get("counts") or {})
                self.host_shard_summary.setText(
                    "HOST / SHARD · "
                    f"{counts.get('ACTIVE', 0)} đang gán · "
                    f"{host_shards.get('active_host_count', 0)} máy · "
                    f"{counts.get('EXPIRED_REQUIRES_REASSIGNMENT', 0)} cần gán lại"
                )
            else:
                self.host_shard_summary.setText(
                    "HOST / SHARD · chưa có gán shard vào máy"
                )

            backend_status = dict(snapshot.get("backend_status") or {})
            queue_backend = dict(backend_status.get("queue_backend") or {})
            queue_binding = dict(queue_backend.get("binding") or {})
            self.queue_backend_summary.setText(
                "BACKEND HÀNG ĐỢI · "
                f"{queue_binding.get('backend_id', 'chưa bind')} · "
                + (
                    "thiết kế multi-host / chưa Reality proof"
                    if queue_backend.get("current_backend_distributed_safe") is True and queue_backend.get("production_reality_proven") is not True
                    else (
                        "multi-host đã có Reality proof"
                        if queue_backend.get("production_reality_proven") is True
                        else "chỉ local / chưa phân tán"
                    )
                )
                + f" · {queue_backend.get('required_distributed_law_count', 0)} luật bắt buộc"
            )

            provider_eval = dict(backend_status.get("provider_evaluation") or {})
            provider_reality = dict(queue_backend.get("provider_reality") or {})
            self.provider_proof_summary.setText(
                "BACKEND PHÂN TÁN · "
                f"{provider_eval.get('required_scenario_count', 0)} bài test bắt buộc · "
                + (
                    f"PASS trên {provider_reality.get('distinct_host_count', 0)} host"
                    if queue_backend.get("production_reality_proven") is True
                    else "chưa có provider thật được chứng minh"
                )
            )

            recovery = dict(backend_status.get("recovery") or {})
            self.recovery_summary.setText(
                "KHÔI PHỤC · "
                f"{recovery.get('requires_explicit_recovery', 0)} việc cần quyền mới · "
                f"{recovery.get('cancelled', 0)} đã hủy · không tự chạy lại"
            )

            watcher_resources = dict(snapshot.get("watcher_resources") or {})
            watcher_rows = [dict(row) for row in watcher_resources.values() if isinstance(row, dict) and row.get("observed") is True]
            if watcher_rows:
                parent_mb = sum(float(row.get("watcher_rss_mb", 0) or 0) for row in watcher_rows)
                child_mb = max(float(row.get("child_rss_mb", 0) or 0) for row in watcher_rows)
                timeout_count = sum(int(row.get("timeout_count", 0) or 0) for row in watcher_rows)
                resource_state = "/".join(str(row.get("resource_state") or "NOT_OBSERVED") for row in watcher_rows)
                self.resource_summary.setText(f"TÀI NGUYÊN WATCHER · parent {parent_mb:.1f} MB · child max {child_mb:.1f} MB · timeout {timeout_count} · {resource_state}")
                self.resource_summary.setToolTip("BAM watcher resource projection\n" + json.dumps(watcher_resources, ensure_ascii=False, sort_keys=True, indent=2))
            else:
                resource_status = dict(backend_status.get("resource_admission") or {})
                if resource_status.get("verified") is True:
                    self.resource_summary.setText(f"TÀI NGUYÊN MÁY · máy {resource_status.get('host_id', 'chưa bind')} · {resource_status.get('process_count', 0)} process được cấp")
                else:
                    self.resource_summary.setText("TÀI NGUYÊN MÁY · chưa có lượt process được cấp tài nguyên")

            scale_metrics = dict(backend_status.get("scale_metrics") or {})
            self.scale_metrics_summary.setText(
                "QUAN SÁT SCALE · "
                + (
                    f"{scale_metrics.get('active_shards', 0)} shard đang có số liệu · "
                    f"fanout {scale_metrics.get('fanout', 0)} · "
                    f"cây {scale_metrics.get('root_level', 0)} tầng · không quét toàn bộ"
                    if scale_metrics.get("verified") is True
                    else "chưa có dữ liệu metrics · không fallback quét toàn bộ shard"
                )
            )

            progress_checkpoint = dict(backend_status.get("progress_checkpoint") or {})
            self.progress_checkpoint_summary.setText(
                "MỐC TIẾN ĐỘ · " + (
                    f"{progress_checkpoint.get('active_shards', 0)} shard đã có mốc · "
                    f"{progress_checkpoint.get('generation_sum', 0)} lần cập nhật kỹ thuật · chỉ hiển thị, không điều khiển"
                    if progress_checkpoint.get("verified") is True
                    else "chưa có mốc kỹ thuật · chỉ hiển thị, không tự tạo hay chạy lại"
                )
            )

            projection_sync = dict(backend_status.get("projection_sync") or {})
            dirty = int(projection_sync.get("dirty_shards", 0) or 0)
            self.projection_sync_summary.setText(
                "ĐỒNG BỘ HIỂN THỊ · "
                + (
                    "đã khớp với queue · sửa projection được khóa theo từng shard"
                    if projection_sync.get("state") == "SYNCED"
                    else f"{dirty} shard đang chờ sửa projection · sửa tuần tự, không chạy lại request"
                )
            )

            storage_health = dict(backend_status.get("storage_health") or {})
            failed_storage = int(storage_health.get("failed_shards", 0) or 0)
            checked_storage = int(storage_health.get("checked_shards", 0) or 0)
            self.storage_health_summary.setText(
                "SỨC KHỎE LƯU TRỮ · "
                + (
                    f"{checked_storage} shard đã kiểm tra nhanh · không phát hiện lệch contract"
                    if storage_health.get("state") == "BOUNDED_CONTRACT_PASS"
                    else f"{failed_storage} shard bị khóa vì lệch cấu trúc/version/counter · không tự sửa dữ liệu"
                )
            )

            deep_integrity = dict(backend_status.get("deep_storage_integrity") or {})
            deep_state = str(deep_integrity.get("state") or "DEEP_INTEGRITY_NOT_RUN")
            if deep_state == "PROJECTION_UNAVAILABLE_REQUIRES_REBUILD":
                deep_text = "cây tổng bị mất/hỏng · cần dựng lại, chưa được coi là sạch"
            elif deep_state == "DEEP_INTEGRITY_NOT_RUN":
                deep_text = "chưa chạy · chỉ chạy khi bảo trì yêu cầu"
            elif deep_state == "DEEP_INTEGRITY_PASS":
                deep_text = f"{deep_integrity.get('checked_shards', 0)} shard đã quét sâu · không phát hiện lỗi"
            else:
                deep_text = f"{deep_integrity.get('failed_shards', 0)} shard có lỗi · cần bảo trì thủ công, không tự sửa"
            self.deep_storage_integrity_summary.setText("KIỂM TRA SÂU DB · " + deep_text)

            logical_audit = dict(backend_status.get("queue_logical_audit") or {})
            logical_state = str(logical_audit.get("state") or "QUEUE_LOGICAL_AUDIT_NOT_RUN")
            if logical_state == "PROJECTION_UNAVAILABLE_REQUIRES_REBUILD":
                logical_text = "cây tổng bị mất/hỏng · cần dựng lại, chưa được coi là sạch"
            elif logical_state == "QUEUE_LOGICAL_AUDIT_NOT_RUN":
                logical_text = "chưa chạy · chỉ chạy khi bảo trì yêu cầu"
            elif logical_state == "QUEUE_LOGICAL_AUDIT_PASS":
                logical_text = f"{logical_audit.get('checked_shards', 0)} shard đã đối chiếu · logic kỹ thuật khớp"
            else:
                logical_text = f"{logical_audit.get('failed_shards', 0)} shard có trạng thái/counter lệch · cần bảo trì thủ công"
            self.queue_logical_audit_summary.setText("KIỂM TRA LOGIC HÀNG ĐỢI · " + logical_text)

            quarantine = dict(backend_status.get("storage_quarantine") or {})
            quarantined = int(quarantine.get("quarantined_shards", 0) or 0)
            self.storage_quarantine_summary.setText(
                "KHÓA BẢO TRÌ · " + (
                    "cây tổng bị mất/hỏng · cần dựng lại, chưa được coi là không khóa"
                    if quarantine.get("state") == "PROJECTION_UNAVAILABLE_REQUIRES_REBUILD"
                    else "không có shard bị khóa" if quarantined == 0
                    else f"{quarantined} shard đang bị khóa · không nhận việc mới cho tới clearance bảo trì"
                )
            )

            maintenance_evidence = dict(backend_status.get("maintenance_evidence") or {})
            evidence_state = str(maintenance_evidence.get("state") or "NO_MAINTENANCE_EVIDENCE")
            if evidence_state == "PROJECTION_UNAVAILABLE_REQUIRES_REBUILD":
                evidence_text = "cây tổng bị mất/hỏng · cần dựng lại trước khi tin trạng thái tổng"
            elif evidence_state == "NO_MAINTENANCE_EVIDENCE":
                evidence_text = "chưa có chuỗi bằng chứng"
            elif evidence_state == "MAINTENANCE_EVIDENCE_CURRENTLY_VERIFIED":
                evidence_text = f"{maintenance_evidence.get('event_count', 0)} sự kiện · chuỗi hiện đã kiểm tra"
            elif evidence_state == "MAINTENANCE_EVIDENCE_TAMPER_DETECTED":
                evidence_text = f"{maintenance_evidence.get('tamper_detected_shards', 0)} shard có bằng chứng bị sửa · clearance đang dừng"
            else:
                evidence_text = f"{maintenance_evidence.get('event_count', 0)} sự kiện · có thay đổi mới, cần kiểm tra lại"
            self.maintenance_evidence_summary.setText("BẰNG CHỨNG BẢO TRÌ · " + evidence_text)

            technical_status_rebuild = dict(backend_status.get("technical_status_rebuild") or {})
            rebuild_state = str(technical_status_rebuild.get("state") or "REBUILD_NOT_RUN")
            self.technical_status_rebuild_summary.setText(
                "TỔNG HỢP TRẠNG THÁI · " + (
                    "chưa cần dựng lại" if rebuild_state == "REBUILD_NOT_RUN"
                    else f"đã dựng lại {technical_status_rebuild.get('projection_count', 0)} nhóm trạng thái · chỉ projection, không chạy lại việc"
                )
            )

            receipt_aggregation = dict(backend_status.get("receipt_aggregation") or {})
            self.receipt_aggregation_summary.setText(
                "BIÊN NHẬN SCALE · "
                + (
                    f"{receipt_aggregation.get('receipt_count', 0)} biên nhận kỹ thuật · "
                    f"{receipt_aggregation.get('active_shards', 0)} shard · cây hash sẵn · không quét toàn bộ"
                    if receipt_aggregation.get("verified") is True
                    else "chưa có dữ liệu cây hash · không suy diễn thành công nghiệp vụ"
                )
            )

            mcp_status = dict(backend_status.get("mcp") or {})
            self.mcp_summary.setText(
                "CẦU MCP · "
                + (
                    "runtime có sẵn"
                    if mcp_status.get("sdk_runtime_available") is True
                    else "runtime chưa cài"
                )
                + " · execute + recover · status chỉ đọc · không có quyền semantic"
            )

            self.execution_view.setPlainText(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self._update_agent_projection(snapshot)
            self.operator_dashboard.update_snapshot(snapshot)
            self.live_state_label.setText("STATE · OBSERVED")

        except Exception as exc:
            self.execution_cell_summary.setText(
                "TẾ BÀO THỰC THI · không đọc được trạng thái"
            )
            self.scale_metrics_summary.setText(
                "QUAN SÁT SCALE · không đọc được trạng thái"
            )
            self.progress_checkpoint_summary.setText("MỐC TIẾN ĐỘ · không đọc được trạng thái")
            self.projection_sync_summary.setText("ĐỒNG BỘ HIỂN THỊ · không đọc được trạng thái")
            self.storage_health_summary.setText("SỨC KHỎE LƯU TRỮ · không đọc được trạng thái")
            self.deep_storage_integrity_summary.setText("KIỂM TRA SÂU DB · không đọc được trạng thái")
            self.queue_logical_audit_summary.setText("KIỂM TRA LOGIC HÀNG ĐỢI · không đọc được trạng thái")
            self.storage_quarantine_summary.setText("KHÓA BẢO TRÌ · không đọc được trạng thái")
            self.maintenance_evidence_summary.setText("BẰNG CHỨNG BẢO TRÌ · không đọc được trạng thái")
            self.technical_status_rebuild_summary.setText("TỔNG HỢP TRẠNG THÁI · không đọc được trạng thái")
            self.receipt_aggregation_summary.setText(
                "BIÊN NHẬN SCALE · không đọc được trạng thái"
            )
            self.shard_admission_summary.setText(
                "CHIA SHARD · không đọc được trạng thái"
            )
            self.stream_queue_summary.setText(
                "HÀNG ĐỢI · không đọc được trạng thái"
            )
            self.process_supervisor_summary.setText(
                "TIẾN TRÌNH WORKER · không đọc được trạng thái"
            )
            self.host_shard_summary.setText(
                "HOST / SHARD · không đọc được trạng thái"
            )
            self.queue_backend_summary.setText(
                "BACKEND HÀNG ĐỢI · không đọc được trạng thái"
            )
            self.provider_proof_summary.setText(
                "BACKEND PHÂN TÁN · không đọc được trạng thái"
            )
            self.recovery_summary.setText(
                "KHÔI PHỤC · không đọc được trạng thái"
            )
            self.resource_summary.setText(
                "TÀI NGUYÊN MÁY · không đọc được trạng thái"
            )
            self.mcp_summary.setText(
                "CẦU MCP · không đọc được trạng thái"
            )
            self.execution_view.setPlainText(
                "BAM execution projection unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
            self.agent_execution.setText("Technical projection unavailable")
            self.agent_capability.setText("Unavailable in BAM projection")
            self.agent_activity_value.setText("No verified agent activity source")
            self.agent_reality.setText(
                f"Projection unavailable: {type(exc).__name__}: {exc}"
            )
            self.operator_dashboard.update_error(f"{type(exc).__name__}: {exc}")
            self.live_state_label.setText("STATE · UNAVAILABLE")

    def _reset_agent_identity_states(self) -> None:
        for label in getattr(self, "_agent_state_labels", {}).values():
            label.setText("DETACHED · no verified gateway binding")
            label.setObjectName("agentDetachedState")

    def _mark_verified_agent_identity(self, requester_id: str, state: str) -> None:
        normalized = "".join(
            character
            for character in str(requester_id).casefold()
            if character.isalnum()
        )
        for name, label in getattr(self, "_agent_state_labels", {}).items():
            identity = "".join(
                character
                for character in name.casefold()
                if character.isalnum()
            )
            if identity and identity in normalized:
                label.setText(f"VERIFIED REQUEST · {state}")
                label.setObjectName("agentVerifiedState")
                return

    def _update_agent_projection(self, snapshot: dict[str, Any]) -> None:
        latest_execution = dict(snapshot.get("latest_execution") or {})
        latest_event = dict(snapshot.get("latest_event") or {})
        latest_receipt = dict(snapshot.get("latest_receipt") or {})
        evidence = dict(latest_event.get("evidence") or {})
        agent = dict(snapshot.get("agent_projection") or {})

        self._reset_agent_identity_states()

        if agent.get("verified") is True:
            requester_id = str(agent.get("requester_id") or "")
            request_id = str(agent.get("request_id") or "")
            capability_id = str(agent.get("capability_id") or "")
            execution_id = str(agent.get("execution_id") or "")
            execution_state = str(agent.get("state") or "unavailable")
            authority_id = str(agent.get("authority_id") or "unavailable")
            authority_version = str(agent.get("authority_version") or "unavailable")

            self.agent_button.setText("AGENTS · 1")
            self.agent_active_request.setText(
                f"{request_id}\nRequester · {requester_id}"
            )

            if capability_id:
                name, purpose, _ = tool_presentation(capability_id)
                self.agent_capability.setText(
                    f"{name}\n{purpose}"
                )
            else:
                self.agent_capability.setText("Capability unavailable")

            self.agent_execution.setText(
                f"{execution_id}\nState: {execution_state}"
            )
            self.agent_activity_value.setText(
                "VERIFIED EXTERNAL AGENT REQUEST\n"
                f"{requester_id} · {execution_state}"
            )
            self.agent_reality.setText(
                "Identity source: independently rebound external authority\n"
                f"Authority: {authority_id} · v{authority_version}\n"
                f"Durable source: {snapshot.get('source') or 'unavailable'}"
            )
            self._mark_verified_agent_identity(
                requester_id,
                execution_state,
            )
            return

        self.agent_button.setText("AGENTS · 0")

        capability_id = (
            evidence.get("capability_id")
            or latest_receipt.get("capability_id")
        )
        self.agent_capability.setText(
            str(capability_id)
            if capability_id
            else "Unavailable in BAM execution projection"
        )

        execution_id = (
            latest_execution.get("execution_id")
            or latest_event.get("execution_id")
            or latest_receipt.get("execution_id")
        )
        execution_state = (
            latest_receipt.get("state")
            or latest_event.get("to_state")
            or latest_execution.get("state")
        )
        if execution_id:
            self.agent_execution.setText(
                f"{execution_id}\nState: {execution_state or 'unavailable'}"
            )
        else:
            self.agent_execution.setText("No technical execution")

        event_id = latest_event.get("event_id")
        if event_id:
            self.agent_activity_value.setText(
                "BAM technical event only — no verified agent attribution\n"
                f"{event_id} · {latest_event.get('to_state') or 'state unavailable'}"
            )
        else:
            self.agent_activity_value.setText("No verified agent activity source")

        self.agent_active_request.setText(
            "Unavailable — requester authority is outside BAM projection"
        )
        self.agent_reality.setText(
            f"Agent binding: {agent.get('reason') or 'not verified'}\n"
            f"Source: {snapshot.get('source') or 'unavailable'}\n"
            f"Executions: {snapshot.get('execution_count', 0)} · "
            f"Events: {snapshot.get('event_count', 0)} · "
            f"Receipts: {snapshot.get('receipt_count', 0)}"
        )

    def log(self, text: str) -> None:
        self.console.appendPlainText(text)

    def show_receipt(self, receipt: Any) -> None:
        self.receipt_view.setPlainText(
            json.dumps(dict(receipt), ensure_ascii=False, indent=2)
        )
        self.receipt_dock.show()
        self.receipt_dock.raise_()
        self._refresh_execution_view()

    def closeEvent(self, event) -> None:
        try:
            self.browser_host.restore_all(show=False)
        except Exception:
            pass
        self.settings.setValue("layout/version", LAYOUT_VERSION)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue(
            "workspace/orientation",
            "vertical" if self.workspace_splitter.orientation() == Qt.Vertical else "horizontal",
        )
        self.settings.setValue("workspace/paneCount", len(self._panes))
        self.settings.setValue("workspace/splitterState", self.workspace_splitter.saveState())
        self.settings.sync()
        if self.session is not None:
            self.session.close()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        style_sheet = f"""
            QMainWindow {{
                background: {BG};
            }}

            QWidget {{
                color: {TEXT};
                font-family: "{self._global_font_family}";
                font-size: 14px;
            }}


            QWidget#operatorShell,
            QStackedWidget#centralStack,
            QWidget#operatorDashboard,
            QWidget#operatorBody,
            QWidget#editorPage {{
                background: #080B10;
            }}

            QFrame#navRail {{
                background: #090D12;
                border-right: 1px solid #18212B;
            }}

            QLabel#navBrand {{
                color: #F1F5F9;
                font-size: 20px;
                font-weight: 900;
                letter-spacing: 2px;
            }}

            QLabel#navCaption,
            QLabel#navBoundary {{
                color: #647182;
                font-size: 9px;
                font-weight: 800;
                letter-spacing: 1px;
            }}

            QToolButton#navButton {{
                background: transparent;
                color: #8794A3;
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 6px 7px;
                font-size: 10px;
                font-weight: 800;
                text-align: left;
            }}

            QToolButton#navButton:hover {{
                background: #111820;
                color: #DCE5ED;
                border-color: #202C38;
            }}

            QToolButton#navButton:checked {{
                background: #152435;
                color: #F3F7FA;
                border-color: #2E4960;
            }}

            QLabel#operatorEyebrow {{
                color: #6F8193;
                font-size: 10px;
                font-weight: 900;
                letter-spacing: 1px;
            }}

            QLabel#operatorTitle {{
                color: #F3F6F9;
                font-size: 24px;
                font-weight: 800;
            }}

            QLabel#operatorStateChip,
            QLabel#liveStatePill {{
                background: #0E1821;
                color: #86B8D8;
                border: 1px solid #29465C;
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 10px;
                font-weight: 800;
            }}

            QFrame#operatorCard {{
                background: #0D131A;
                border: 1px solid #1D2A36;
                border-radius: 10px;
                min-height: 116px;
            }}

            QLabel#operatorCardTitle {{
                color: #738296;
                font-size: 10px;
                font-weight: 900;
                letter-spacing: 1px;
                border: none;
            }}

            QLabel#operatorCardValue {{
                color: #D7E0E8;
                font-size: 13px;
                font-weight: 550;
                border: none;
            }}

            QPushButton#operatorQuickAction {{
                min-height: 28px;
                padding: 4px 10px;
                background: #101720;
                border-color: #22303D;
                font-size: 10px;
                font-weight: 800;
            }}

            QScrollArea#operatorScroll {{
                background: transparent;
                border: none;
            }}

            QMenuBar {{
                background: #080B10;
                color: #99A4B2;
                border: none;
                padding: 2px 8px;
            }}

            QMenuBar::item {{
                padding: 5px 8px;
                border-radius: 5px;
            }}

            QMenuBar::item:selected {{
                background: #121820;
                color: {TEXT};
            }}

            QMenu {{
                background: #0D1218;
                border: 1px solid #202A35;
                padding: 5px;
            }}

            QMenu::item {{
                padding: 7px 26px 7px 10px;
                border-radius: 5px;
            }}

            QMenu::item:selected {{
                background: #17202A;
            }}

            QToolBar#commandToolbar {{
                background: #090C11;
                border: none;
                border-bottom: 1px solid #151B22;
                min-height: 58px;
                spacing: 8px;
                padding: 4px 12px;
            }}

            QLabel#appTitle {{
                color: #F4F7FA;
                font-size: 17px;
                font-weight: 800;
                letter-spacing: 1px;
            }}

            QLabel#brandSubtitle {{
                color: #637080;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
                padding-top: 3px;
            }}

            QLabel#workspacePath {{
                color: #8793A1;
                font-size: 12px;
                padding-left: 14px;
            }}

            QToolButton#agentsButton {{
                color: #AAB5C0;
                background: #0D131A;
                border: 1px solid #202C38;
                border-radius: 8px;
                min-height: 32px;
                padding: 3px 14px;
                font-size: 11px;
                font-weight: 700;
            }}

            QToolButton#agentsButton:hover {{
                color: #F0F4F8;
                background: #131C26;
                border-color: #32475A;
            }}

            QWidget#mcpPanel {{
                background: #0B0F14;
            }}


            QComboBox#mcpField,
            QLineEdit#mcpField,
            QSpinBox#mcpField {{
                background: #0D131A;
                color: #E6EDF3;
                border: 1px solid #243342;
                border-radius: 6px;
                padding: 5px 8px;
                min-height: 24px;
                selection-background-color: #254764;
            }}
            QComboBox#mcpField QAbstractItemView {{
                background: #0D131A;
                color: #E6EDF3;
                border: 1px solid #243342;
                selection-background-color: #254764;
            }}

            QLabel#mcpPurpose {{
                color: #A8B4C1;
                font-size: 12px;
                font-weight: 700;
            }}

            QLabel#mcpBoundary {{
                color: #8B98A7;
                background: #0D131A;
                border: 1px solid #1C2834;
                border-radius: 8px;
                padding: 9px;
                font-size: 11px;
            }}

            QLabel#mcpSectionLabel {{
                color: #718296;
                font-size: 9px;
                font-weight: 900;
                letter-spacing: 1px;
                padding-top: 4px;
            }}

            QLabel#mcpStatusValue {{
                background: #0E1821;
                color: #DCEAF4;
                border: 1px solid #29465C;
                border-radius: 8px;
                padding: 9px 10px;
                font-size: 11px;
                font-weight: 700;
            }}

            QToolButton#mcpAdvancedButton {{
                color: #8E9CAB;
                background: transparent;
                border: none;
                text-align: left;
                padding: 5px 2px;
                font-size: 11px;
            }}

            QToolButton#mcpAdvancedButton:hover {{
                color: #D9E2EA;
            }}

            QPushButton#mcpPrimaryAction {{
                background: #19334A;
                border-color: #315876;
                color: #EAF4FB;
                font-weight: 700;
            }}

            QPlainTextEdit#mcpDetail {{
                background: #080C11;
                color: #AEB8C3;
                border: 1px solid #17212B;
                border-radius: 7px;
                padding: 9px;
                font-family: "{self._global_font_family}";
                font-size: 10px;
            }}

            QWidget#agentDrawer {{
                background: #0B0F14;
            }}

            QLabel#agentBoundary {{
                color: #778494;
                font-size: 10px;
            }}

            QFrame#agentIdentity,
            QFrame#agentProjection {{
                background: #0D131A;
                border: 1px solid #1C2834;
                border-radius: 8px;
            }}

            QLabel#agentIdentityImage {{
                background: transparent;
                border: none;
            }}

            QLabel#agentIdentityName {{
                color: #EFF4F8;
                font-size: 12px;
                font-weight: 700;
                border: none;
            }}

            QLabel#agentIdentityRole {{
                color: #8290A0;
                font-size: 9px;
                border: none;
            }}

            QLabel#agentDetachedState {{
                color: #C99B58;
                font-size: 9px;
                border: none;
            }}

            QToolButton#modeButton {{
                background: transparent;
                color: #7F8B99;
                border: none;
                border-radius: 5px;
                min-height: 25px;
                padding: 2px 8px;
            }}

            QToolButton#modeButton:hover {{
                color: #D8E0E8;
                background: #111820;
            }}

            QToolButton#modeButton:checked {{
                color: #EAF4FC;
                background: #152536;
            }}

            QToolButton#modeButton:disabled {{
                color: #46515E;
            }}

            QFrame#agentStatusCard {{
                background: #0D131A;
                border: 1px solid #202B37;
                border-radius: 8px;
            }}

            QLabel#agentHead {{
                background: transparent;
                border: none;
            }}

            QLabel#agentName {{
                color: #EDF2F7;
                font-size: 12px;
                font-weight: 700;
            }}

            QLabel#agentState {{
                color: #798696;
                font-size: 10px;
            }}

            QProgressBar {{
                background: #171F28;
                border: none;
                border-radius: 2px;
            }}

            QProgressBar::chunk {{
                background: {CYAN};
                border-radius: 2px;
            }}

            QToolButton#statusOverlay {{
                background: transparent;
                border: none;
            }}

            QToolButton#addAgentButton {{
                color: #667382;
                background: #0B1016;
                border: 1px solid #1B2530;
                border-radius: 8px;
                min-width: 66px;
                min-height: 48px;
            }}

            QToolBar#studioToolbar {{
                background: #090D12;
                border: none;
                border-top: 1px solid #171F28;
                spacing: 4px;
                padding: 3px 8px;
            }}

            QToolBar#studioToolbar QToolButton {{
                color: #8793A1;
                background: transparent;
                border: 1px solid transparent;
                border-radius: 5px;
                min-height: 24px;
                padding: 2px 10px;
            }}

            QToolBar#studioToolbar QToolButton:hover {{
                color: #E1E7ED;
                background: #121922;
                border-color: #202C38;
            }}

            QToolBar#studioDockToolbar {{
                background: #080C11;
                border: none;
                border-top: 1px solid #171F28;
                min-height: 68px;
                padding: 0;
            }}

            QWidget#studioDockSurface {{
                background: #080C11;
            }}

            QFrame#studioCard {{
                background: #0C1219;
                border: 1px solid #1B2835;
                border-radius: 6px;
            }}

            QFrame#studioCard:hover {{
                background: #101923;
                border-color: #2A3C4E;
            }}

            QLabel#studioCardTitle {{
                color: #EEF3F8;
                font-size: 10px;
                font-weight: 700;
                border: none;
            }}

            QLabel#studioCardDetail {{
                color: #788696;
                font-size: 9px;
                border: none;
            }}

            QLabel#studioCardState {{
                color: {CYAN};
                font-size: 9px;
                border: none;
            }}

            QDockWidget {{
                background: {PANEL};
                color: #8895A5;
                font-weight: 700;
                border: none;
            }}

            QDockWidget::title {{
                background: #0A0E13;
                color: #768392;
                border: none;
                padding: 7px 10px;
                font-size: 10px;
            }}

            QWidget#filesSurface,
            QWidget#commandPanel,
            QWidget#skillCatalogPanel,
            QWidget#chromeProfilesPanel {{
                background: #0B0F14;
            }}

            QLabel#skillSummary {{
                color: #7D8997;
                font-size: 10px;
            }}

            QListWidget#skillList {{
                background: #090E14;
                padding: 4px;
            }}

            QListWidget#skillList::item {{
                min-height: 28px;
                padding: 3px 7px;
                border-radius: 5px;
                color: #AAB5C0;
            }}

            QListWidget#skillList::item:hover {{
                background: #111922;
                color: #E1E7ED;
            }}

            QListWidget#skillList::item:selected {{
                background: #162536;
                color: #F1F5F8;
            }}

            QTextEdit#skillDetail {{
                background: #080C11;
                color: #AEB8C3;
                border: 1px solid #17212B;
                border-radius: 7px;
                padding: 9px;
                font-family: "{self._global_font_family}";
                font-size: 10px;
            }}

            QLabel#sectionLabel {{
                color: #687587;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1px;
            }}

            QLabel#commandTitle {{
                color: #F2F5F8;
                font-size: 18px;
                font-weight: 750;
            }}

            QLabel#commandMeta {{
                color: #7D8997;
                font-size: 11px;
            }}

            QLabel#lastResult {{
                color: #8491A0;
                background: #0D131A;
                border: 1px solid #17212B;
                border-radius: 8px;
                padding: 10px;
                font-size: 11px;
            }}

            QLineEdit,
            QTextEdit,
            QListWidget {{
                background: #0D1218;
                color: #DCE3EA;
                border: 1px solid #1B2530;
                border-radius: 8px;
                selection-background-color: #204565;
                selection-color: #FFFFFF;
            }}

            QLineEdit {{
                min-height: 30px;
                padding: 2px 10px;
            }}

            QLineEdit:focus,
            QTextEdit:focus {{
                border-color: #365A78;
            }}

            QLineEdit#fileSearch,
            QLineEdit#capabilitySearch {{
                background: #0A0E13;
                color: #B9C2CC;
            }}

            QListWidget#capabilityList {{
                padding: 4px;
            }}

            QListWidget#capabilityList::item {{
                border: none;
                border-radius: 6px;
                min-height: 28px;
                padding: 4px 7px;
                color: #9AA5B2;
            }}

            QListWidget#capabilityList::item:hover {{
                background: #111922;
                color: #DCE4EC;
            }}

            QListWidget#capabilityList::item:selected {{
                background: #162536;
                color: #EFF5FA;
            }}

            QTreeView#fileTree {{
                background: transparent;
                color: #AAB5C0;
                border: none;
                outline: none;
            }}

            QTreeView#fileTree::item {{
                min-height: 28px;
                padding: 2px 4px;
                border-radius: 5px;
            }}

            QTreeView#fileTree::item:hover {{
                background: #111820;
                color: #E1E7ED;
            }}

            QTreeView#fileTree::item:selected {{
                background: #162331;
                color: #F3F6F8;
            }}

            QHeaderView::section {{
                background: #0D131A;
                color: #718091;
                border: none;
                border-right: 1px solid #18212B;
                padding: 6px 7px;
                font-size: 10px;
            }}

            QTabWidget::pane {{
                background: #090D12;
                border: none;
            }}

            QTabBar::tab {{
                background: #0B1016;
                color: #6F7C8B;
                border: none;
                padding: 10px 16px;
                margin-right: 2px;
            }}

            QTabBar::tab:selected {{
                background: #101720;
                color: #EDF2F7;
                border-bottom: 2px solid {CYAN};
            }}

            QTabBar::tab:hover:!selected {{
                color: #B8C2CC;
                background: #0E151D;
            }}

            QPlainTextEdit#codeEditor {{
                background: #090D12;
                color: #DDE4EB;
                border: none;
                padding: 16px 18px;
                selection-background-color: #244661;
            }}

            QTextEdit#workspaceWelcome {{
                background: #090D12;
                color: #99A5B2;
                border: none;
                padding: 30px;
            }}

            QPushButton {{
                background: #111820;
                color: #C7D0D9;
                border: 1px solid #202C38;
                border-radius: 8px;
                padding: 7px 11px;
            }}

            QPushButton:hover {{
                background: #17212C;
                border-color: #33475A;
            }}

            QPushButton#primaryRunButton {{
                background: #438DCA;
                color: #FFFFFF;
                border: none;
                border-radius: 9px;
                font-size: 13px;
                font-weight: 800;
                letter-spacing: 1px;
            }}

            QPushButton#primaryRunButton:hover {{
                background: #4D9BDD;
            }}

            QPushButton#primaryRunButton:pressed {{
                background: #377DB7;
            }}

            QPushButton#primaryRunButton:disabled {{
                background: #17212B;
                color: #596675;
            }}

            QScrollArea#parameterScroll {{
                background: transparent;
                border: none;
            }}

            QWidget#parameterSurface {{
                background: transparent;
            }}

            QTextEdit#evidenceView,
            QPlainTextEdit#consoleView {{
                background: #080C11;
                color: #AEB8C3;
                border: none;
                padding: 12px;
                font-family: "{self._global_font_family}";
                font-size: 11px;
            }}

            QSplitter::handle {{
                background: #151C24;
                width: 1px;
                height: 1px;
            }}

            QScrollBar:vertical {{
                width: 8px;
                background: transparent;
            }}

            QScrollBar::handle:vertical {{
                background: #283441;
                min-height: 30px;
                border-radius: 4px;
            }}

            QScrollBar::handle:vertical:hover {{
                background: #364554;
            }}

            QScrollBar:horizontal {{
                height: 8px;
                background: transparent;
            }}

            QScrollBar::handle:horizontal {{
                background: #283441;
                min-width: 30px;
                border-radius: 4px;
            }}

            QScrollBar::add-line,
            QScrollBar::sub-line,
            QScrollBar::add-page,
            QScrollBar::sub-page {{
                background: transparent;
                border: none;
            }}
        """
        style_sheet += '\nQWidget#pluginDockPanel { background: #10151D; }\nQLabel#connectionTitle { font-size: 18px; font-weight: 600; color: #EDF2F7; }\nQLabel#connectionSelectionTitle { font-size: 15px; font-weight: 600; color: #EDF2F7; }\nQLabel#pluginSummary { font-size: 12px; color: #A9B5C3; }\nQLabel#connectionFootnote { font-size: 11px; color: #96A3B3; }\nQListWidget#pluginList { border: none; background: transparent; outline: none; }\nQListWidget#pluginList::item { padding: 9px 10px; border-radius: 7px; color: #B8C4D2; }\nQListWidget#pluginList::item:selected { background: #1B3042; color: #EEF6FF; }\nQListWidget#pluginList::item:hover { background: #192430; }\nQPushButton#pluginToggle { background: #294D68; border: none; border-radius: 7px; color: #F0F6FC; }\nQPushButton#pluginToggle:hover { background: #346080; }\nQPushButton#pluginToggle:disabled { background: #202A35; color: #798595; }\n'
        style_sheet += 'QWidget#pluginDockPanel QComboBox, QWidget#pluginDockPanel QToolButton { background: #192430; color: #B8C4D2; border: 1px solid #293846; border-radius: 5px; padding: 6px; } QWidget#pluginDockPanel QComboBox QAbstractItemView { background: #192430; color: #EDF2F7; selection-background-color: #294D68; }'
        self.setStyleSheet(self._scale_style_sheet(style_sheet, self._global_font_size))


def run_workbench(
    initial_workspace: str | Path | None = None,
    *,
    storage_root: str | Path | None = None,
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)

    workspace = (
        Path(initial_workspace).resolve()
        if initial_workspace
        else None
    )

    window = BlackArmorWorkbench(
        workspace,
        storage_root=(
            Path(storage_root)
            if storage_root is not None
            else None
        ),
    )
    window.show()
    return app.exec()

