"""
@file watcher.py
@brief Multi-repository filesystem watcher using watchdog.

MultiRepoWatcher extends the single-repo watchdog pattern from ingest.py
to support N concurrent repositories. Each watched repo gets its own
watchdog Observer for clean lifecycle management and crash isolation.

Resource sharing strategy:
- EmbeddingClient and IntentClassifier: one shared instance (thread-safe).
- ASTChunker: one per watched repo (tree-sitter Parser is not thread-safe).
- ThreadedConnectionPool: one per watched repo (2 connections each).
"""

import sys
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector
from PySide6.QtCore import QObject, Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chunker import ASTChunker  # noqa: E402
from classifier import IntentClassifier  # noqa: E402
from embedder import EmbeddingClient  # noqa: E402
from ingest import (  # noqa: E402
    detect_language,
    is_gitignored,
    load_config,
    process_file,
    should_exclude,
)

_DEFAULT_CONFIG = str(_ROOT / "codebrain.toml")


def _deep_merge(base: dict, override: dict) -> dict:
    """@brief Recursively merge override into base, returning a new dict.

    @param base Base configuration dictionary.
    @param override Dictionary whose values take precedence.
    @return Merged dictionary (new object, base is not modified).
    """
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


class _ReindexHandler(FileSystemEventHandler):
    """@brief Watchdog event handler that re-indexes modified/created files
    and prunes deleted files/directories in real-time.
    """

    def __init__(
        self,
        repo_root: Path,
        repo_name: str,
        cfg: dict,
        embedder: EmbeddingClient,
        classifier: IntentClassifier,
        chunker: ASTChunker,
        db_pool: psycopg2.pool.ThreadedConnectionPool,
        on_reindexed,
        on_error,
    ) -> None:
        super().__init__()
        self._repo_root = repo_root
        self._repo_name = repo_name
        self._cfg = cfg
        self._embedder = embedder
        self._classifier = classifier
        self._chunker = chunker
        self._db_pool = db_pool
        self._on_reindexed = on_reindexed
        self._on_error = on_error
        self._excludes = cfg.get("ingestion", {}).get("exclude", [])

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_deleted(self, event) -> None:
        fpath = Path(event.src_path)
        if (
            should_exclude(fpath, self._repo_root, self._excludes)
            or is_gitignored(fpath, self._repo_root)
        ):
            return

        try:
            rel_path = str(fpath.relative_to(self._repo_root))
        except ValueError:
            return

        conn = self._db_pool.getconn()
        try:
            cur = conn.cursor()
            if event.is_directory:
                cur.execute(
                    "DELETE FROM files WHERE repo = %s AND path LIKE %s",
                    (self._repo_name, f"{rel_path}/%")
                )
            else:
                cur.execute(
                    "DELETE FROM files WHERE repo = %s AND path = %s",
                    (self._repo_name, rel_path)
                )
            conn.commit()
            self._on_reindexed(rel_path, "deleted")
        except Exception as exc:
            self._on_error(str(exc))
        finally:
            self._db_pool.putconn(conn)

    def on_moved(self, event) -> None:
        # Remove old path
        src_path = Path(event.src_path)
        if not (
            should_exclude(src_path, self._repo_root, self._excludes)
            or is_gitignored(src_path, self._repo_root)
        ):
            try:
                rel_src_path = str(src_path.relative_to(self._repo_root))
                conn = self._db_pool.getconn()
                try:
                    cur = conn.cursor()
                    if event.is_directory:
                        cur.execute(
                            "DELETE FROM files WHERE repo = %s AND (path = %s OR path LIKE %s)",
                            (self._repo_name, rel_src_path, f"{rel_src_path}/%")
                        )
                    else:
                        cur.execute(
                            "DELETE FROM files WHERE repo = %s AND path = %s",
                            (self._repo_name, rel_src_path)
                        )
                    conn.commit()
                except Exception as exc:
                    self._on_error(str(exc))
                finally:
                    self._db_pool.putconn(conn)
            except ValueError:
                pass

        if event.is_directory:
            # Re-index all files in new directory
            import os
            new_dir = Path(event.dest_path)
            for root, _, filenames in os.walk(new_dir):
                for fname in filenames:
                    self._handle(Path(root) / fname)
        else:
            self._handle(Path(event.dest_path))

    def _handle(self, fpath: Path) -> None:
        """@brief Filter and re-index one file."""
        try:
            if should_exclude(fpath, self._repo_root, self._excludes):
                return
            if is_gitignored(fpath, self._repo_root):
                return
            lang = detect_language(fpath, self._cfg)
            if not lang:
                return

            result = process_file(
                fpath,
                self._repo_root,
                self._repo_name,
                self._cfg,
                self._embedder,
                self._classifier,
                self._chunker,
                self._db_pool,
            )
            try:
                rel = str(fpath.relative_to(self._repo_root))
            except ValueError:
                rel = str(fpath)
            self._on_reindexed(rel, result.get("status", "error"))
            if result.get("error"):
                self._on_error(f"{rel}: {result['error']}")
            for warning in result.get("warnings", []):
                self._on_error(f"{rel}: {warning}")
        except Exception as exc:
            self._on_error(str(exc))


