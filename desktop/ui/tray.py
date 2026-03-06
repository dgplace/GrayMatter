"""
@file tray.py
@brief System tray icon and context menu for background watch mode.

SystemTrayManager provides a persistent tray icon when watchers are active.
It lets the user show/hide the main window, inspect watched repos, and quit
the application without opening the main window.
"""

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from desktop.core.watcher import MultiRepoWatcher


def _make_app_icon() -> QIcon:
    """@brief Create a simple programmatic app icon (blue circle with 'CB').

    Used as both the tray icon and the window icon. No external image files
    are required, so the app works out of the box on all platforms.

    @return QIcon built from an in-memory QPixmap.
    """
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))  # transparent background
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QColor("#1565C0"))
    painter.setPen(QColor("#0D47A1"))
    painter.drawEllipse(2, 2, 60, 60)

    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPixelSize(22)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), 0x0084, "CB")  # Qt.AlignCenter

    painter.end()
    return QIcon(pixmap)


class SystemTrayManager(QObject):
    """@brief Manage the system tray icon, tooltip, and context menu.

    Listens to MultiRepoWatcher signals to update the menu dynamically
    as repos start and stop being watched. Shows balloon notifications
    when files are re-indexed in watch mode.
    """

    def __init__(self, main_window, watcher: MultiRepoWatcher) -> None:
        """@brief Construct and initialise the system tray.

        @param main_window The MainWindow instance to show/hide on request.
        @param watcher MultiRepoWatcher whose signals drive menu updates.
        """
        super().__init__()
        self._main_window = main_window
        self._watcher = watcher
        self._icon = _make_app_icon()

        self._tray = QSystemTrayIcon(self._icon)
        self._tray.setToolTip("CodeBrain Desktop")
        self._tray.activated.connect(self._on_activated)

        self._menu = QMenu()
        self._action_show = self._menu.addAction("Show Window")
        self._action_show.triggered.connect(self._show_window)
        self._menu.addSeparator()

        # Placeholder for the "Watched repos" submenu — rebuilt dynamically.
        self._watched_section_label = self._menu.addAction("Watched Repos")
        self._watched_section_label.setEnabled(False)
        self._watched_actions: dict[str, object] = {}

        self._menu.addSeparator()
        self._action_stop_all = self._menu.addAction("Stop All Watchers")
        self._action_stop_all.triggered.connect(watcher.stop_all)
        self._action_stop_all.setEnabled(False)
        self._menu.addSeparator()
        self._action_quit = self._menu.addAction("Quit CodeBrain")
        self._action_quit.triggered.connect(QApplication.quit)

        self._tray.setContextMenu(self._menu)

        watcher.watch_started.connect(self._on_watch_started)
        watcher.watch_stopped.connect(self._on_watch_stopped)

    def show(self) -> None:
        """@brief Display the tray icon in the system notification area."""
        self._tray.show()

    def hide(self) -> None:
        """@brief Remove the tray icon from the notification area."""
        self._tray.hide()

    def notify(self, title: str, message: str) -> None:
        """@brief Show a brief balloon/notification from the tray icon.

        @param title Notification title.
        @param message Notification body text.
        """
        self._tray.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def app_icon(self) -> QIcon:
        """@brief Return the shared application icon.

        @return QIcon used for the tray and window title bar.
        """
        return self._icon

    # ------------------------------------------------------------------
    # Private slots
    # ------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """@brief Toggle main window visibility on double-click.

        @param reason The activation reason from Qt.
        """
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self) -> None:
        """@brief Bring the main window to the foreground."""
        self._main_window.show()
        self._main_window.raise_()
        self._main_window.activateWindow()

    def _on_watch_started(self, repo_name: str) -> None:
        """@brief Add a menu entry for a newly watched repository.

        @param repo_name Name of the repo now being watched.
        """
        action = QAction(f"  \u25CF {repo_name}", self._menu)
        action.setEnabled(False)
        self._menu.insertAction(self._action_stop_all, action)
        self._watched_actions[repo_name] = action
        self._action_stop_all.setEnabled(True)
        self._tray.setToolTip(
            f"CodeBrain Desktop — watching {len(self._watched_actions)} repo(s)"
        )

    def _on_watch_stopped(self, repo_name: str) -> None:
        """@brief Remove the menu entry for a repo that stopped being watched.

        @param repo_name Name of the repo that stopped.
        """
        action = self._watched_actions.pop(repo_name, None)
        if action:
            self._menu.removeAction(action)
        has_watchers = bool(self._watched_actions)
        self._action_stop_all.setEnabled(has_watchers)
        if has_watchers:
            self._tray.setToolTip(
                f"CodeBrain Desktop — watching {len(self._watched_actions)} repo(s)"
            )
        else:
            self._tray.setToolTip("CodeBrain Desktop")
