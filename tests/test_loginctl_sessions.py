#!/usr/bin/env python3
"""Probe current login sessions via loginctl.

Run directly:

    python3 tests/test_loginctl_sessions.py
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


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


def run_loginctl(args: list[str]) -> str:
    result = subprocess.run(
        ["loginctl", *args, "--no-pager"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"loginctl failed: {message}")

    return result.stdout


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def run_process(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_command(args)


def show_session(session_id: str) -> dict[str, str]:
    output = run_loginctl(["show-session", session_id])
    properties: dict[str, str] = {}

    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value

    return properties


def parse_who_line(line: str) -> UtmpEntry | None:
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


def list_utmp_entries() -> list[UtmpEntry]:
    result = run_command(["who"])
    if result.returncode != 0:
        return []

    entries: list[UtmpEntry] = []
    for line in result.stdout.splitlines():
        entry = parse_who_line(line)
        if entry is not None:
            entries.append(entry)

    return entries


def matching_utmp_entry(
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


def session_sort_key(session_id: str) -> tuple[int, int | str]:
    return (0, int(session_id)) if session_id.isdigit() else (1, session_id)


def read_process(pid: str) -> ProcessInfo | None:
    result = run_process(["ps", "-p", pid, "-o", "comm=", "-o", "args="])
    if result.returncode != 0:
        return None

    line = result.stdout.strip()
    if not line:
        return None

    name, _, args = line.partition(" ")
    return ProcessInfo(pid=pid, name=name, args=args.strip())


def child_pids(pid: str) -> list[str]:
    result = run_process(["ps", "--no-headers", "-o", "pid=", "--ppid", pid])
    if result.returncode != 0:
        return []

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def cgroup_pids(properties: dict[str, str]) -> list[str]:
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


def session_processes(properties: dict[str, str]) -> list[ProcessInfo]:
    leader = properties.get("Leader")
    pids = cgroup_pids(properties)

    if pids:
        processes = [process for pid in pids if (process := read_process(pid))]
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

        process = read_process(pid)
        if process is not None:
            found.append(process)

        queue.extend(child_pids(pid))

    return found


def list_sessions() -> list[LoginSession]:
    output = run_loginctl(["list-sessions", "--no-legend"])
    utmp_entries = list_utmp_entries()
    sessions: list[LoginSession] = []
    for line in output.splitlines():
        columns = line.split()
        if not columns:
            continue

        session_id = columns[0]
        properties = show_session(session_id)
        sessions.append(
            LoginSession(
                session_id=session_id,
                uid=properties.get("User", ""),
                user=properties.get("Name", ""),
                seat=properties.get("Seat", ""),
                tty=properties.get("TTY", ""),
                properties=properties,
                processes=session_processes(properties),
                utmp=matching_utmp_entry(properties, utmp_entries),
            )
        )

    return sessions


def main() -> int:
    try:
        sessions = list_sessions()
    except FileNotFoundError:
        print("loginctl is not installed or is not on PATH.", file=sys.stderr)
        return 127
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not sessions:
        print("No login sessions found.")
        return 0

    alive_by_source: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for session in sessions:
        if session.life != "alive":
            continue
        key = (session.user, session.kind, session.source, session.application)
        alive_by_source[key].append(session.session_id)

    for session_ids in alive_by_source.values():
        session_ids.sort(key=session_sort_key)

    print_row("SESSION", "USER", "LIFE", "STATE", "KIND", "SOURCE", "APP", "DETAILS")
    for session in sessions:
        details = session.details
        superseded_by = alive_by_source.get(
            (session.user, session.kind, session.source, session.application)
        )
        if session.life == "dead" and superseded_by:
            related_ids = [
                session_id
                for session_id in superseded_by
                if session_id != session.session_id
            ]
            if related_ids:
                details = f"{details}, related={','.join(related_ids)}"

        print_row(
            session.session_id,
            session.user,
            session.life,
            session.state,
            session.kind,
            session.source,
            session.application,
            details,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
