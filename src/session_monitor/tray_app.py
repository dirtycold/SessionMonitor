"""Tray application skeleton for SessionMonitor."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from qtpy.QtCore import QEvent
from qtpy.QtCore import QPoint
from qtpy.QtCore import Qt
from qtpy.QtCore import Signal
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


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    state: str
    application: str


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

    def __init__(self) -> None:
        super().__init__()
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
        self._refresh_button.clicked.connect(self._load_placeholder_rows)

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

        self._load_placeholder_rows()

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

    def _load_placeholder_rows(self) -> None:
        rows = [
            SessionRow("6", "active", "sddm"),
            SessionRow("41", "active", "codex"),
            SessionRow("42", "active", "vscode"),
            SessionRow("43", "closing", "mosh"),
        ]

        self._tree.clear()
        self._action_widgets.clear()

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
        self._window = SessionWindow()
        self._tray = QSystemTrayIcon(self._tray_icon(), self._window)
        self._tray.setToolTip("SessionMonitor")
        self._tray.activated.connect(self._on_tray_activated)

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
        else:
            QMessageBox.warning(
                self._window,
                "SessionMonitor",
                "System tray is not available. Opening the window instead.",
            )
            self.open_window()

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
        icon = QIcon.fromTheme("utilities-terminal")
        if icon.isNull():
            icon = self._window.style().standardIcon(QStyle.SP_ComputerIcon)
        return icon


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SessionMonitor")
    app.setQuitOnLastWindowClosed(False)

    tray_app = SessionTrayApp(app)
    tray_app.show()
    return app.exec()
