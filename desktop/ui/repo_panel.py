"""
@file repo_panel.py
@brief Repository management panel: add, remove, index, and watch repos.

RepoPanel is the primary view of the application. It shows a scrollable list
of RepoCard widgets — one per registered repository. Each card displays the
repo name, filesystem path, last ingestion summary, and provides controls
for running ingestion and toggling the file watcher.
"""

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop.core.engine import IngestionEngine
from desktop.core.state import AppState
from desktop.core.watcher import MultiRepoWatcher

# Status badge colours (stylesheet fragments)
_BADGE_IDLE = "background:#e0e0e0; color:#333; border-radius:4px; padding:2px 6px;"
_BADGE_RUNNING = "background:#1976D2; color:white; border-radius:4px; padding:2px 6px;"
_BADGE_WATCHING = "background:#388E3C; color:white; border-radius:4px; padding:2px 6px;"
_BADGE_ERROR = "background:#D32F2F; color:white; border-radius:4px; padding:2px 6px;"
_BADGE_DONE = "background:#616161; color:white; border-radius:4px; padding:2px 6px;"


class RepoCard(QFrame):
    """@brief Card widget representing one registered repository.

    Emits request_index and request_watch so the parent panel can delegate
    to the engine and watcher without the card holding direct references.
    """

    request_index = Signal(str)         # (repo_path,)
    request_reindex = Signal(str)       # (repo_path,) — force=True
    request_synthesize = Signal(str)    # (repo_name,) — synthesize modules
    request_cancel = Signal(str)        # (repo_name,)
    request_watch = Signal(str, bool)   # (repo_path, enabled)
    request_remove = Signal(str)        # (repo_path,)

    def __init__(self, repo: dict, engine: IngestionEngine, watcher: MultiRepoWatcher) -> None:
        """@brief Construct a card from a repo state dict.

        @param repo Row dict from AppState.list_repos().
        @param engine IngestionEngine for status queries.
        @param watcher MultiRepoWatcher for watch status queries.
        """
        super().__init__()
        self._path = repo["path"]
        self._name = repo["name"]
        self._engine = engine
        self._watcher = watcher

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        main_layout = QVBoxLayout(self)

        # --- Header row: name + status badge + remove button ---
        header = QHBoxLayout()
        name_lbl = QLabel(f"<b>{self._name}</b>")
        header.addWidget(name_lbl)
        header.addStretch()

        self._status_badge = QLabel("idle")
        self._status_badge.setStyleSheet(_BADGE_IDLE)
        header.addWidget(self._status_badge)

        btn_remove = QPushButton("✕")
        btn_remove.setToolTip("Remove this repository")
        btn_remove.setFixedWidth(28)
        btn_remove.setFlat(True)
        btn_remove.clicked.connect(lambda: self.request_remove.emit(self._path))
        header.addWidget(btn_remove)
        main_layout.addLayout(header)

        # --- Path label ---
        path_lbl = QLabel(self._path)
        path_lbl.setStyleSheet("color: gray; font-size: 11px;")
        path_lbl.setWordWrap(True)
        main_layout.addWidget(path_lbl)

        # --- Last ingestion summary ---
        self._last_run_lbl = QLabel()
        self._last_run_lbl.setStyleSheet("font-size: 11px;")
        self._update_last_run_label(repo)
        main_layout.addWidget(self._last_run_lbl)

        # --- Action buttons ---
        actions = QHBoxLayout()

        self._btn_index = QPushButton("Index Now")
        self._btn_index.clicked.connect(self._on_index_clicked)
        actions.addWidget(self._btn_index)

        self._btn_reindex = QPushButton("Re-index")
        self._btn_reindex.setToolTip("Force re-index all files (equivalent to --force)")
        self._btn_reindex.clicked.connect(self._on_reindex_clicked)
        actions.addWidget(self._btn_reindex)

        self._btn_synthesize = QPushButton("Synthesize")
        self._btn_synthesize.setToolTip("Run module intent synthesis")
        self._btn_synthesize.clicked.connect(self._on_synthesize_clicked)
        actions.addWidget(self._btn_synthesize)

        self._btn_watch = QPushButton()
        self._update_watch_button()
        self._btn_watch.clicked.connect(self._on_watch_clicked)
        actions.addWidget(self._btn_watch)

        actions.addStretch()
        main_layout.addLayout(actions)

    # ------------------------------------------------------------------
    # State update methods (called by RepoPanel)
    # ------------------------------------------------------------------

    def set_running(self, running: bool) -> None:
        """@brief Update the card to reflect an active or completed ingestion.

        @param running True while an ingestion worker is active.
        """
        if running:
            self._status_badge.setText("indexing")
            self._status_badge.setStyleSheet(_BADGE_RUNNING)
            self._btn_index.setText("Cancel")
            self._btn_index.clicked.disconnect()
            self._btn_index.clicked.connect(self._on_cancel_clicked)
            self._btn_reindex.setEnabled(False)
            self._btn_synthesize.setEnabled(False)
        else:
            self._btn_index.setText("Index Now")
            try:
                self._btn_index.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_index.clicked.connect(self._on_index_clicked)
            self._btn_reindex.setEnabled(True)
            self._btn_synthesize.setEnabled(True)
            self._update_status_badge()

    def set_watching(self, watching: bool) -> None:
        """@brief Update the card to reflect watch mode state.

        @param watching True while the watcher is active for this repo.
        """
        self._update_watch_button(watching)
        self._update_status_badge()

    def update_after_ingestion(self, repo: dict) -> None:
        """@brief Refresh the last-run label from the latest repo state dict.

        @param repo Updated repo row dict from AppState.
        """
        self._update_last_run_label(repo)
        self._update_status_badge()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _on_index_clicked(self) -> None:
        """@brief Emit request_index with this repo's path."""
        self.request_index.emit(self._path)

    def _on_reindex_clicked(self) -> None:
        """@brief Emit request_reindex to trigger a force re-index."""
        self.request_reindex.emit(self._path)

    def _on_synthesize_clicked(self) -> None:
        """@brief Emit request_synthesize with this repo's name."""
        self.request_synthesize.emit(self._name)

    def _on_cancel_clicked(self) -> None:
        """@brief Emit request_cancel with this repo's name."""
        self.request_cancel.emit(self._name)

    def _on_watch_clicked(self) -> None:
        """@brief Toggle watch mode and emit request_watch."""
        currently_watching = self._watcher.is_watching(self._name)
        self.request_watch.emit(self._path, not currently_watching)

    def _update_watch_button(self, watching: Optional[bool] = None) -> None:
        """@brief Sync the watch button label with current watch state.

        @param watching Override; if None, queries the watcher directly.
        """
        if watching is None:
            watching = self._watcher.is_watching(self._name)
        self._btn_watch.setText("Stop Watching" if watching else "Start Watching")

    def _update_status_badge(self) -> None:
        """@brief Set the status badge colour and text based on current state."""
        if self._engine.is_running(self._name):
            self._status_badge.setText("indexing")
            self._status_badge.setStyleSheet(_BADGE_RUNNING)
        elif self._watcher.is_watching(self._name):
            self._status_badge.setText("watching")
            self._status_badge.setStyleSheet(_BADGE_WATCHING)
        else:
            self._status_badge.setText("idle")
            self._status_badge.setStyleSheet(_BADGE_IDLE)

    def _update_last_run_label(self, repo: dict) -> None:
        """@brief Refresh the last-run summary label from repo metadata.

        Handles two stat formats:
        - From the desktop engine: {indexed, skipped, errors, chunks, symbols}
        - Synced from PostgreSQL ingestion_runs: {files_processed, chunks, symbols}

        @param repo Repo row dict from AppState.
        """
        at = repo.get("last_ingestion_at")
        status = repo.get("last_ingestion_status")
        stats = repo.get("last_ingestion_stats") or {}
        if at and status:
            date_str = at[:16].replace("T", " ")
            # Prefer desktop-engine breakdown; fall back to PG aggregate counts.
            if "indexed" in stats:
                detail = (
                    f"{stats['indexed']} indexed, "
                    f"{stats.get('skipped', 0)} skipped, "
                    f"{stats.get('errors', 0)} errors"
                )
                if stats.get("classifier_fallbacks", 0):
                    detail += f", {stats.get('classifier_fallbacks', 0)} classifier fallback(s)"
            elif "files_processed" in stats:
                files = stats["files_processed"]
                chunks = stats.get("chunks", 0)
                symbols = stats.get("symbols", 0)
                detail = f"{files} files, {chunks} chunks, {symbols} symbols"
            else:
                detail = ""
            text = f"Last run: {date_str} — {status}"
            if detail:
                text += f"  ({detail})"
        else:
            text = "Never indexed"
        self._last_run_lbl.setText(text)


