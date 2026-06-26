# SSH Session Notifier — Initial Project Plan

## Goal

Build a simple Linux-first desktop application that can:

1. List active SSH sessions.
2. Show a desktop notification when a new SSH session is established.
3. Run primarily as a normal user application.
4. Avoid requiring root privileges for the main GUI.
5. Keep the architecture extensible for future backends, platforms, and privileged helper operations.

The project should start from scratch rather than borrowing code from existing tools. Existing projects such as `whowatch` are useful as conceptual references, but not as code foundations.

---

## Product Philosophy

The application should be:

* Simple and understandable.
* User-session oriented, not a headless server daemon.
* Rootless by default.
* Desktop-notification native.
* Linux-first, with clean abstraction points for future platforms.
* Designed so a daemon or privileged helper can be added later, but not required initially.

Avoid over-engineering the first version.

---

## Initial Architecture

Start with a standalone PyQt application.

```text
ssh-session-notifier/
  app/
    main.py
    tray.py
    main_window.py
    session_table.py

  backend/
    base.py
    linux_logind_dbus.py
    linux_loginctl.py

  model/
    ssh_session.py

  notifier/
    base.py
    qt_tray.py

  utils/
    diff.py
    subprocess.py

  tests/
    test_session_diff.py
    test_loginctl_parser.py
```

---

## Recommended MVP

### v0 Features

The first working version should:

* Start as a PyQt tray application.
* Show a tray icon.
* Provide a window/table listing active SSH sessions.
* Detect newly created SSH sessions.
* Show desktop notifications for new sessions.
* Run without root privileges.
* Use systemd-logind as the primary Linux backend.
* Use polling only as a fallback.

---

## Session Detection Strategy

### Preferred Event Source: systemd-logind over D-Bus

Use systemd-logind’s D-Bus interface to subscribe to session lifecycle events.

Relevant events:

```text
org.freedesktop.login1.Manager.SessionNew
org.freedesktop.login1.Manager.SessionRemoved
```

On `SessionNew`:

1. Inspect the new session.
2. Check whether it is remote.
3. Check whether the service is `sshd`.
4. If yes, add it to current state and show a notification.

Conceptual filter:

```text
Remote=yes
Service=sshd
```

Possible useful properties:

```text
Id
Name
User
Remote
RemoteHost
Service
TTY
Leader
State
Type
Class
Timestamp
```

### Initial Simpler Backend: `loginctl`

For quick implementation, shell out to:

```bash
loginctl list-sessions --no-legend
```

Then for each session:

```bash
loginctl show-session <session-id> \
  -p Id \
  -p Name \
  -p User \
  -p Remote \
  -p RemoteHost \
  -p Service \
  -p TTY \
  -p Leader \
  -p State \
  -p Type \
  -p Class
```

This is not as elegant as direct D-Bus property reading, but it is fast to prototype and easy to debug.

Later, replace shelling out with direct D-Bus property reads.

---

## Event-Driven vs Polling

Do not try to “epoll SSH logins” directly.

`epoll` works on file descriptors; it does not understand SSH login events. The more appropriate Linux-native event sources are:

1. systemd-logind D-Bus signals.
2. `sd_login_monitor` from libsystemd.
3. PAM hooks, later, if perfect capture is needed.
4. journald/auth.log watching only as a fallback.

### Recommended v0 Behavior

```text
Application starts
  ↓
Take initial snapshot of current SSH sessions
  ↓
Subscribe to logind session events
  ↓
When a session event arrives:
    refresh session snapshot
    compare old/new state
    notify about new SSH sessions
```

This is event-driven enough without needing a continuous polling loop.

---

## Desktop Notification Strategy

Start with PyQt’s built-in tray notification:

```python
QSystemTrayIcon.showMessage(...)
```

Possible notification text:

```text
New SSH session

User: eric
Host: 192.168.1.50
TTY: pts/3
Session: 42
```

Later notification backends can include:

```text
notifier/qt_tray.py
notifier/libnotify.py
notifier/notify_send.py
notifier/webhook.py
notifier/telegram.py
```

But the first version should only need Qt tray notifications.

---

## Root and Privilege Model

The main GUI should not run as root.

The normal app should be able to:

```text
✓ list active login sessions
✓ detect logind session events
✓ filter SSH sessions
✓ show desktop notifications
✓ run as the current graphical user
```

The normal app may not be able to:

```text
✗ read protected auth logs
✗ install PAM hooks
✗ inspect every process detail on hardened systems
✗ monitor when the user is not logged in
✗ guarantee detection of extremely short sessions if relying only on state refresh
```

This is acceptable for the MVP.

---

## Avoid Starting with a Daemon

Do not start with:

```text
system daemon + GUI client
```

That adds unnecessary complexity:

* systemd service installation
* root permissions
* IPC
* user-session discovery
* DBus session forwarding
* notification routing
* packaging complexity

