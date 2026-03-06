"""
@file main_window.py
@brief Top-level application window with sidebar navigation and stacked views.

MainWindow hosts a QListWidget sidebar on the left and a QStackedWidget on
the right. Navigating the sidebar switches the visible view. When the user
closes the window while watchers are active, the app minimises to the system
tray instead of quitting.
"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from desktop.core.engine import IngestionEngine
from desktop.core.state import AppState
from desktop.core.watcher import MultiRepoWatcher
from desktop.ui.history_view import HistoryView
from desktop.ui.ingestion_view import IngestionView
from desktop.ui.repo_panel import RepoPanel
from desktop.ui.settings_dialog import SettingsDialog
from desktop.ui.stats_view import StatsView

# Sidebar navigation entries: (display text, page index)
_NAV_ITEMS = [
    ("Repositories", 0),
    ("Ingestion", 1),
    ("Statistics", 2),
    ("History", 3),
]


class MainWindow(QMainWindow):
    """@brief Primary application window.

    Creates all view widgets, wires them together, and manages the window
    lifecycle (minimise to tray when watching, clean shutdown on quit).
    """

    def __init__(
        self,
        state: AppState,
        engine: IngestionEngine,
        watcher: MultiRepoWatcher,
        app_icon: QIcon,
    ) -> None:
        """@brief Construct and lay out the main window.

        @param state AppState instance shared with all views.
        @param engine IngestionEngine instance shared with all views.
        @param watcher MultiRepoWatcher instance shared with all views.
        @param app_icon Application icon for the window title bar.
        """
        super().__init__()
        self._state = state
        self._engine = engine
        self._watcher = watcher

        self.setWindowTitle("CodeBrain Desktop")
        self.setWindowIcon(app_icon)
        self.setMinimumSize(960, 640)

        # Build views
        self._repo_panel = RepoPanel(state, engine, watcher)
        self._ingestion_view = IngestionView(engine, watcher)
        self._stats_view = StatsView(state, engine)
        self._history_view = HistoryView(state, engine)

        # Stack
        self._stack = QStackedWidget()
        self._stack.addWidget(self._repo_panel)     # page 0
        self._stack.addWidget(self._ingestion_view) # page 1
        self._stack.addWidget(self._stats_view)     # page 2
        self._stack.addWidget(self._history_view)   # page 3

        # Sidebar
        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(148)
        self._sidebar.setSpacing(2)
        for label, _ in _NAV_ITEMS:
            item = QListWidgetItem(label)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._sidebar.addItem(item)
        self._sidebar.setCurrentRow(0)
        self._sidebar.currentRowChanged.connect(self._on_nav_changed)

        # Toolbar action for Settings
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        settings_action = toolbar.addAction("Settings")
        settings_action.triggered.connect(self._open_settings)

        # Splitter (sidebar | stack)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([148, 812])

        self.setCentralWidget(splitter)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._sb_watch_label = QLabel("No active watchers")
        self._status_bar.addPermanentWidget(self._sb_watch_label)

        # Wire up signals
        self._repo_panel.ingestion_started.connect(self._on_ingestion_started)
        watcher.watch_started.connect(self._update_status_bar)
        watcher.watch_stopped.connect(self._update_status_bar)
        engine.repo_started.connect(self._update_status_bar)
        engine.repo_completed.connect(self._on_engine_repo_completed)
        engine.repo_error.connect(self._update_status_bar)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @Slot(int)
    def _on_nav_changed(self, row: int) -> None:
        """@brief Switch the visible view when the sidebar selection changes.

        @param row New sidebar selection index.
        """
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
            # Refresh list-based views when they become visible
            if row == 2:
                self._stats_view.refresh_repo_list()
            elif row == 3:
                self._history_view.refresh_repo_list()

    def navigate_to(self, page: int) -> None:
        """@brief Programmatically switch to a named page.

        @param page Stack index (0=Repos, 1=Ingestion, 2=Stats, 3=History).
        """
        if 0 <= page < self._stack.count():
            self._sidebar.setCurrentRow(page)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_ingestion_started(self, repo_name: str) -> None:
        """@brief Switch to the Ingestion view when a new run starts.

        @param repo_name Repository name that started indexing.
        """
        self.navigate_to(1)
        self._update_status_bar()

    @Slot(str, dict)
    def _on_engine_repo_completed(self, repo_name: str, stats: dict) -> None:
        """@brief Update the status bar when ingestion completes.

        @param repo_name Repository name that finished.
        @param stats Final stats dict (unused here).
        """
        self._update_status_bar()

    @Slot()
    @Slot(str)
    @Slot(str, int)
    @Slot(str, str)
    def _update_status_bar(self, *_args) -> None:
        """@brief Refresh the status bar watcher/running counts."""
        watching = len(self._watcher.watched_repos())
        parts = []
        if watching:
            parts.append(f"Watching: {watching} repo(s)")
        if parts:
            self._sb_watch_label.setText(" | ".join(parts))
        else:
            self._sb_watch_label.setText("No active watchers")

    def _open_settings(self) -> None:
        """@brief Open the settings dialog and apply overrides on save."""
        try:
            from ingest import load_config
            import sys
            from pathlib import Path
            _root = Path(__file__).resolve().parent.parent.parent
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            cfg = load_config(str(_root / "codebrain.toml"))
            overrides = self._state.build_config_overrides()
            # Apply persisted overrides for display
            from desktop.core.engine import _deep_merge
            cfg = _deep_merge(cfg, overrides)
        except Exception:
            cfg = {}

        dlg = SettingsDialog(self._state, current_config=cfg, parent=self)
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.exec()

    def _on_settings_saved(self) -> None:
        """@brief Propagate updated overrides to the engine and watcher."""
        overrides = self._state.build_config_overrides()
        self._engine.set_config_overrides(overrides)
        self._watcher.set_config_overrides(overrides)

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """@brief Minimise to tray when watchers are active; quit otherwise.

        @param event The Qt close event.
        """
        if self._watcher.watched_repos():
            self.hide()
            event.ignore()
        else:
            event.accept()
