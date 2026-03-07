"""
@file stats_view.py
@brief Per-repository statistics view querying the PostgreSQL database.

StatsView lets the user select a registered repository from a drop-down
and displays aggregate counts (files, chunks, symbols) plus a language
breakdown table. Data is fetched synchronously via IngestionEngine on demand.
"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop.core.engine import IngestionEngine
from desktop.core.state import AppState


class StatsView(QWidget):
    """@brief Displays aggregate statistics for a selected repository.

    Queries the PostgreSQL files, code_chunks, and symbols tables via
    IngestionEngine.get_repo_stats(). Refreshes on repo selection change
    and on explicit refresh button click.
    """

    def __init__(
        self, state: AppState, engine: IngestionEngine, parent=None
    ) -> None:
        """@brief Construct the stats view.

        @param state AppState for the repo name list.
        @param engine IngestionEngine for DB queries.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self._state = state
        self._engine = engine

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        # Header
        header = QHBoxLayout()
        header.addWidget(QLabel("<h2>Statistics</h2>"))
        header.addStretch()
        root.addLayout(header)

        # Repo selector
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Repository:"))
        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo.currentTextChanged.connect(self._refresh)
        selector_row.addWidget(self._combo)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh)
        selector_row.addWidget(btn_refresh)
        root.addLayout(selector_row)

        # Summary counters group
        summary_group = QGroupBox("Summary")
        summary_layout = QHBoxLayout(summary_group)

        self._lbl_files = self._stat_label("Files", "—")
        self._lbl_chunks = self._stat_label("Chunks", "—")
        self._lbl_symbols = self._stat_label("Symbols", "—")
        for widget in (self._lbl_files, self._lbl_chunks, self._lbl_symbols):
            summary_layout.addWidget(widget)

        root.addWidget(summary_group)

        # Language breakdown table
        lang_group = QGroupBox("Language Breakdown")
        lang_layout = QVBoxLayout(lang_group)

        self._lang_table = QTableWidget(0, 2)
        self._lang_table.setHorizontalHeaderLabels(["Language", "Files"])
        self._lang_table.horizontalHeader().setStretchLastSection(True)
        self._lang_table.verticalHeader().setVisible(False)
        self._lang_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._lang_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        lang_layout.addWidget(self._lang_table)
        root.addWidget(lang_group, stretch=1)

        # Module intents table
        modules_group = QGroupBox("Module Intents")
        modules_layout = QVBoxLayout(modules_group)

        self._modules_table = QTableWidget(0, 5)
        self._modules_table.setHorizontalHeaderLabels(["Module", "Kind", "Role", "Dominant Intent", "Files"])
        self._modules_table.horizontalHeader().setStretchLastSection(True)
        self._modules_table.verticalHeader().setVisible(False)
        self._modules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._modules_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        modules_layout.addWidget(self._modules_table)
        root.addWidget(modules_group, stretch=2)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: gray; font-style: italic;")
        root.addWidget(self._status_label)
        root.addStretch()

        # Connect to ingestion completion to auto-refresh
        engine.repo_completed.connect(self._on_repo_completed)

        self.refresh_repo_list()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_repo_list(self) -> None:
        """@brief Repopulate the repo combo box from AppState."""
        current = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        for repo in self._state.list_repos():
            self._combo.addItem(repo["name"], repo["path"])
        # Restore previous selection if still present
        idx = self._combo.findText(current)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)
        self._refresh()

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    @Slot()
    def _refresh(self) -> None:
        """@brief Fetch and display stats for the currently selected repo."""
        repo_name = self._combo.currentText()
        if not repo_name:
            self._clear_stats()
            return

        self._status_label.setText("Loading…")
        stats = self._engine.get_repo_stats(repo_name)

        if stats is None:
            self._clear_stats()
            self._status_label.setText(
                "Could not load stats. Is PostgreSQL running?"
            )
            self._status_label.setStyleSheet("color: red;")
            return

        self._lbl_files.setProperty("value", str(stats["file_count"]))
        self._lbl_files.findChild(QLabel, "value_label").setText(
            str(stats["file_count"])
        )
        self._lbl_chunks.findChild(QLabel, "value_label").setText(
            str(stats["chunk_count"])
        )
        self._lbl_symbols.findChild(QLabel, "value_label").setText(
            str(stats["symbol_count"])
        )

        langs = stats.get("languages", [])
        self._lang_table.setRowCount(len(langs))
        for row, lang in enumerate(langs):
            self._lang_table.setItem(row, 0, QTableWidgetItem(lang["language"] or "unknown"))
            count_item = QTableWidgetItem(str(lang["count"]))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._lang_table.setItem(row, 1, count_item)

        modules = self._engine.get_module_intents(repo_name)
        self._modules_table.setRowCount(len(modules))
        for row, mod in enumerate(modules):
            self._modules_table.setItem(row, 0, QTableWidgetItem(mod["module_name"] or mod["module_path"]))
            self._modules_table.setItem(row, 1, QTableWidgetItem(mod["kind"]))
            self._modules_table.setItem(row, 2, QTableWidgetItem(mod["role"] or "unknown"))
            self._modules_table.setItem(row, 3, QTableWidgetItem(mod["dominant_intent"] or "unknown"))
            count_item = QTableWidgetItem(str(mod["file_count"]))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._modules_table.setItem(row, 4, count_item)

        self._status_label.setText(f"Stats for '{repo_name}'")
        self._status_label.setStyleSheet("color: gray; font-style: italic;")

    def _clear_stats(self) -> None:
        """@brief Reset all stat labels and the language table to empty state."""
        for grp_widget in (self._lbl_files, self._lbl_chunks, self._lbl_symbols):
            val = grp_widget.findChild(QLabel, "value_label")
            if val:
                val.setText("—")
        self._lang_table.setRowCount(0)
        self._modules_table.setRowCount(0)

    @Slot(str, dict)
    def _on_repo_completed(self, repo_name: str, _stats: dict) -> None:
        """@brief Auto-refresh when the displayed repo finishes indexing.

        @param repo_name Repo that just finished.
        @param _stats Final stats (unused here; we query DB directly).
        """
        if repo_name == self._combo.currentText():
            self._refresh()

    @staticmethod
    def _stat_label(title: str, initial: str) -> QWidget:
        """@brief Build a labelled stat counter widget (title above, value below).

        @param title Short label text (e.g. 'Files').
        @param initial Initial value string (e.g. '—').
        @return Container widget with two child QLabels.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet("font-size: 11px; color: gray;")
        layout.addWidget(title_lbl)

        value_lbl = QLabel(initial)
        value_lbl.setObjectName("value_label")
        value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_lbl.setStyleSheet("font-size: 28px; font-weight: bold;")
        layout.addWidget(value_lbl)

        return container
