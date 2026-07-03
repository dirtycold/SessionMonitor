"""Desktop notification support."""

from __future__ import annotations


APP_NAME = "SessionMonitor"
NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
DEFAULT_ICON = "utilities-terminal"


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