Instead, start with:

```text
normal user PyQt app
  ├── logind event backend
  ├── active session table
  ├── desktop notification
  └── optional privileged helper later
```

---

## Future Privileged Operations

If privileged operations are needed later, use a small on-demand helper.

Preferred model:

```text
GUI remains unprivileged
  ↓
User requests privileged action
  ↓
GUI invokes helper via pkexec / polkit
  ↓
Helper performs one narrow operation
  ↓
Helper exits
```

Possible future privileged helper actions:

```text
- install/remove PAM hook
- read protected auth logs once
- install system-wide integration
- terminate another user’s session
```

Avoid:

```text
- running the GUI as root
- keeping a long-running root helper
- making desktop notifications depend on root
```

---

## Possible Later Backends

Keep backend abstraction clean from the beginning.

```text
backend/base.py
backend/linux_logind_dbus.py
backend/linux_loginctl.py
backend/linux_sd_login.py
backend/linux_journald.py
backend/linux_pam_socket.py
backend/macos_who.py
backend/windows_openssh.py
```

### Backend Priority

```text
v0:
  loginctl snapshot + logind D-Bus events

v1:
  direct D-Bus property reads

v2:
  sd_login_monitor backend integrated with Qt event loop

v3:
  optional PAM/polkit helper for high-reliability event capture
```

---

## Data Model

Define a normalized SSH session object.

```python
@dataclass(frozen=True)
class SshSession:
    session_id: str
    user_name: str | None
    uid: int | None
    remote_host: str | None
    tty: str | None
    service: str | None
    leader_pid: int | None
    state: str | None
    session_type: str | None
```

Session identity can initially be:

```text
session_id
```

For diffing:

```python
old_sessions: dict[str, SshSession]
new_sessions: dict[str, SshSession]

added = new_sessions.keys() - old_sessions.keys()
removed = old_sessions.keys() - new_sessions.keys()
possibly_changed = old_sessions.keys() & new_sessions.keys()
```

---

## UI Plan

### Tray Menu

```text
Open
Refresh
Notifications: Enabled/Disabled
Start on Login
Quit
```

### Main Window

Columns:

```text
Session ID
User
Remote Host
TTY
State
Type
Leader PID
```

Optional details panel later:

```text
Login time
Process tree
Client IP
Authentication method
Last activity
```

---

## CLI Debug Hooks

Even though the product is GUI-first, add simple CLI modes for testing.

```bash
ssh-session-notifier --once-json
ssh-session-notifier --debug
ssh-session-notifier --test-notification
```

These make backend development much easier before the UI is polished.

---

## Development Steps

### Step 1: Create Session Model

* Define `SshSession`.
* Define backend interface.
* Define session diff logic.
* Add unit tests for diff logic.

### Step 2: Implement `loginctl` Snapshot Backend

* Run `loginctl list-sessions`.
* Parse session IDs.
* Run `loginctl show-session`.
* Parse key/value properties.
* Filter `Remote=yes` and `Service=sshd`.
* Return `list[SshSession]`.

### Step 3: Add CLI Debug Mode

Support:

```bash
python -m ssh_session_notifier --once-json
```

Expected output:

```json
[
  {
    "session_id": "42",
    "user_name": "eric",
    "remote_host": "192.168.1.50",
    "tty": "pts/3",
    "service": "sshd",
    "state": "active"
  }
]
```

### Step 4: Build PyQt Tray App

* Create `QApplication`.
* Create `QSystemTrayIcon`.
* Add menu.
* Add notification test action.
* Add main window with table.

### Step 5: Add Periodic Refresh Fallback

Initially use a timer, for example every 5 seconds.

This provides a working baseline before D-Bus event integration.

### Step 6: Add logind D-Bus Event Backend

* Subscribe to `SessionNew`.
* Subscribe to `SessionRemoved`.
* On event, refresh snapshot.
* Diff sessions.
* Notify only for newly added SSH sessions.

### Step 7: Polish Notifications

Add settings:

```text
Enable notifications
Ignore localhost
Ignore specific users
Ignore specific remote hosts
Notification timeout
```

Do not overbuild this in v0.

---

## Non-Goals for MVP

Do not implement these initially:

```text
- daemon mode
- PAM hook
- eBPF monitoring
- auditd integration
- Telegram/Slack notifications
- session recording
- command monitoring
- remote web dashboard
- multi-user notification routing
- root-required features
```

---

## Design Decision Summary

Use:

```text
PyQt tray application
systemd-logind events
loginctl or D-Bus session inspection
rootless desktop notifications
optional polling fallback
```

Do not use initially:

```text
system daemon
PAM hook
auth.log parser
journald parser
eBPF
root GUI
```

The core architectural principle is:

```text
The app is a user-session desktop notifier first.
It may grow privileged helpers later, but it should not become a daemon-first server monitor.
```
