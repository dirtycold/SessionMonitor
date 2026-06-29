#!/usr/bin/env python3
"""Monitor login session changes through logind D-Bus events.

Run directly:

    python3 tests/test_loginctl_session_monitor.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

from test_loginctl_sessions import LoginSession
from test_loginctl_sessions import list_sessions
from test_loginctl_sessions import print_row
from test_loginctl_sessions import session_sort_key


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


def related_alive_sessions(sessions: list[LoginSession]) -> dict[tuple[str, str, str, str], list[str]]:
    related: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)

    for session in sessions:
        if session.life != "alive":
            continue
        key = (session.user, session.kind, session.source, session.application)
        related[key].append(session.session_id)

    for session_ids in related.values():
        session_ids.sort(key=session_sort_key)

    return related


def view_sessions() -> dict[str, SessionView]:
    sessions = list_sessions()
    related = related_alive_sessions(sessions)
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


def print_view(prefix: str, view: SessionView) -> None:
    print(f"{prefix}:")
    print_row("SESSION", "USER", "LIFE", "STATE", "KIND", "SOURCE", "APP", "DETAILS")
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


def print_snapshot(views: dict[str, SessionView]) -> None:
    print("Initial sessions:")
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


def print_changes(
    previous: dict[str, SessionView],
    current: dict[str, SessionView],
) -> None:
    previous_ids = set(previous)
    current_ids = set(current)

    for session_id in sorted(current_ids - previous_ids, key=session_sort_key):
        print_view("ADDED", current[session_id])

    for session_id in sorted(previous_ids - current_ids, key=session_sort_key):
        print_view("REMOVED", previous[session_id])

    for session_id in sorted(previous_ids & current_ids, key=session_sort_key):
        if previous[session_id] != current[session_id]:
            print_view("CHANGED", current[session_id])


def monitor_command() -> list[str]:
    if shutil.which("gdbus"):
        return ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1"]
    if shutil.which("busctl"):
        return ["busctl", "monitor", "org.freedesktop.login1"]
    raise RuntimeError("Neither gdbus nor busctl is available.")


def should_refresh(line: str) -> bool:
    return any(
        token in line
        for token in (
            "SessionNew",
            "SessionRemoved",
            "PropertiesChanged",
            "org.freedesktop.login1",
        )
    )


def main() -> int:
    try:
        previous = view_sessions()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print_snapshot(previous)
    print()

    try:
        command = monitor_command()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 127

    print(f"Monitoring logind events with: {' '.join(command)}")
    print("Press Ctrl-C to stop.")
    print()

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        print(f"failed to start monitor: {exc}", file=sys.stderr)
        return 1

    assert process.stdout is not None
    last_refresh = 0.0

    try:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                print(f"event: {line}")
            if not should_refresh(line):
                continue

            now = time.monotonic()
            if now - last_refresh < 0.2:
                continue
            last_refresh = now

            try:
                current = view_sessions()
            except RuntimeError as exc:
                print(f"refresh failed: {exc}", file=sys.stderr)
                continue

            print_changes(previous, current)
            previous = current
    except KeyboardInterrupt:
        print()
        print("Stopped.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