class _WatchedRepo:
    """@brief Runtime state bundle for one watched repository."""

    def __init__(
        self,
        repo_path: Path,
        repo_name: str,
        observer: Observer,
        db_pool: psycopg2.pool.ThreadedConnectionPool,
        chunker: ASTChunker,
    ) -> None:
        """@brief Bundle all per-repo resources.

        @param repo_path Absolute repository root.
        @param repo_name Repository name.
        @param observer watchdog Observer for this repo.
        @param db_pool Dedicated connection pool (2 connections).
        @param chunker Dedicated ASTChunker instance.
        """
        self.repo_path = repo_path
        self.repo_name = repo_name
        self.observer = observer
        self.db_pool = db_pool
        self.chunker = chunker


class MultiRepoWatcher(QObject):
    """@brief Manage concurrent watchdog Observers for N repositories.

    Each watched repo gets its own Observer + handler pair so that
    stopping one repo's watcher does not affect others. The shared
    EmbeddingClient and IntentClassifier are re-used across repos because
    their httpx.Client internals are thread-safe.
    """

    file_changed = Signal(str, str, str)  # (repo_name, rel_path, status)
    watch_error = Signal(str, str)        # (repo_name, error_message)
    watch_started = Signal(str)           # (repo_name,)
    watch_stopped = Signal(str)           # (repo_name,)

    def __init__(
        self,
        config_path: str = _DEFAULT_CONFIG,
        config_overrides: Optional[dict] = None,
    ) -> None:
        """@brief Construct the watcher.

        @param config_path Absolute path to codebrain.toml.
        @param config_overrides Nested dict of user setting overrides.
        """
        super().__init__()
        self._config_path = config_path
        self._config_overrides = config_overrides or {}
        self._watched: dict[str, _WatchedRepo] = {}

        # Shared clients — created lazily on first watch to allow config
        # overrides to be applied before any network calls are made.
        self._embedder: Optional[EmbeddingClient] = None
        self._classifier: Optional[IntentClassifier] = None

    def set_config_overrides(self, overrides: dict) -> None:
        """@brief Update config overrides and reset shared clients so they
        pick up any changed endpoints on next use.

        @param overrides Nested dict of config overrides.
        """
        self._config_overrides = overrides
        self._embedder = None
        self._classifier = None

    def start_watching(self, repo_path: str) -> bool:
        """@brief Begin watching a repository for file changes.

        No-ops if the repo is already being watched.

        @param repo_path Absolute path to the repository root.
        @return True if a new watcher was started, False if already watching.
        """
        root = Path(repo_path).resolve()
        repo_name = root.name

        if repo_name in self._watched:
            return False

        try:
            cfg = self._load_cfg()

            # Initialise shared clients on first use.
            if self._embedder is None:
                self._embedder = EmbeddingClient(cfg)
            if self._classifier is None:
                self._classifier = IntentClassifier(cfg)

            db_pool = psycopg2.pool.ThreadedConnectionPool(
                1, 2, cfg["database"]["url"]
            )
            chunker = ASTChunker(cfg)

            def on_reindexed(rel_path: str, status: str) -> None:
                self.file_changed.emit(repo_name, rel_path, status)

            def on_error(error: str) -> None:
                self.watch_error.emit(repo_name, error)

            handler = _ReindexHandler(
                repo_root=root,
                repo_name=repo_name,
                cfg=cfg,
                embedder=self._embedder,
                classifier=self._classifier,
                chunker=chunker,
                db_pool=db_pool,
                on_reindexed=on_reindexed,
                on_error=on_error,
            )

            observer = Observer()
            observer.schedule(handler, str(root), recursive=True)
            observer.start()

            self._watched[repo_name] = _WatchedRepo(
                repo_path=root,
                repo_name=repo_name,
                observer=observer,
                db_pool=db_pool,
                chunker=chunker,
            )
            self.watch_started.emit(repo_name)
            return True

        except Exception as exc:
            self.watch_error.emit(repo_name, str(exc))
            return False

    def stop_watching(self, repo_name: str) -> None:
        """@brief Stop watching a repository and release its resources.

        @param repo_name Name of the repo to stop watching.
        """
        entry = self._watched.pop(repo_name, None)
        if entry:
            self._teardown_entry(entry)
            self.watch_stopped.emit(repo_name)

    def stop_all(self) -> None:
        """@brief Stop all active watchers. Safe to call on app shutdown."""
        for name in list(self._watched.keys()):
            self.stop_watching(name)

    def is_watching(self, repo_name: str) -> bool:
        """@brief Check if a repo is currently being watched.

        @param repo_name Repository name to query.
        @return True if an active observer exists for this repo.
        """
        return repo_name in self._watched

    def watched_repos(self) -> list[str]:
        """@brief Return names of all currently watched repositories.

        @return List of repo name strings.
        """
        return list(self._watched.keys())

    def _load_cfg(self) -> dict:
        """@brief Load codebrain.toml and apply overrides.

        @return Effective merged configuration dict.
        """
        cfg = load_config(self._config_path)
        if self._config_overrides:
            cfg = _deep_merge(cfg, self._config_overrides)
        return cfg

    @staticmethod
    def _teardown_entry(entry: _WatchedRepo) -> None:
        """@brief Stop the observer and close the connection pool.

        @param entry The _WatchedRepo bundle to tear down.
        """
        try:
            entry.observer.stop()
            entry.observer.join(timeout=5)
        except Exception:
            pass
        try:
            entry.db_pool.closeall()
        except Exception:
            pass
