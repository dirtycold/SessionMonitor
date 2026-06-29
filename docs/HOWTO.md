# HOWTO

This document collects practical notes for experimenting with the session
notifier prototype.

## Project Draft

The initial project plan lives at:

```text
docs/DRAFT.md
```

## Probes

### Snapshot Probe

The current exploratory probe is:

```bash
python3 tests/test_loginctl_sessions.py
```

It prints one row per `loginctl` session:

```text
SESSION  USER  LIFE  STATE  KIND  SOURCE  APP  DETAILS
```

The probe is meant to answer a small set of questions:

- Is the session local or remote?
- Is it alive, dead, closing, or unknown?
- Where did the remote session come from?
- Which application most likely created it?
- Are there related live sessions from the same user/source/application?

### Event Monitor Probe

The event-driven shell probe is:

```bash
python3 tests/test_loginctl_session_monitor.py
```

It takes an initial snapshot, starts a logind D-Bus monitor with `gdbus` or
`busctl`, and refreshes the snapshot when logind emits session-related events.

It prints grouped changes:

```text
ADDED
REMOVED
CHANGED
```

This probe proved the useful runtime shape:

```text
logind event arrives
  -> refresh full session snapshot
  -> diff previous/current snapshots
  -> emit or print grouped changes
```

The event payload is not treated as the source of truth. It is only the trigger
to refresh the snapshot. This matters because multiple session changes can be
observed after a single event.

### QtDBus Monitor Probe

The Qt-oriented probe is:

```bash
python3 tests/test_qt_logind_session_monitor.py
```

It uses `qtpy` and QtDBus:

```python
from qtpy.QtDBus import QDBusConnection
```

The probe defines a self-contained `QtLogindSessionMonitor(QObject)` class. It
owns both startup probing and event monitoring:

- connects to logind `SessionNew`
- connects to logind `SessionRemoved`
- debounces events with a `QTimer`
- refreshes the full session snapshot
- computes grouped changes
- emits one unified signal

The public data signal is:

```python
updated = Signal(object, object)
```

The first argument is an `UpdateKind` enum:

```text
SNAPSHOT
ADDED
REMOVED
CHANGED
```

The second argument is always:

```python
dict[str, SessionView]
```

That dictionary can contain zero, one, or many entries. The initial startup data
is emitted as `UpdateKind.SNAPSHOT`; later updates are grouped by change type.

The terminal probe also uses a no-op `QTimer` to keep Python signal handling
responsive while Qt owns the event loop. Without that timer, Ctrl-C may not
stop the process promptly in a terminal-only Qt application.

### Desktop Notification Probe

The desktop notification probe is:

```bash
python3 tests/test_desktop_notification.py
```

It sends a notification through the freedesktop notification service:

```text
org.freedesktop.Notifications
```

This is the common Linux desktop notification API used by Plasma, GNOME, Xfce,
and other major desktop environments. The probe talks to the user's session
D-Bus, so it should be run from a real graphical user session.

Basic test:

```bash
python3 tests/test_desktop_notification.py \
  --summary "SSH session" \
  --body "hello"
```

The probe prints the notification server and capability list before sending the
notification. On KDE Plasma, for example, it may report `Plasma (KDE)` and
capabilities such as `body`, `actions`, and `persistence`.

Timeout behavior:

```bash
python3 tests/test_desktop_notification.py --timeout 5000
python3 tests/test_desktop_notification.py --timeout 0
python3 tests/test_desktop_notification.py --timeout -1
```

Common meanings:

```text
5000  expire after about five seconds
0     persistent notification
-1    notification server default
```

For future SSH login alerts, `--timeout 0` is worth considering because a new
remote session can be security-relevant and should not vanish too quickly.

Icon behavior:

```bash
python3 tests/test_desktop_notification.py --no-icon
python3 tests/test_desktop_notification.py --icon dialog-information
python3 tests/test_desktop_notification.py --icon utilities-terminal
python3 tests/test_desktop_notification.py --icon network-server
```

The probe defaults to a repo-local SVG test icon:

```text
tests/assets/session-monitor-notification.svg
```

No global icon registration is required. You can pass either a file path or a
desktop theme icon name with `--icon`. The safest themed icon smoke test is
`dialog-information`; `utilities-terminal` and `network-server` are closer to
the SSH-session use case when the desktop theme provides them.

## Detection Mechanism

The probe starts from systemd-logind:

```bash
loginctl list-sessions --no-legend --no-pager
loginctl show-session <session-id> --no-pager
```

`show-session` is treated as the source of truth for the core session fields:

```text
Id
User
Name
Remote
RemoteHost
Service
TTY
Leader
State
Type
Class
Display
Seat
ControlGroup
```

The script then enriches those fields with process information from the
session leader, child processes, and the session cgroup when available.

### Life State

`LIFE` is a simplified status derived from logind state plus visible processes:

```text
alive    State is active, online, or opening
alive    a matching utmp/who entry proves the session is still usable
closing  State is closing, but session processes are still visible
dead     State is closing, and no session processes are visible
unknown  anything else
```

`State=closing` does not always mean a remote tool is gone. It can mean the
original SSH/PAM login has closed while child processes or a tool-specific
transport is still around.

### Application Classification

The probe classifies `APP` from the best signal it can find:

```text
mosh      mosh-server process, or a matching who/utmp entry containing "mosh"
sftp      sftp-server or internal-sftp process
codex     codex app-server or .codex process path
vscode    .vscode-server, code-server, or remotessh process path
ssh       Service=sshd fallback
sddm      Service=sddm for the local desktop session
```

This is intentionally heuristic. `loginctl` itself only knows that many remote
sessions were created through `sshd`; identifying the higher-level client
requires looking at the process tree.

### Mosh

Mosh needs special handling because SSH is only used to bootstrap the
connection. After that, mosh keeps its own UDP-based connection alive.

That means logind can show the original SSH session as `State=closing` even
when the mosh terminal is still usable. The probe checks `who`/utmp as a
secondary signal so a mosh session can still be reported as alive.

### Related Sessions

Some clients open multiple SSH sessions from the same source. Examples include:

- Codex remote access
- VS Code Remote SSH
- SFTP clients
- OpenSSH multiplexing or reconnect behavior

When a dead session has live sessions with the same user, source, and
application classification, the probe annotates it with:

```text
related=<session-id>[,<session-id>...]
```

This does not mean the live sessions were spawned by the dead one. It only means
they look related enough to avoid treating the dead session as a separate
current login.

## Useful Manual Checks

Inspect one session:

```bash
loginctl session-status <session-id>
loginctl show-session <session-id> --no-pager
```

Inspect a session leader:

```bash
ps -fp <leader-pid>
pstree -aps <leader-pid>
```

Inspect terminal login records:

```bash
who
who -u
```

Clean up a stale session when you are sure it is no longer useful:

```bash
loginctl terminate-session <session-id>
```

If it remains stuck and you are certain it is stale:

```bash
loginctl kill-session <session-id>
```

## Next Step

The QtDBus probe should eventually move out of `tests/` into an application
backend module. The PyQt UI can subscribe to `updated(UpdateKind, views)` and
decide which events should create tray notifications.
