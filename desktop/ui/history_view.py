"""
@file history_view.py
@brief Ingestion run history view querying the PostgreSQL ingestion_runs table.

HistoryView shows a filterable table of past ingestion runs with columns for
repository name, start/end timestamps, file counts, and status. The user can
filter by repository and refresh manually.
"""

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
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

_COLUMNS = [
    "Repository", "Started", "Completed", "Files", "Chunks", "Symbols", "Status"
]

_STATUS_COLOURS = {
    "completed": "#388E3C",
    "running": "#1976D2",
    "error": "#D32F2F",
}


def _fmt_dt(dt) -> str:
    """@brief Format a datetime or ISO string for display.

    @param dt datetime object or ISO-format string.
    @return Formatted 'YYYY-MM-DD HH:MM' string, or '—' if None/invalid.
    """
    if dt is None:
        return "—"
    try:
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(dt)[:16]


class HistoryView(QWidget):
    """@brief Table view of past ingestion runs from the ingestion_runs table.

    Queries IngestionEngine.get_ingestion_history() on demand and when new
    ingestions complete. Supports per-repo filtering via a combo box.
    """

    def __init__(
        self, state: AppState, engine: IngestionEngine, parent=None
    ) -> None:
        """@brief Construct the history view.

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
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("<h2>Ingestion History</h2>"))
        header_row.addStretch()
        root.addLayout(header_row)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter by repo:"))

        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo.addItem("All repositories", None)
        self._combo.currentIndexChanged.connect(self._refresh)
        filter_row.addWidget(self._combo)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh)
        filter_row.addWidget(btn_refresh)
        root.addLayout(filter_row)

        # Table
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        root.addWidget(self._table)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: gray; font-style: italic;")
        root.addWidget(self._status_label)

        engine.repo_completed.connect(self._on_repo_completed)

        self.refresh_repo_list()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_repo_list(self) -> None:
        """@brief Repopulate the filter combo box from AppState."""
        current_data = self._combo.currentData()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("All repositories", None)
        for repo in self._state.list_repos():
            self._combo.addItem(repo["name"], repo["name"])
        # Restore previous selection
        if current_data:
            idx = self._combo.findData(current_data)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)
        self._refresh()

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    @Slot()
    def _refresh(self) -> None:
        """@brief Reload history rows from the database."""
        repo_name: Optional[str] = self._combo.currentData()
        self._status_label.setText("Loading…")

        rows = self._engine.get_ingestion_history(repo_name=repo_name, limit=200)

        self._table.setRowCount(len(rows))
        for r, run in enumerate(rows):
            self._table.setItem(r, 0, QTableWidgetItem(run.get("repo", "")))
            self._table.setItem(r, 1, QTableWidgetItem(_fmt_dt(run.get("started_at"))))
            self._table.setItem(r, 2, QTableWidgetItem(_fmt_dt(run.get("completed_at"))))

            for col, key in [(3, "files_processed"), (4, "chunks_created"), (5, "symbols_found")]:
                val = run.get(key)
                item = QTableWidgetItem("—" if val is None else str(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(r, col, item)

            status = run.get("status", "")
            status_item = QTableWidgetItem(status)
            colour = _STATUS_COLOURS.get(status, "#333")
            status_item.setForeground(Qt.GlobalColor.white)
            self._table.setItem(r, 6, status_item)

        count = len(rows)
        label = f"All repositories" if not repo_name else f"'{repo_name}'"
        self._status_label.setText(f"{count} run(s) — {label}")

    @Slot(str, dict)
    def _on_repo_completed(self, repo_name: str, _stats: dict) -> None:
        """@brief Auto-refresh after any ingestion completes.

        @param repo_name Repo that finished (used to check filter).
        @param _stats Unused final stats dict.
        """
        current = self._combo.currentData()
        if current is None or current == repo_name:
            self._refresh()
