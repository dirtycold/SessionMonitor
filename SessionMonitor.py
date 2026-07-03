#!/usr/bin/env python3
"""SessionMonitor tray application."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qtpy.QtCore import QEvent
from qtpy.QtCore import QObject
from qtpy.QtCore import QPoint
from qtpy.QtCore import Qt
from qtpy.QtCore import QTimer
from qtpy.QtCore import Signal
from qtpy.QtCore import Slot
from qtpy.QtDBus import QDBusConnection
from qtpy.QtDBus import QDBusObjectPath
from qtpy.QtGui import QCloseEvent
from qtpy.QtGui import QIcon
from qtpy.QtWidgets import QAction
from qtpy.QtWidgets import QApplication
from qtpy.QtWidgets import QAbstractItemView
from qtpy.QtWidgets import QDialog
from qtpy.QtWidgets import QDialogButtonBox
from qtpy.QtWidgets import QHBoxLayout
from qtpy.QtWidgets import QHeaderView
from qtpy.QtWidgets import QMainWindow
from qtpy.QtWidgets import QMenu
from qtpy.QtWidgets import QMessageBox
from qtpy.QtWidgets import QStyle
from qtpy.QtWidgets import QSystemTrayIcon
from qtpy.QtWidgets import QTextEdit
from qtpy.QtWidgets import QToolButton
from qtpy.QtWidgets import QTreeWidget
from qtpy.QtWidgets import QTreeWidgetItem
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget


APP_NAME = "SessionMonitor"
NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
DEFAULT_ICON = "utilities-terminal"


@dataclass(frozen=True)
class ProcessInfo:
    pid: str
    name: str
    args: str


@dataclass(frozen=True)
class UtmpEntry:
    user: str
    tty: str
    source: str
    application: str


@dataclass(frozen=True)
class LoginSession:
    session_id: str
    uid: str
    user: str
    seat: str
    tty: str
    properties: dict[str, str]
    processes: list[ProcessInfo]
    utmp: UtmpEntry | None

    @property
    def remote(self) -> bool:
        return self.properties.get("Remote", "").lower() == "yes"

    @property
    def remote_host(self) -> str:
        return self.properties.get("RemoteHost", "")

    @property
    def service(self) -> str:
        return self.properties.get("Service", "")

    @property
    def session_type(self) -> str:
        return self.properties.get("Type", "")

    @property
    def session_class(self) -> str:
        return self.properties.get("Class", "")

    @property
    def display(self) -> str:
        return self.properties.get("Display", "")

    @property
    def state(self) -> str:
        return self.properties.get("State", "")

    @property
    def life(self) -> str:
        if self.utmp is not None:
            return "alive"
        if self.state in {"active", "online", "opening"}:
            return "alive"
        if self.state == "closing":
            return "closing" if self.processes else "dead"
        return "unknown"

    @property
    def kind(self) -> str:
        return "remote" if self.remote else "local"

    @property
    def source(self) -> str:
        if self.utmp is not None and self.utmp.source:
            return self.utmp.source
        if self.remote_host:
            return self.remote_host
        if self.display:
            return self.display
        if self.seat:
            return self.seat
        if self.tty:
            return self.tty
        return "-"

    @property
    def application(self) -> str:
        if self.utmp is not None and self.utmp.application != "-":
            return self.utmp.application

        process_text = " ".join(
            [process.name for process in self.processes]
            + [process.args for process in self.processes]
        )
        lowered_process_text = process_text.lower()

        if "mosh-server" in lowered_process_text:
            return "mosh"
        if "sftp-server" in lowered_process_text or "internal-sftp" in lowered_process_text:
            return "sftp"
        if "codex app-server" in lowered_process_text or "/.codex/" in lowered_process_text:
            return "codex"
        if (
            ".vscode-server" in lowered_process_text
            or "code-server" in lowered_process_text
            or "remotessh" in lowered_process_text
        ):
            return "vscode"
        if self.service == "sshd":
            return "ssh"
        if self.service:
            return self.service
        if self.processes:
            return self.processes[0].name
        return "-"

    @property
    def details(self) -> str:
        parts = []
        if self.tty:
            parts.append(f"tty={self.tty}")
        if self.session_type:
            parts.append(f"type={self.session_type}")
        if self.session_class:
            parts.append(f"class={self.session_class}")
        return ", ".join(parts) or "-"


@dataclass(frozen=True)
class SessionView:
    session_id: str
    user: str
    life: str
    state: str
    kind: str
    source: str
    application: str
    details: str


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    state: str
    application: str
    view: SessionView | None = None


class UpdateKind(Enum):
    SNAPSHOT = "SNAPSHOT"
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    CHANGED = "CHANGED"


def session_sort_key(session_id: str) -> tuple[int, int | str]:
    return (0, int(session_id)) if session_id.isdigit() else (1, session_id)


class QtLogindSessionMonitor(QObject):
    """Fetch and monitor login sessions using logind events."""

    updated = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._views: dict[str, SessionView] = {}
        self.last_error = ""
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(200)
        self._refresh_timer.timeout.connect(self.refresh)

    def start(self) -> bool:
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            self.last_error = "system D-Bus is not connected"
            return False

        connected_new = bus.connect(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "SessionNew",
            self._on_session_signal,
        )
        connected_removed = bus.connect(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "SessionRemoved",
            self._on_session_signal,
        )

        if not connected_new or not connected_removed:
            self.last_error = "failed to connect to logind session signals"
            return False

        self.refresh()
        return True

    @Slot(str, QDBusObjectPath)
    def _on_session_signal(self, session_id: str, path: QDBusObjectPath) -> None:
        _ = session_id
        _ = path
        self._refresh_timer.start()

    @Slot()
    def refresh(self) -> None:
        try:
            current = self.session_views()
        except RuntimeError as exc:
            self.last_error = str(exc)
            print(self.last_error, file=sys.stderr)
            return

        if not self._views:
            self._views = current
            self.updated.emit(UpdateKind.SNAPSHOT, current)
            return

        previous = self._views
        previous_ids = set(previous)
        current_ids = set(current)

        added = {
            session_id: current[session_id]
            for session_id in sorted(current_ids - previous_ids, key=session_sort_key)
        }
        removed = {
            session_id: previous[session_id]
            for session_id in sorted(previous_ids - current_ids, key=session_sort_key)
        }
        changed = {
            session_id: current[session_id]
            for session_id in sorted(previous_ids & current_ids, key=session_sort_key)
            if previous[session_id] != current[session_id]
        }

        if added:
            self.updated.emit(UpdateKind.ADDED, added)
        if removed:
            self.updated.emit(UpdateKind.REMOVED, removed)
        if changed:
            self.updated.emit(UpdateKind.CHANGED, changed)

        self._views = current

    def session_views(self) -> dict[str, SessionView]:
        sessions = self.list_sessions()
        related = self.related_alive_sessions(sessions)
        views: dict[str, SessionView] = {}

        for session in sessions:
            details = session.details
            related_ids = [
                session_id
                for session_id in related.get(
                    (session.user, session.kind, session.source, session.application),
                    [],
                )
                if session_id != session.session_id
            ]
            if session.life == "dead" and related_ids:
                details = f"{details}, related={','.join(related_ids)}"

            views[session.session_id] = SessionView(
                session_id=session.session_id,
                user=session.user,
                life=session.life,
                state=session.state,
                kind=session.kind,
                source=session.source,
                application=session.application,
                details=details,
            )

        return views

    def related_alive_sessions(
        self,
        sessions: list[LoginSession],
    ) -> dict[tuple[str, str, str, str], list[str]]:
        related: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)

        for session in sessions:
            if session.life != "alive":
                continue
            key = (session.user, session.kind, session.source, session.application)
            related[key].append(session.session_id)

        for session_ids in related.values():
            session_ids.sort(key=session_sort_key)

        return related

    def list_sessions(self) -> list[LoginSession]:
        output = self.run_loginctl(["list-sessions", "--no-legend"])
        utmp_entries = self.list_utmp_entries()
        sessions: list[LoginSession] = []

        for line in output.splitlines():
            columns = line.split()
            if not columns:
                continue

            session_id = columns[0]
            properties = self.show_session(session_id)
            sessions.append(
                LoginSession(
                    session_id=session_id,
                    uid=properties.get("User", ""),
                    user=properties.get("Name", ""),
                    seat=properties.get("Seat", ""),
                    tty=properties.get("TTY", ""),
                    properties=properties,
                    processes=self.session_processes(properties),
                    utmp=self.matching_utmp_entry(properties, utmp_entries),
                )
            )

        return sessions

    def run_loginctl(self, args: list[str]) -> str:
        result = self.run_command(["loginctl", *args, "--no-pager"])
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"loginctl failed: {message}")
        return result.stdout

    def show_session(self, session_id: str) -> dict[str, str]:
        output = self.run_loginctl(["show-session", session_id])
        properties: dict[str, str] = {}

        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value

        return properties

    def list_utmp_entries(self) -> list[UtmpEntry]:
        result = self.run_command(["who"])
        if result.returncode != 0:
            return []

        entries: list[UtmpEntry] = []
        for line in result.stdout.splitlines():
            entry = self.parse_who_line(line)
            if entry is not None:
                entries.append(entry)

        return entries

    def parse_who_line(self, line: str) -> UtmpEntry | None:
        columns = line.split(maxsplit=4)
        if len(columns) < 2:
            return None

        user = columns[0]
        tty = columns[1]
        if tty[:4].isdigit() and "-" in tty:
            return None

        comment = columns[4].strip() if len(columns) == 5 else ""
        source = "-"
        application = "-"

        if comment.startswith("(") and comment.endswith(")"):
            comment = comment[1:-1].strip()

        if comment:
            source = comment
            lowered = comment.lower()
            if "mosh" in lowered:
                application = "mosh"
                source = comment.split(" via ", maxsplit=1)[0].strip() or source
            elif "ssh" in lowered:
                application = "ssh"

        return UtmpEntry(user=user, tty=tty, source=source, application=application)

    def matching_utmp_entry(
        self,
        properties: dict[str, str],
        entries: list[UtmpEntry],
    ) -> UtmpEntry | None:
        tty = properties.get("TTY", "")
        remote = properties.get("Remote", "").lower() == "yes"
        remote_host = properties.get("RemoteHost", "")
        service = properties.get("Service", "")
        state = properties.get("State", "")
        user = properties.get("Name", "")

        for entry in entries:
            if not tty or entry.tty != tty:
                continue
            if remote and entry.source.startswith(":"):
                continue
            if remote and entry.source == "-":
                continue
            if user and entry.user != user:
                continue
            if remote_host and entry.source not in {remote_host, "-"}:
                continue
            if remote_host and entry.source == "-":
                continue
            if not remote or entry.source == remote_host or entry.application != "-":
                return entry

        if remote and service == "sshd" and state == "closing":
            for entry in entries:
                if entry.application != "mosh":
                    continue
                if user and entry.user != user:
                    continue
                if remote_host and entry.source == remote_host:
                    return entry

        return None

    def session_processes(self, properties: dict[str, str]) -> list[ProcessInfo]:
        leader = properties.get("Leader")
        pids = self.cgroup_pids(properties)

        if pids:
            processes = [process for pid in pids if (process := self.read_process(pid))]
            if processes:
                return processes[:32]

        session_status_processes = self.session_status_processes(properties)
        if session_status_processes:
            return session_status_processes[:32]

        if not leader:
            return []

        found: list[ProcessInfo] = []
        queue = [leader]
        seen = set()

        while queue and len(found) < 32:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)

            process = self.read_process(pid)
            if process is not None:
                found.append(process)

            queue.extend(self.child_pids(pid))

        return found

    def session_status_processes(self, properties: dict[str, str]) -> list[ProcessInfo]:
        session_id = properties.get("Id")
        if not session_id:
            return []

        result = self.run_command(
            ["loginctl", "session-status", session_id, "--no-pager"],
        )
        if result.returncode != 0:
            return []

        processes: list[ProcessInfo] = []
        for line in result.stdout.splitlines():
            match = re.search(r"[\u251c\u2514]\u2500\s*(\d+)\s+(.+)$", line)
            if match is None:
                continue

            pid = match.group(1)
            args = match.group(2).strip()
            name = args.split(maxsplit=1)[0].strip('"')
            processes.append(ProcessInfo(pid=pid, name=name, args=args))

        return processes

    def cgroup_pids(self, properties: dict[str, str]) -> list[str]:
        control_group = properties.get("ControlGroup")
        if not control_group:
            return []

        root = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        if not root.exists():
            return []

        pids: list[str] = []
        for procs_file in root.rglob("cgroup.procs"):
            try:
                pids.extend(
                    line.strip() for line in procs_file.read_text().splitlines()
                    if line.strip()
                )
            except OSError:
                continue

        return pids

    def read_process(self, pid: str) -> ProcessInfo | None:
        result = self.run_process(["ps", "-p", pid, "-o", "comm=", "-o", "args="])
        if result.returncode != 0:
            return None

        line = result.stdout.strip()
        if not line:
            return None

        name, _, args = line.partition(" ")
        return ProcessInfo(pid=pid, name=name, args=args.strip())

    def child_pids(self, pid: str) -> list[str]:
        result = self.run_process(["ps", "--no-headers", "-o", "pid=", "--ppid", pid])
        if result.returncode != 0:
            return []

        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def run_process(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run_command(args)

    def run_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, check=False, capture_output=True, text=True)


class DesktopNotifier:
    """Send freedesktop desktop notifications over the session bus."""

    def __init__(self) -> None:
        self.last_error = ""
        self._interface = None

    def send(
        self,
        summary: str,
        body: str,
        expire_timeout_ms: int = 5000,
        icon: str = DEFAULT_ICON,
    ) -> bool:
        try:
            dbus = self._dbus()
            interface = self._notification_interface()
            interface.Notify(
                APP_NAME,
                dbus.UInt32(0),
                icon,
                summary,
                body,
                dbus.Array([], signature="s"),
                dbus.Dictionary({}, signature="sv"),
                dbus.Int32(expire_timeout_ms),
            )
        except Exception as exc:  # dbus-python raises DBusException; import can fail too.
            self.last_error = str(exc)
            return False

        self.last_error = ""
        return True

    def _notification_interface(self) -> object:
        if self._interface is not None:
            return self._interface

        dbus = self._dbus()
        bus = dbus.SessionBus()
        service = bus.get_object(NOTIFICATIONS_SERVICE, NOTIFICATIONS_PATH)
        self._interface = dbus.Interface(
            service,
            dbus_interface=NOTIFICATIONS_INTERFACE,
        )
        return self._interface

    def _dbus(self) -> object:
        import dbus

        return dbus


class HoverActionTree(QTreeWidget):
    """QTreeWidget that shows row actions only for the hovered row."""

    row_hovered = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched == self.viewport():
            if event.type() == QEvent.MouseMove:
                point = event.pos()
                if isinstance(point, QPoint):
                    self.row_hovered.emit(self.itemAt(point))
            elif event.type() in {QEvent.Leave, QEvent.FocusOut}:
                self.row_hovered.emit(None)

        return super().eventFilter(watched, event)


class SessionWindow(QMainWindow):
    """Main session list window."""

    def __init__(self, monitor: QtLogindSessionMonitor) -> None:
        super().__init__()
        self._monitor = monitor
        self.setWindowTitle("SessionMonitor")
        self.resize(760, 420)

        self._action_widgets: list[tuple[QTreeWidgetItem, QWidget]] = []
        self._tree = HoverActionTree()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Session ID", "State", "Application", ""])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.setUniformRowHeights(True)
        self._tree.setIndentation(0)
        self._tree.row_hovered.connect(self._set_hovered_item)

        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        footer_layout.setSpacing(6)

        self._about_button = self._tool_button("About", QStyle.SP_MessageBoxInformation)
        self._about_button.clicked.connect(self._show_about)

        self._refresh_button = self._tool_button("Refresh", QStyle.SP_BrowserReload)
        self._refresh_button.clicked.connect(self._monitor.refresh)

        footer_layout.addWidget(self._about_button)
        footer_layout.addWidget(self._refresh_button)
        footer_layout.addStretch(1)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._tree)
        layout.addWidget(footer)
        self.setCentralWidget(content)

        self.set_sessions({})

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def _tool_button(self, text: str, standard_pixmap: QStyle.StandardPixmap) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(text)
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        return button

    def set_sessions(self, views: dict[str, SessionView]) -> None:
        self._tree.clear()
        self._action_widgets.clear()

        rows = [
            SessionRow(
                session_id=view.session_id,
                state=view.state or view.life,
                application=view.application,
                view=view,
            )
            for _session_id, view in sorted(
                views.items(),
                key=lambda item: session_sort_key(item[0]),
            )
        ]

        for row in rows:
            item = QTreeWidgetItem([row.session_id, row.state, row.application, ""])
            item.setData(0, Qt.UserRole, row)
            self._tree.addTopLevelItem(item)

            actions = self._row_actions(row)
            actions.setVisible(False)
            self._action_widgets.append((item, actions))
            self._tree.setItemWidget(item, 3, actions)
            actions.setVisible(False)

        self._set_hovered_item(None)

    def _row_actions(self, row: SessionRow) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        details = QToolButton()
        details.setText("Details")
        details.setToolTip(f"Show details for session {row.session_id}")
        details.setIcon(self.style().standardIcon(QStyle.SP_FileDialogInfoView))
        details.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        details.clicked.connect(lambda: self._show_details(row))

        terminate = QToolButton()
        terminate.setText("Terminate")
        terminate.setToolTip(f"Terminate session {row.session_id}")
        terminate.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        terminate.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        terminate.setPopupMode(QToolButton.MenuButtonPopup)
        terminate.clicked.connect(lambda: self._request_terminate(row))

        menu = QMenu(terminate)
        kill_action = QAction("Kill", terminate)
        kill_action.triggered.connect(lambda: self._request_kill(row))
        menu.addAction(kill_action)
        terminate.setMenu(menu)

        layout.addStretch(1)
        layout.addWidget(details)
        layout.addWidget(terminate)
        return wrapper

    def _set_hovered_item(self, hovered_item: QTreeWidgetItem | None) -> None:
        for item, widget in self._action_widgets:
            widget.setVisible(item is hovered_item)

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About SessionMonitor",
            "SessionMonitor tray UI skeleton.",
        )

    def _show_details(self, row: SessionRow) -> None:
        result = subprocess.run(
            ["loginctl", "session-status", row.session_id, "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
        )
        details = result.stdout.strip()
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
        if not details:
            details = f"No details returned for session {row.session_id}."

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Session {row.session_id} Details")
        dialog.resize(720, 480)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.NoWrap)
        text.setPlainText(details)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)

        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        layout.addWidget(buttons)

        dialog.exec()

    def _request_terminate(self, row: SessionRow) -> None:
        QMessageBox.information(
            self,
            "Terminate Session",
            f"Terminate session {row.session_id} will be implemented later.",
        )

    def _request_kill(self, row: SessionRow) -> None:
        QMessageBox.warning(
            self,
            "Kill Session",
            f"Kill session {row.session_id} will be implemented later.",
        )


class SessionTrayApp:
    """Owns the application window and tray icon."""

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._monitor = QtLogindSessionMonitor()
        self._notifier = DesktopNotifier()
        self._sessions: dict[str, SessionView] = {}
        self._window = SessionWindow(self._monitor)
        self._tray = QSystemTrayIcon(self._tray_icon(), self._window)
        self._tray.setToolTip("SessionMonitor")
        self._tray.activated.connect(self._on_tray_activated)
        self._monitor.updated.connect(self._on_sessions_updated)

        menu = QMenu()
        open_action = QAction("Open", menu)
        open_action.triggered.connect(self.open_window)
        exit_action = QAction("Exit", menu)
        exit_action.triggered.connect(self.exit_app)

        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self._tray.setContextMenu(menu)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()
            self.open_window()
        else:
            QMessageBox.warning(
                self._window,
                "SessionMonitor",
                "System tray is not available. Opening the window instead.",
            )
            self.open_window()

    def start_monitor(self) -> bool:
        if self._monitor.start():
            return True

        QMessageBox.warning(
            self._window,
            "SessionMonitor",
            f"Could not start logind monitoring:\n{self._monitor.last_error}",
        )
        return False

    def open_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def exit_app(self) -> None:
        self._tray.hide()
        self._app.quit()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.Trigger,
            QSystemTrayIcon.DoubleClick,
        }:
            self.open_window()

    def _tray_icon(self) -> QIcon:
        icon = QIcon.fromTheme(DEFAULT_ICON)
        if icon.isNull():
            icon = self._window.style().standardIcon(QStyle.SP_ComputerIcon)
        return icon

    def _on_sessions_updated(
        self,
        kind: UpdateKind,
        views: dict[str, SessionView],
    ) -> None:
        if kind == UpdateKind.SNAPSHOT:
            self._sessions = dict(views)
        elif kind == UpdateKind.REMOVED:
            for session_id in views:
                self._sessions.pop(session_id, None)
        else:
            self._sessions.update(views)

        self._window.set_sessions(self._sessions)

        if kind != UpdateKind.SNAPSHOT and views:
            self._notify_session_change(kind, views)

    def _notify_session_change(
        self,
        kind: UpdateKind,
        views: dict[str, SessionView],
    ) -> None:
        count = len(views)
        verb = {
            UpdateKind.ADDED: "added",
            UpdateKind.REMOVED: "removed",
            UpdateKind.CHANGED: "changed",
        }.get(kind, "updated")
        summary = f"Session {verb}" if count == 1 else f"{count} sessions {verb}"
        lines = [
            f"{view.session_id}: {view.application} {view.state} from {view.source}"
            for view in views.values()
        ]
        body = "\n".join(lines)

        if self._notifier.send(summary, body):
            return

        if self._tray.isVisible():
            self._tray.showMessage(
                summary,
                body,
                QSystemTrayIcon.Information,
                5000,
            )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    tray_app = SessionTrayApp(app)
    tray_app.show()
    tray_app.start_monitor()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
