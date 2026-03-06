"""
@file state.py
@brief Persistent desktop application state stored in a local SQLite database.

Tracks registered repositories, their watch preferences, last ingestion results,
and user setting overrides. Stored in the platform-appropriate user data directory
so it survives app restarts without requiring PostgreSQL to be reachable.

Platform paths:
- macOS:   ~/Library/Application Support/CodeBrain/desktop_state.db
- Linux:   ~/.local/share/CodeBrain/desktop_state.db
- Windows: %LOCALAPPDATA%/CodeBrain/CodeBrain/desktop_state.db
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_data_dir

_APP_NAME = "CodeBrain"
_APP_AUTHOR = "CodeBrain"
_STATE_DIR = Path(user_data_dir(_APP_NAME, _APP_AUTHOR))
_STATE_DB = _STATE_DIR / "desktop_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    auto_watch  INTEGER NOT NULL DEFAULT 0,
    last_ingestion_at     TEXT,
    last_ingestion_status TEXT,
    last_ingestion_stats  TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AppState:
    """@brief Persistent desktop app state over a local SQLite database.

    All methods are synchronous and safe to call from the main (UI) thread.
    Heavy pipeline state (indexed files, embeddings, etc.) lives in PostgreSQL;
    this class only stores UI-level preferences and repo metadata.
    """

    def __init__(self, db_path: Path = _STATE_DB) -> None:
        """@brief Open or create the state database.

        @param db_path Absolute path to the SQLite database file.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Repository management
    # ------------------------------------------------------------------

    def add_repo(self, path: str) -> dict:
        """@brief Register a repository for management.

        @param path Absolute path to the repository root.
        @return Row dict for the newly added (or existing) repo.
        """
        resolved = str(Path(path).resolve())
        name = Path(resolved).name
        self._conn.execute(
            "INSERT OR IGNORE INTO repos (path, name) VALUES (?, ?)",
            (resolved, name),
        )
        self._conn.commit()
        return self.get_repo(resolved)

    def remove_repo(self, path: str) -> None:
        """@brief Unregister a repository.

        @param path Absolute path previously registered with add_repo().
        """
        resolved = str(Path(path).resolve())
        self._conn.execute("DELETE FROM repos WHERE path = ?", (resolved,))
        self._conn.commit()

    def list_repos(self) -> list[dict]:
        """@brief Return all registered repositories with their metadata.

        @return List of repo row dicts ordered by name.
        """
        rows = self._conn.execute(
            "SELECT * FROM repos ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_repo(self, path: str) -> Optional[dict]:
        """@brief Fetch a single repo by its absolute path.

        @param path Absolute path to the repository root.
        @return Row dict or None if not registered.
        """
        resolved = str(Path(path).resolve())
        row = self._conn.execute(
            "SELECT * FROM repos WHERE path = ?", (resolved,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def set_auto_watch(self, path: str, enabled: bool) -> None:
        """@brief Toggle the auto-watch flag for a repository.

        When True, the watcher starts automatically on app launch.

        @param path Absolute path to the repository root.
        @param enabled Whether to auto-watch on next launch.
        """
        resolved = str(Path(path).resolve())
        self._conn.execute(
            "UPDATE repos SET auto_watch = ? WHERE path = ?",
            (1 if enabled else 0, resolved),
        )
        self._conn.commit()

    def update_ingestion_result(
        self, path: str, status: str, stats: dict, at: Optional[str] = None
    ) -> None:
        """@brief Record the result of the latest ingestion run.

        @param path Absolute path to the repository root.
        @param status Human-readable status string (e.g. 'completed', 'error').
        @param stats Dict with keys indexed, skipped, errors, chunks, symbols.
        @param at ISO-format timestamp string; defaults to current UTC time.
        """
        resolved = str(Path(path).resolve())
        timestamp = at if at else datetime.utcnow().isoformat()
        self._conn.execute(
            """UPDATE repos
               SET last_ingestion_at = ?, last_ingestion_status = ?,
                   last_ingestion_stats = ?
               WHERE path = ?""",
            (timestamp, status, json.dumps(stats), resolved),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Settings overrides
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        """@brief Read a persisted setting value.

        Keys use dot-notation matching config sections, e.g. 'database.url'.

        @param key Dot-notation setting key.
        @param default Value to return when the key is absent.
        @return Setting value string, or default.
        """
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """@brief Persist a setting override.

        @param key Dot-notation setting key.
        @param value String value to store.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def delete_setting(self, key: str) -> None:
        """@brief Remove a setting override, reverting to the TOML default.

        @param key Dot-notation setting key to remove.
        """
        self._conn.execute(
            "DELETE FROM app_settings WHERE key = ?", (key,)
        )
        self._conn.commit()

    def all_settings(self) -> dict[str, str]:
        """@brief Return all persisted setting overrides as a flat dict.

        @return Dict mapping dot-notation keys to string values.
        """
        rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {row[0]: row[1] for row in rows}

    def build_config_overrides(self) -> dict[str, Any]:
        """@brief Convert flat setting overrides into a nested config dict.

        Each dot-notation key like 'embeddings.model' becomes a nested
        dict that can be deep-merged on top of the base codebrain.toml config.

        @return Nested dict of overrides suitable for deep-merging.
        """
        result: dict = {}
        for key, raw_value in self.all_settings().items():
            # Try to parse as int or float for numeric fields
            value: Any = raw_value
            try:
                value = int(raw_value)
            except ValueError:
                try:
                    value = float(raw_value)
                except ValueError:
                    pass

            parts = key.split(".", 1)
            if len(parts) == 2:
                section, field = parts
                result.setdefault(section, {})[field] = value
            else:
                result[key] = value
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        """@brief Convert a sqlite3.Row to a plain dict.

        @param row SQLite row object.
        @return Plain dict.
        """
        d = dict(row)
        if d.get("last_ingestion_stats"):
            try:
                d["last_ingestion_stats"] = json.loads(d["last_ingestion_stats"])
            except (json.JSONDecodeError, TypeError):
                d["last_ingestion_stats"] = {}
        d["auto_watch"] = bool(d.get("auto_watch", 0))
        return d

    def close(self) -> None:
        """@brief Close the database connection."""
        self._conn.close()
