"""
@file ingestion_view.py
@brief Live ingestion progress view with per-repo progress bars and a file log.

IngestionView connects to both IngestionEngine and MultiRepoWatcher signals:
- Full ingestion runs: progress section with progress bar, counters, cancel button.
- Watch mode re-indexes: persistent watch-status section per watched repo showing
  live re-index counts; individual file events appended to the shared file log.

A shared scrolling log at the bottom shows the last N processed file paths
from both full ingestion runs and watch-triggered re-indexes.
"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop.core.engine import IngestionEngine
from desktop.core.watcher import MultiRepoWatcher

_LOG_MAX_LINES = 500


class _RepoProgressSection(QGroupBox):
    """@brief UI section for one active or recently completed ingestion run.

    Displays a progress bar, status counters, and a cancel button. The group
    box title shows the repository name. The cancel button triggers
    IngestionEngine.cancel_ingestion().
    """

    def __init__(self, repo_name: str, total: int, engine: IngestionEngine) -> None:
        """@brief Construct the progress section for one repository.

        @param repo_name Repository display name and engine key.
        @param total Total number of files to process.
        @param engine IngestionEngine instance for cancel support.
        """
        super().__init__(repo_name)
        self._repo_name = repo_name
        self._engine = engine
        self._total = total

        layout = QVBoxLayout(self)

        # Progress bar
        self._bar = QProgressBar()
        self._bar.setMinimum(0)
        self._bar.setMaximum(max(total, 1))
        self._bar.setValue(0)
        self._bar.setFormat(f"%v / {total} files")
        layout.addWidget(self._bar)

        # Status counters row
        counter_row = QHBoxLayout()
        self._lbl_indexed = QLabel("Indexed: 0")
        self._lbl_skipped = QLabel("Skipped: 0")
        self._lbl_errors = QLabel("Errors: 0")
        for lbl in (self._lbl_indexed, self._lbl_skipped, self._lbl_errors):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            counter_row.addWidget(lbl)
        layout.addLayout(counter_row)

        # Cancel / status row
        action_row = QHBoxLayout()
        self._status_label = QLabel("Running…")
        action_row.addWidget(self._status_label)
        action_row.addStretch()

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setFixedWidth(80)
        self._btn_cancel.clicked.connect(self._cancel)
        action_row.addWidget(self._btn_cancel)
        layout.addLayout(action_row)

        self._counters = {"indexed": 0, "skipped": 0, "errors": 0}

    def update_progress(self, current: int, result: dict) -> None:
        """@brief Advance the progress bar and update status counters.

        @param current Number of files processed so far.
        @param result Per-file result dict from process_file().
        """
        self._bar.setValue(current)
        status = result.get("status", "")
        if status == "indexed":
            self._counters["indexed"] += 1
        elif status == "skipped":
            self._counters["skipped"] += 1
        else:
            self._counters["errors"] += 1
            self._lbl_errors.setStyleSheet("color: red;")

        self._lbl_indexed.setText(f"Indexed: {self._counters['indexed']}")
        self._lbl_skipped.setText(f"Skipped: {self._counters['skipped']}")
        self._lbl_errors.setText(f"Errors: {self._counters['errors']}")

    def mark_completed(self, stats: dict) -> None:
        """@brief Update the section to reflect a completed ingestion run.

        @param stats Final stats dict from the worker.
        """
        self._bar.setValue(self._bar.maximum())
        text = (
            f"Done — {stats.get('indexed', 0)} indexed, "
            f"{stats.get('skipped', 0)} skipped, "
            f"{stats.get('errors', 0)} errors"
        )
        if stats.get("classifier_fallbacks", 0):
            text += f", {stats.get('classifier_fallbacks', 0)} classifier fallback(s)"
        self._status_label.setText(text)
        self._btn_cancel.setText("Dismiss")
        self._btn_cancel.clicked.disconnect()
        self._btn_cancel.clicked.connect(self._dismiss)

    def mark_error(self, message: str) -> None:
        """@brief Update the section to reflect a fatal ingestion error.

        @param message Error message to display.
        """
        self._status_label.setText(f"Error: {message}")
        self._status_label.setStyleSheet("color: red;")
        self._btn_cancel.setText("Dismiss")
        self._btn_cancel.clicked.disconnect()
        self._btn_cancel.clicked.connect(self._dismiss)

    def _cancel(self) -> None:
        """@brief Request cancellation via the engine."""
        self._engine.cancel_ingestion(self._repo_name)
        self._btn_cancel.setEnabled(False)
        self._status_label.setText("Cancelling…")

    def _dismiss(self) -> None:
        """@brief Remove this section from its parent layout."""
        parent = self.parentWidget()
        if parent:
            layout = parent.layout()
            if layout:
                layout.removeWidget(self)
        self.deleteLater()


class _SynthesisProgressSection(QGroupBox):
    """@brief UI section for one active or recently completed synthesis run.

    Displays a deterministic progress bar driven by SYNTH:phase:current:total
    lines emitted by synthesize_modules.py --machine.
    """

    _PHASE_LABELS = {
        "dir": "Synthesizing directory modules",
        "logical": "Synthesizing logical modules",
    }

    def __init__(self, repo_name: str) -> None:
        """@brief Construct the synthesis progress section.

        @param repo_name Repository name shown in the group box title.
        """
        super().__init__(f"Synthesis: {repo_name}")
        self._repo_name = repo_name

        layout = QVBoxLayout(self)

        self._bar = QProgressBar()
        self._bar.setMinimum(0)
        self._bar.setMaximum(0)  # indeterminate until first progress signal
        layout.addWidget(self._bar)

        action_row = QHBoxLayout()
        self._status_label = QLabel("Starting synthesis...")
        action_row.addWidget(self._status_label)
        action_row.addStretch()

        self._btn_dismiss = QPushButton("Dismiss")
        self._btn_dismiss.setFixedWidth(80)
        self._btn_dismiss.setEnabled(False)
        self._btn_dismiss.clicked.connect(self._dismiss)
        action_row.addWidget(self._btn_dismiss)
        layout.addLayout(action_row)

    def update_progress(self, current: int, total: int, phase: str) -> None:
        """@brief Advance the progress bar from a synthesis progress signal.

        @param current Items processed so far in this phase.
        @param total Total items in this phase.
        @param phase Phase identifier ('dir' or 'logical').
        """
        if total > 0:
            self._bar.setMaximum(total)
            self._bar.setValue(current)
            self._bar.setFormat(f"%v / {total}")
        label = self._PHASE_LABELS.get(phase, phase)
        self._status_label.setText(f"{label}... ({current}/{total})")

    def mark_completed(self, message: str) -> None:
        """@brief Update the section to reflect completed synthesis."""
        self._bar.setMaximum(100)
        self._bar.setValue(100)
        self._bar.setFormat("100%")
        self._status_label.setText(f"Done — {message}")
        self._btn_dismiss.setEnabled(True)

    def mark_error(self, message: str) -> None:
        """@brief Update the section to reflect a synthesis error."""
        self._bar.setMaximum(100)
        self._bar.setValue(100)
        self._status_label.setText(f"Error: {message}")
        self._status_label.setStyleSheet("color: red;")
        self._btn_dismiss.setEnabled(True)

    def _dismiss(self) -> None:
        """@brief Remove this section from its parent layout."""
        parent = self.parentWidget()
        if parent:
            layout = parent.layout()
            if layout:
                layout.removeWidget(self)
        self.deleteLater()


class _WatchStatusSection(QGroupBox):
    """@brief Persistent status section for one actively watched repository.

    Displayed while a repo is in watch mode. Shows a running count of
    re-indexed files and surfaces any watcher errors. Removed when watching stops.
    """

    def __init__(self, repo_name: str) -> None:
        """@brief Construct the watch status section.

        @param repo_name Repository name shown in the group box title.
        """
        super().__init__(f"Watching: {repo_name}")
        self._reindexed = 0
        self._errors = 0

        layout = QVBoxLayout(self)
        self._count_label = QLabel("Watching for file changes\u2026")
        layout.addWidget(self._count_label)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: red;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

    def on_file_reindexed(self, status: str) -> None:
        """@brief Update counters when a watched file is re-indexed.

        @param status 'indexed', 'skipped', or 'error'.
        """
        if status == "indexed":
            self._reindexed += 1
        self._count_label.setText(f"Re-indexed: {self._reindexed} file(s)")

    def on_error(self, error: str) -> None:
        """@brief Show the latest watcher error.

        @param error Error message string.
        """
        self._errors += 1
        self._error_label.setText(f"Last error: {error}")
        self._error_label.show()


class IngestionView(QWidget):
    """@brief Scrollable view displaying all active and recently completed ingestions.

    Connects to both IngestionEngine and MultiRepoWatcher signals:
    - Ingestion runs: progress sections with progress bars and cancel buttons.
    - Watch mode: persistent watch-status sections with live re-index counts.
    A shared file log at the bottom records all per-file events.
    """

    def __init__(self, engine: IngestionEngine, watcher: MultiRepoWatcher, parent=None) -> None:
        """@brief Construct the ingestion view and connect to the engine and watcher.

        @param engine IngestionEngine whose signals drive ingestion progress sections.
        @param watcher MultiRepoWatcher whose signals drive watch-status sections.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self._engine = engine
        self._sections: dict[str, _RepoProgressSection] = {}
        self._watch_sections: dict[str, _WatchStatusSection] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Scrollable area for per-repo sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._sections_widget = QWidget()
        self._sections_layout = QVBoxLayout(self._sections_widget)
        self._sections_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._empty_label = QLabel("No active ingestion runs or watchers.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: gray; font-style: italic;")
        self._sections_layout.addWidget(self._empty_label)
        scroll.setWidget(self._sections_widget)

        root_layout.addWidget(scroll, stretch=3)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root_layout.addWidget(sep)

        # Shared file log
        log_label = QLabel("Recent files & events:")
        log_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(log_label)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(_LOG_MAX_LINES)
        self._log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._log.setFixedHeight(160)
        root_layout.addWidget(self._log)

        # Wire engine signals (full ingestion runs)
        engine.repo_started.connect(self._on_repo_started)
        engine.progress.connect(self._on_progress)
        engine.repo_completed.connect(self._on_repo_completed)
        engine.repo_error.connect(self._on_repo_error)
        engine.file_processed.connect(self._on_file_processed)

        # Wire engine signals (synthesis)
        engine.synthesis_started.connect(self._on_synthesis_started)
        engine.synthesis_progress.connect(self._on_synthesis_progress)
        engine.synthesis_completed.connect(self._on_synthesis_completed)
        engine.synthesis_error.connect(self._on_synthesis_error)

        # Wire watcher signals (watch-mode re-indexes)
        watcher.watch_started.connect(self._on_watch_started)
        watcher.watch_stopped.connect(self._on_watch_stopped)
        watcher.file_changed.connect(self._on_watch_file_changed)
        watcher.watch_error.connect(self._on_watch_error)

        # Sync any repos already watching before this view was constructed
        # (e.g. auto-watch repos restored on startup).
        for name in watcher.watched_repos():
            self._on_watch_started(name)

    # ------------------------------------------------------------------
    # Engine signal handlers
    # ------------------------------------------------------------------

    @Slot(str, int)
    def _on_repo_started(self, repo_name: str, total: int) -> None:
        """@brief Add a new progress section when ingestion begins.

        @param repo_name Repository name.
        @param total Total files to process.
        """
        self._empty_label.hide()
        section = _RepoProgressSection(repo_name, total, self._engine)
        self._sections[repo_name] = section
        self._sections_layout.insertWidget(0, section)

    @Slot(str, int, int, dict)
    def _on_progress(self, repo_name: str, current: int, total: int, result: dict) -> None:
        """@brief Advance the progress bar for the given repo.

        @param repo_name Repository name.
        @param current Files processed so far.
        @param total Total files.
        @param result Per-file result dict, optionally including `warnings`.
        """
        section = self._sections.get(repo_name)
        if section:
            section.update_progress(current, result)
        path = result.get("path", "")
        for warning in result.get("warnings", []):
            self._log.appendPlainText(f"[!] {repo_name}/{path}: {warning}")

    @Slot(str, dict)
    def _on_repo_completed(self, repo_name: str, stats: dict) -> None:
        """@brief Mark the progress section as completed.

        @param repo_name Repository name.
        @param stats Final aggregated stats dict.
        """
        section = self._sections.pop(repo_name, None)
        if section:
            section.mark_completed(stats)
        self._check_empty()

    @Slot(str, str)
    def _on_repo_error(self, repo_name: str, message: str) -> None:
        """@brief Mark the progress section as errored.

        @param repo_name Repository name.
        @param message Error description.
        """
        section = self._sections.pop(repo_name, None)
        if section:
            section.mark_error(message)
        self._check_empty()

    @Slot(str, str, str)
    def _on_file_processed(self, repo_name: str, path: str, status: str) -> None:
        """@brief Append a line to the shared file log.

        @param repo_name Repository name (shown as prefix).
        @param path Relative path of the processed file.
        @param status One of 'indexed', 'skipped', 'errors'.
        """
        icon = {"indexed": "+", "skipped": "=", "errors": "!"}.get(status, "?")
        self._log.appendPlainText(f"[{icon}] {repo_name}/{path}")

    # ------------------------------------------------------------------
    # Synthesis signal handlers
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_synthesis_started(self, repo_name: str) -> None:
        self._empty_label.hide()
        section = _SynthesisProgressSection(repo_name)
        self._sections[f"synth_{repo_name}"] = section
        self._sections_layout.insertWidget(0, section)
        self._log.appendPlainText(f"[i] {repo_name}: Module synthesis started...")

    @Slot(str, int, int, str)
    def _on_synthesis_progress(self, repo_name: str, current: int,
                               total: int, phase: str) -> None:
        """@brief Update synthesis progress bar from machine-readable output.

        @param repo_name Repository name.
        @param current Items processed so far.
        @param total Total items in this phase.
        @param phase Phase identifier ('dir' or 'logical').
        """
        section = self._sections.get(f"synth_{repo_name}")
        if section and isinstance(section, _SynthesisProgressSection):
            section.update_progress(current, total, phase)

    @Slot(str, str)
    def _on_synthesis_completed(self, repo_name: str, message: str) -> None:
        section = self._sections.get(f"synth_{repo_name}")
        if section and isinstance(section, _SynthesisProgressSection):
            section.mark_completed(message)
        self._log.appendPlainText(f"[+] {repo_name}: {message}")

    @Slot(str, str)
    def _on_synthesis_error(self, repo_name: str, error: str) -> None:
        section = self._sections.get(f"synth_{repo_name}")
        if section and isinstance(section, _SynthesisProgressSection):
            section.mark_error(error)
        self._log.appendPlainText(f"[!] {repo_name}: Synthesis error: {error}")

    # ------------------------------------------------------------------
    # Watcher signal handlers
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_watch_started(self, repo_name: str) -> None:
        """@brief Add a watch-status section when a repo starts being watched.

        @param repo_name Repository name that started watching.
        """
        if repo_name in self._watch_sections:
            return
        self._empty_label.hide()
        section = _WatchStatusSection(repo_name)
        self._watch_sections[repo_name] = section
        self._sections_layout.insertWidget(0, section)

    @Slot(str)
    def _on_watch_stopped(self, repo_name: str) -> None:
        """@brief Remove the watch-status section when watching stops.

        @param repo_name Repository name that stopped watching.
        """
        section = self._watch_sections.pop(repo_name, None)
        if section:
            self._sections_layout.removeWidget(section)
            section.deleteLater()
        self._check_empty()

    @Slot(str, str, str)
    def _on_watch_file_changed(self, repo_name: str, rel_path: str, status: str) -> None:
        """@brief Update the watch-status section and append to the file log.

        @param repo_name Repository whose file changed.
        @param rel_path Relative path of the re-indexed file.
        @param status 'indexed', 'skipped', or 'error'.
        """
        section = self._watch_sections.get(repo_name)
        if section:
            section.on_file_reindexed(status)
        icon = {"indexed": "~", "skipped": "=", "error": "!"}.get(status, "?")
        self._log.appendPlainText(f"[{icon}] {repo_name}/{rel_path}")

    @Slot(str, str)
    def _on_watch_error(self, repo_name: str, error: str) -> None:
        """@brief Surface a watcher error in the status section and file log.

        @param repo_name Repository that encountered the error.
        @param error Error message string.
        """
        section = self._watch_sections.get(repo_name)
        if section:
            section.on_error(error)
        self._log.appendPlainText(f"[!] {repo_name}: {error}")

    def _check_empty(self) -> None:
        """@brief Show the empty-state label when no sections remain."""
        if not self._sections and not self._watch_sections:
            self._empty_label.show()
