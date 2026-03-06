"""
@file app.py
@brief Application lifecycle coordinator for CodeBrain Desktop.

CodeBrainApp initialises all subsystems (AppState, IngestionEngine,
MultiRepoWatcher, MainWindow, SystemTrayManager) in the correct order,
restores auto-watched repositories from the previous session, and provides
a clean teardown path on application exit.
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_DEFAULT_CONFIG = str(_ROOT / "codebrain.toml")


class CodeBrainApp:
    """@brief Top-level coordinator for the CodeBrain desktop application.

    Instantiated once by __main__.py. Holds references to all major
    subsystems so they are not garbage-collected during the Qt event loop.
    """

    def __init__(self) -> None:
        """@brief Construct all subsystems.

        The order matters: AppState must exist before engine and watcher
        (for override loading); engine and watcher must exist before
        MainWindow and tray (which connect to their signals).
        """
        # Deferred imports keep startup fast and avoid circular imports
        # at module level (all desktop.* modules import from the project root).
        from desktop.core.engine import IngestionEngine
        from desktop.core.state import AppState
        from desktop.core.watcher import MultiRepoWatcher
        from desktop.ui.main_window import MainWindow
        from desktop.ui.tray import SystemTrayManager

        self._state = AppState()

        # Load config overrides from the previous session.
        overrides = self._state.build_config_overrides()

        self._engine = IngestionEngine(config_path=_DEFAULT_CONFIG)
        self._engine.set_config_overrides(overrides)

        self._watcher = MultiRepoWatcher(
            config_path=_DEFAULT_CONFIG,
            config_overrides=overrides,
        )

        # Tray manager creates the shared app icon; pass it to MainWindow.
        self._tray = SystemTrayManager(
            main_window=None,  # MainWindow not yet constructed
            watcher=self._watcher,
        )

        self._window = MainWindow(
            state=self._state,
            engine=self._engine,
            watcher=self._watcher,
            app_icon=self._tray.app_icon(),
        )

        # Now that MainWindow exists, wire up the tray reference.
        self._tray._main_window = self._window

        # Notify tray on file-watcher events so the user sees balloon messages.
        self._watcher.file_changed.connect(self._on_file_changed)

        # Persist auto-watch state when watcher stops (e.g. user-initiated stop).
        self._watcher.watch_stopped.connect(self._on_watch_stopped)

        # Clean up on Qt quit.
        QApplication.instance().aboutToQuit.connect(self._on_quit)

    def start(self) -> None:
        """@brief Show the main window, tray icon, and restore auto-watched repos.

        Call after QApplication is created but before app.exec().
        """
        self._tray.show()
        self._window.show()
        self._restore_auto_watchers()

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _restore_auto_watchers(self) -> None:
        """@brief Start watchers for repos that had auto_watch=True last session."""
        for repo in self._state.list_repos():
            if repo.get("auto_watch"):
                self._watcher.start_watching(repo["path"])

    def _on_file_changed(self, repo_name: str, rel_path: str, status: str) -> None:
        """@brief Show a tray notification when a watched file is re-indexed.

        @param repo_name Repository whose file changed.
        @param rel_path Relative path of the changed file.
        @param status 'indexed', 'skipped', or 'error'.
        """
        if status == "indexed":
            self._tray.notify(
                f"{repo_name} updated",
                f"Re-indexed: {rel_path}",
            )

    def _on_watch_stopped(self, repo_name: str) -> None:
        """@brief Clear the auto_watch flag when a repo's watcher stops.

        Prevents unintended re-watch on the next launch if the user
        explicitly stopped watching rather than the app restarting.
        Note: the auto_watch flag is only set when the user toggles it
        via the UI, so this only updates repos that match by name.

        @param repo_name Repository name whose watcher stopped.
        """
        for repo in self._state.list_repos():
            if repo["name"] == repo_name:
                # Only clear if it was set (don't touch repos that never had it).
                if repo.get("auto_watch"):
                    self._state.set_auto_watch(repo["path"], False)
                break

    def _on_quit(self) -> None:
        """@brief Tear down all active watchers and close the state DB."""
        self._watcher.stop_all()
        self._engine.stop_all()
        self._state.close()
