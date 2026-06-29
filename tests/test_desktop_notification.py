#!/usr/bin/env python3
"""Send a desktop notification through the freedesktop notification service.

Run directly:

    python3 tests/test_desktop_notification.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import dbus


APP_NAME = "SessionMonitor Probe"
NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
DEFAULT_ICON = Path(__file__).resolve().parent / "assets" / "session-monitor-notification.svg"


def notification_interface() -> dbus.Interface:
    bus = dbus.SessionBus()
    service = bus.get_object(NOTIFICATIONS_SERVICE, NOTIFICATIONS_PATH)
    return dbus.Interface(service, dbus_interface=NOTIFICATIONS_INTERFACE)


def send_notification(
    interface: dbus.Interface,
    summary: str,
    body: str,
    expire_timeout_ms: int,
    icon: str,
) -> int:
    hints = dbus.Dictionary({}, signature="sv")
    if icon:
        hints["image-path"] = dbus.String(icon, variant_level=1)

    notification_id = interface.Notify(
        APP_NAME,
        dbus.UInt32(0),
        icon,
        summary,
        body,
        dbus.Array([], signature="s"),
        hints,
        dbus.Int32(expire_timeout_ms),
    )
    return int(notification_id)


def describe_server(interface: dbus.Interface) -> None:
    try:
        name, vendor, version, spec_version = interface.GetServerInformation()
        print(f"server: {name} ({vendor}) version={version} spec={spec_version}")
    except dbus.DBusException as exc:
        print(f"server: unavailable ({exc})")

    try:
        capabilities = interface.GetCapabilities()
        print(f"capabilities: {', '.join(capabilities) if capabilities else '-'}")
    except dbus.DBusException as exc:
        print(f"capabilities: unavailable ({exc})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a freedesktop desktop notification over session D-Bus.",
    )
    parser.add_argument(
        "--summary",
        default="SessionMonitor notification probe",
        help="notification title",
    )
    parser.add_argument(
        "--body",
        default="If you can see this, org.freedesktop.Notifications works.",
        help="notification body text",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5000,
        help="expiration timeout in milliseconds; -1 uses the server default",
    )
    parser.add_argument(
        "--icon",
        default=str(DEFAULT_ICON),
        help=(
            "icon file path or themed icon name; defaults to the repo-local "
            "test icon"
        ),
    )
    parser.add_argument(
        "--no-icon",
        action="store_true",
        help="send the notification without an icon",
    )
    return parser.parse_args()


def resolve_icon(icon: str, no_icon: bool) -> str:
    if no_icon:
        return ""

    path = Path(icon).expanduser()
    if path.exists():
        return str(path.resolve())

    return icon


def main() -> int:
    args = parse_args()
    icon = resolve_icon(args.icon, args.no_icon)

    try:
        interface = notification_interface()
        describe_server(interface)
        notification_id = send_notification(
            interface,
            args.summary,
            args.body,
            args.timeout,
            icon,
        )
    except dbus.DBusException as exc:
        print(f"notification failed: {exc}", file=sys.stderr)
        return 1

    print(f"notification sent: id={notification_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
