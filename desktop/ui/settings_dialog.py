"""
@file settings_dialog.py
@brief Settings dialog for editing database, embedding, and classifier config.

Reads the effective configuration (codebrain.toml merged with any existing
overrides) and persists user changes as app setting overrides in the local
SQLite AppState. Changes take effect on the next ingestion or watcher start.
"""

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from desktop.core.state import AppState


class SettingsDialog(QDialog):
    """@brief Modal settings dialog covering database, embeddings, and classifier.

    Edits are stored as flat key-value overrides in AppState, which the
    IngestionEngine and MultiRepoWatcher apply at runtime via config_overrides.
    The underlying codebrain.toml file is never modified.

    Emits settings_saved when the user accepts the dialog.
    """

    settings_saved = Signal()

    def __init__(
        self,
        state: AppState,
        current_config: Optional[dict] = None,
        parent=None,
    ) -> None:
        """@brief Construct the settings dialog.

        @param state AppState instance for persisting overrides.
        @param current_config Current effective config dict (for pre-population).
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self._state = state
        self._cfg = current_config or {}
        self.setWindowTitle("CodeBrain Settings")
        self.setMinimumWidth(520)
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """@brief Construct all form widgets and layouts."""
        root_layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(16)

        layout.addWidget(self._build_database_group())
        layout.addWidget(self._build_embeddings_group())
        layout.addWidget(self._build_classifier_group())
        layout.addWidget(self._build_ingestion_group())
        layout.addStretch()

        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        note = QLabel(
            "<i>Changes are stored as local overrides and applied on the next "
            "ingestion or watcher start. The codebrain.toml file is not modified.</i>"
        )
        note.setWordWrap(True)
        root_layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    def _build_database_group(self) -> QGroupBox:
        """@brief Build the Database configuration group box.

        @return Populated QGroupBox.
        """
        group = QGroupBox("Database")
        form = QFormLayout(group)
        self._db_url = QLineEdit()
        self._db_url.setPlaceholderText("postgresql://user:pass@host:port/dbname")
        form.addRow("Connection URL:", self._db_url)
        return group

    def _build_embeddings_group(self) -> QGroupBox:
        """@brief Build the Embeddings configuration group box.

        @return Populated QGroupBox.
        """
        group = QGroupBox("Embeddings")
        form = QFormLayout(group)

        self._emb_model = QLineEdit()
        self._emb_model.setPlaceholderText("nomic-embed-text")
        form.addRow("Model:", self._emb_model)

        self._emb_dimensions = QSpinBox()
        self._emb_dimensions.setRange(64, 8192)
        self._emb_dimensions.setSingleStep(64)
        form.addRow("Dimensions:", self._emb_dimensions)

        self._emb_api_style = QComboBox()
        self._emb_api_style.addItems(["ollama", "openai"])
        form.addRow("API Style:", self._emb_api_style)

        self._emb_base_url = QLineEdit()
        self._emb_base_url.setPlaceholderText("http://127.0.0.1:11434")
        form.addRow("Base URL:", self._emb_base_url)

        self._emb_api_key = QLineEdit()
        self._emb_api_key.setPlaceholderText("(optional, for OpenAI-compatible APIs)")
        self._emb_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key:", self._emb_api_key)

        return group

    def _build_classifier_group(self) -> QGroupBox:
        """@brief Build the Classifier configuration group box.

        @return Populated QGroupBox.
        """
        group = QGroupBox("Classifier")
        form = QFormLayout(group)

        self._cls_model = QLineEdit()
        self._cls_model.setPlaceholderText("mistralai/devstral-medium-2507")
        form.addRow("Model:", self._cls_model)

        self._cls_base_url = QLineEdit()
        self._cls_base_url.setPlaceholderText("http://127.0.0.1:3000")
        form.addRow("Base URL:", self._cls_base_url)

        return group

    def _build_ingestion_group(self) -> QGroupBox:
        """@brief Build the Ingestion configuration group box.

        @return Populated QGroupBox.
        """
        group = QGroupBox("Ingestion")
        form = QFormLayout(group)

        self._workers = QSpinBox()
        self._workers.setRange(1, 64)
        form.addRow("Worker threads:", self._workers)

        self._chunk_size = QSpinBox()
        self._chunk_size.setRange(64, 4096)
        self._chunk_size.setSingleStep(64)
        form.addRow("Chunk size (words):", self._chunk_size)

        self._overlap = QSpinBox()
        self._overlap.setRange(0, 512)
        self._overlap.setSingleStep(16)
        form.addRow("Overlap (words):", self._overlap)

        return group

    # ------------------------------------------------------------------
    # Value loading and saving
    # ------------------------------------------------------------------

    def _load_values(self) -> None:
        """@brief Pre-populate form fields from the effective config and overrides."""
        db = self._cfg.get("database", {})
        emb = self._cfg.get("embeddings", {})
        cls = self._cfg.get("classifier", {})
        ing = self._cfg.get("ingestion", {})

        # Database
        self._db_url.setText(self._override("database.url", db.get("url", "")))

        # Embeddings
        self._emb_model.setText(
            self._override("embeddings.model", emb.get("model", ""))
        )
        self._emb_dimensions.setValue(
            int(self._override("embeddings.dimensions", str(emb.get("dimensions", 768))))
        )
        api_style = self._override("embeddings.api_style", emb.get("api_style", "ollama"))
        idx = self._emb_api_style.findText(api_style)
        if idx >= 0:
            self._emb_api_style.setCurrentIndex(idx)
        self._emb_base_url.setText(
            self._override("embeddings.base_url", emb.get("base_url", ""))
        )
        self._emb_api_key.setText(
            self._override("embeddings.api_key", emb.get("api_key", ""))
        )

        # Classifier
        self._cls_model.setText(
            self._override("classifier.model", cls.get("model", ""))
        )
        self._cls_base_url.setText(
            self._override("classifier.base_url", cls.get("base_url", ""))
        )

        # Ingestion
        self._workers.setValue(
            int(self._override("ingestion.workers", str(ing.get("workers", 4))))
        )
        self._chunk_size.setValue(
            int(self._override("ingestion.chunk_size", str(ing.get("chunk_size", 512))))
        )
        self._overlap.setValue(
            int(self._override("ingestion.overlap", str(ing.get("overlap", 64))))
        )

    def _override(self, key: str, default: str) -> str:
        """@brief Return the user override for key, or default.

        @param key Dot-notation setting key.
        @param default Value to return when no override exists.
        @return String value.
        """
        saved = self._state.get_setting(key)
        return saved if saved else str(default)

    def _save(self) -> None:
        """@brief Persist all form values to AppState and close the dialog."""
        self._state.set_setting("database.url", self._db_url.text().strip())
        self._state.set_setting("embeddings.model", self._emb_model.text().strip())
        self._state.set_setting(
            "embeddings.dimensions", str(self._emb_dimensions.value())
        )
        self._state.set_setting(
            "embeddings.api_style", self._emb_api_style.currentText()
        )
        self._state.set_setting("embeddings.base_url", self._emb_base_url.text().strip())
        api_key = self._emb_api_key.text().strip()
        if api_key:
            self._state.set_setting("embeddings.api_key", api_key)
        else:
            self._state.delete_setting("embeddings.api_key")

        self._state.set_setting("classifier.model", self._cls_model.text().strip())
        self._state.set_setting("classifier.base_url", self._cls_base_url.text().strip())
        self._state.set_setting("ingestion.workers", str(self._workers.value()))
        self._state.set_setting("ingestion.chunk_size", str(self._chunk_size.value()))
        self._state.set_setting("ingestion.overlap", str(self._overlap.value()))

        self.settings_saved.emit()
        self.accept()
