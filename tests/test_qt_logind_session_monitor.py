#!/usr/bin/env python3
"""QtDBus-based login session monitor probe.

Run directly:

    python3 tests/test_qt_logind_session_monitor.py
"""

from __future__ import annotations

import signal
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qtpy.QtCore import QCoreApplication
from qtpy.QtCore import QObject
from qtpy.QtCore import QTimer
from qtpy.QtCore import Signal
from qtpy.QtCore import Slot
from qtpy.QtDBus import QDBusConnection
from qtpy.QtDBus import QDBusObjectPath


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


class UpdateKind(Enum):
    SNAPSHOT = "SNAPSHOT"
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    CHANGED = "CHANGED"


def session_sort_key(session_id: str) -> tuple[int, int | str]:
    return (0, int(session_id)) if session_id.isdigit() else (1, session_id)


def print_row(
    session_id: str,
    user: str,
    life: str,
    state: str,
    kind: str,
    source: str,
    application: str,
    details: str,
) -> None:
    print(
        f"{session_id:<8} {user:<16} {life:<8} {state:<8} {kind:<8} "
        f"{source:<40} {application:<10} {details}"
    )


def print_views(prefix: str, views: dict[str, SessionView]) -> None:
    print(f"{prefix}:")
    print_row("SESSION", "USER", "LIFE", "STATE", "KIND", "SOURCE", "APP", "DETAILS")
    for session_id in sorted(views, key=session_sort_key):
        view = views[session_id]
        print_row(
            view.session_id,
            view.user,
            view.life,
            view.state,
            view.kind,
            view.source,
            view.application,
            view.details,
        )


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
        print(f"event: session={session_id} path={path.path()}")
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
            if tty and entry.tty == tty:
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
            return processes[:32]

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


def main() -> int:
    app = QCoreApplication(sys.argv)
    monitor = QtLogindSessionMonitor()

    # Let Python process Ctrl-C while Qt owns the event loop.
    interrupt_timer = QTimer()
    interrupt_timer.timeout.connect(lambda: None)
    interrupt_timer.start(250)

    def stop(_signum: int, _frame: object) -> None:
        print()
        print("Stopped.")
        app.quit()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    monitor.updated.connect(lambda kind, views: print_views(kind.value, views))

    if not monitor.start():
        print(monitor.last_error, file=sys.stderr)
        return 1

    print()
    print("Monitoring logind events with QtDBus.")
    print("Press Ctrl-C to stop.")
    print()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