class RepoPanel(QWidget):
    """@brief Scrollable list of repository cards with an Add Repository button.

    Manages RepoCard instances, routes index and watch requests to the engine
    and watcher, and keeps the AppState in sync with user actions.
    """

    # Emitted so MainWindow can switch to the Ingestion view.
    ingestion_started = Signal(str)  # (repo_name,)

    def __init__(
        self,
        state: AppState,
        engine: IngestionEngine,
        watcher: MultiRepoWatcher,
        parent=None,
    ) -> None:
        """@brief Construct the panel and populate it from AppState.

        @param state AppState for repo persistence.
        @param engine IngestionEngine for indexing.
        @param watcher MultiRepoWatcher for file watching.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self._state = state
        self._engine = engine
        self._watcher = watcher
        self._cards: dict[str, RepoCard] = {}  # keyed by repo path

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(8, 8, 8, 4)
        title = QLabel("<h2>Repositories</h2>")
        header.addWidget(title)
        header.addStretch()

        btn_add = QPushButton("+ Add Repository")
        btn_add.clicked.connect(self._add_repo)
        header.addWidget(btn_add)
        root_layout.addLayout(header)

        # Scrollable card list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._cards_widget = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_widget)
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._cards_layout.setSpacing(8)
        self._cards_layout.setContentsMargins(8, 0, 8, 8)

        self._empty_label = QLabel(
            "No repositories added yet.\nClick '+ Add Repository' to get started."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: gray; font-style: italic;")
        self._cards_layout.addWidget(self._empty_label)

        scroll.setWidget(self._cards_widget)
        root_layout.addWidget(scroll)

        # Wire engine and watcher signals
        engine.repo_started.connect(self._on_repo_started)
        engine.repo_completed.connect(self._on_repo_completed)
        engine.repo_error.connect(self._on_repo_error)
        watcher.watch_started.connect(self._on_watch_started)
        watcher.watch_stopped.connect(self._on_watch_stopped)

        # Populate from persisted state, syncing last-run info from PostgreSQL
        # for repos that were indexed via the CLI before the desktop app was used.
        for repo in state.list_repos():
            self._sync_pg_history(repo["path"])
            updated = state.get_repo(repo["path"]) or repo
            self._add_card(updated)

    # ------------------------------------------------------------------
    # Repo management
    # ------------------------------------------------------------------

    def _add_repo(self) -> None:
        """@brief Open a folder picker and register the chosen directory."""
        path = QFileDialog.getExistingDirectory(
            self, "Select Repository Root", str(Path.home())
        )
        if not path:
            return
        if self._state.get_repo(path):
            QMessageBox.information(
                self, "Already Added",
                f"Repository at {path} is already registered."
            )
            return
        self._state.add_repo(path)
        # Backfill last-run metadata from PostgreSQL for repos already indexed via CLI.
        self._sync_pg_history(path)
        repo = self._state.get_repo(path) or self._state.add_repo(path)
        self._add_card(repo)

    def _sync_pg_history(self, repo_path: str) -> None:
        """@brief Pull the most recent ingestion run from PostgreSQL into AppState.

        Called when adding a repo or at startup so repos that were indexed
        via the CLI (not through the desktop app) show the correct last-run info.
        No-ops silently if PostgreSQL is unreachable.

        @param repo_path Absolute path to the repository root.
        """
        repo_name = Path(repo_path).name
        history = self._engine.get_ingestion_history(repo_name=repo_name, limit=1)
        if not history:
            return
        run = history[0]
        started_at = run.get("started_at")
        at = str(started_at)[:19] if started_at else None
        stats = {
            "files_processed": run.get("files_processed") or 0,
            "chunks": run.get("chunks_created") or 0,
            "symbols": run.get("symbols_found") or 0,
        }
        self._state.update_ingestion_result(
            repo_path,
            run.get("status", "completed"),
            stats,
            at=at,
        )

    def _add_card(self, repo: dict) -> None:
        """@brief Create a RepoCard and insert it into the scrollable list.

        @param repo Repo row dict from AppState.
        """
        card = RepoCard(repo, self._engine, self._watcher)
        card.request_index.connect(self._on_request_index)
        card.request_reindex.connect(self._on_request_reindex)
        card.request_synthesize.connect(self._on_request_synthesize)
        card.request_cancel.connect(self._engine.cancel_ingestion)
        card.request_watch.connect(self._on_request_watch)
        card.request_remove.connect(self._on_request_remove)
        self._cards[repo["path"]] = card
        self._cards_layout.insertWidget(0, card)
        self._empty_label.hide()

    # ------------------------------------------------------------------
    # Button-action handlers
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_request_index(self, repo_path: str) -> None:
        """@brief Start ingestion for the requested repo.

        @param repo_path Absolute path to the repository root.
        """
        started = self._engine.start_ingestion(repo_path)
        if started:
            self.ingestion_started.emit(Path(repo_path).name)

    @Slot(str)
    def _on_request_reindex(self, repo_path: str) -> None:
        """@brief Start force re-indexing for the requested repo.

        Equivalent to ingest.py --force.

        @param repo_path Absolute path to the repository root.
        """
        started = self._engine.start_ingestion(repo_path, force=True)
        if started:
            self.ingestion_started.emit(Path(repo_path).name)

    @Slot(str)
    def _on_request_synthesize(self, repo_name: str) -> None:
        """@brief Run synthesize_modules.py in a background process.

        @param repo_name Name of the repository to synthesize.
        """
        import subprocess
        import sys
        
        QMessageBox.information(
            self,
            "Synthesis Started",
            f"Module synthesis for '{repo_name}' has started in the background. It may take a few minutes."
        )
        
        def run_synthesis():
            try:
                subprocess.run(
                    [sys.executable, "synthesize_modules.py", "--repo", repo_name, "--mode", "all"],
                    cwd=str(Path(__file__).resolve().parent.parent.parent),
                    capture_output=True
                )
            except Exception as e:
                print(f"Synthesis failed: {e}")

        import threading
        threading.Thread(target=run_synthesis, daemon=True).start()

    @Slot(str, bool)
    def _on_request_watch(self, repo_path: str, enable: bool) -> None:
        """@brief Start or stop watching the requested repo.

        @param repo_path Absolute path to the repository root.
        @param enable True to start watching; False to stop.
        """
        repo_name = Path(repo_path).name
        if enable:
            self._watcher.start_watching(repo_path)
            self._state.set_auto_watch(repo_path, True)
        else:
            self._watcher.stop_watching(repo_name)
            self._state.set_auto_watch(repo_path, False)

    @Slot(str)
    def _on_request_remove(self, repo_path: str) -> None:
        """@brief Confirm and remove a repository from the panel and state.

        @param repo_path Absolute path to the repository to remove.
        """
        repo_name = Path(repo_path).name
        answer = QMessageBox.question(
            self,
            "Remove Repository",
            f"Remove '{repo_name}' from CodeBrain Desktop?\n\n"
            "The indexed data in PostgreSQL is NOT deleted.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._watcher.is_watching(repo_name):
            self._watcher.stop_watching(repo_name)
        card = self._cards.pop(repo_path, None)
        if card:
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._state.remove_repo(repo_path)
        if not self._cards:
            self._empty_label.show()

    # ------------------------------------------------------------------
    # Engine and watcher signal handlers
    # ------------------------------------------------------------------

    @Slot(str, int)
    def _on_repo_started(self, repo_name: str, total: int) -> None:
        """@brief Mark the matching card as running.

        @param repo_name Repository name.
        @param total Total files to process (unused here).
        """
        card = self._card_by_name(repo_name)
        if card:
            card.set_running(True)

    @Slot(str, dict)
    def _on_repo_completed(self, repo_name: str, stats: dict) -> None:
        """@brief Update the card and persist the result in AppState.

        @param repo_name Repository name.
        @param stats Final aggregated stats dict.
        """
        card = self._card_by_name(repo_name)
        if not card:
            return
        path = self._path_by_name(repo_name)
        if path:
            self._state.update_ingestion_result(path, "completed", stats)
            repo = self._state.get_repo(path)
            if repo:
                card.update_after_ingestion(repo)
        card.set_running(False)

    @Slot(str, str)
    def _on_repo_error(self, repo_name: str, message: str) -> None:
        """@brief Mark the card as errored.

        @param repo_name Repository name.
        @param message Error description.
        """
        card = self._card_by_name(repo_name)
        if card:
            card.set_running(False)
        path = self._path_by_name(repo_name)
        if path:
            self._state.update_ingestion_result(path, "error", {})

    @Slot(str)
    def _on_watch_started(self, repo_name: str) -> None:
        """@brief Update the card to reflect watch mode active.

        @param repo_name Repository name.
        """
        card = self._card_by_name(repo_name)
        if card:
            card.set_watching(True)

    @Slot(str)
    def _on_watch_stopped(self, repo_name: str) -> None:
        """@brief Update the card to reflect watch mode stopped.

        @param repo_name Repository name.
        """
        card = self._card_by_name(repo_name)
        if card:
            card.set_watching(False)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _card_by_name(self, repo_name: str) -> Optional[RepoCard]:
        """@brief Find a card by repo name (basename).

        @param repo_name Repository directory name.
        @return Matching RepoCard or None.
        """
        for path, card in self._cards.items():
            if Path(path).name == repo_name:
                return card
        return None

    def _path_by_name(self, repo_name: str) -> Optional[str]:
        """@brief Find the registered path for a given repo name.

        @param repo_name Repository directory name.
        @return Absolute path string or None.
        """
        for path in self._cards:
            if Path(path).name == repo_name:
                return path
        return None
