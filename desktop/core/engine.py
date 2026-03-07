"""
@file engine.py
@brief Bridge between the GUI and the existing CodeBrain ingestion pipeline.

IngestionEngine manages per-repository ingestion workers (QThread-backed) and
forwards Qt signals to the UI layer. IngestionWorker executes on a background
thread, reusing the existing process_file(), walk_repo(), and ensure_schema()
functions from ingest.py without modification.

Threading model:
- IngestionWorker runs on its own QThread.
- Internally it spawns a ThreadPoolExecutor (same as ingest.py).
- Qt signals emitted from worker threads are automatically queued to the
  main thread by Qt's cross-thread signal mechanism.
"""

import copy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector
from PySide6.QtCore import QObject, QThread, Signal

# Ensure project root is importable regardless of launch working directory.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chunker import ASTChunker  # noqa: E402
from classifier import IntentClassifier  # noqa: E402
from embedder import EmbeddingClient  # noqa: E402
from ingest import (  # noqa: E402
    ensure_schema,
    get_db,
    load_config,
    normalize_result_status,
    process_file,
    walk_repo,
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


class IngestionWorker(QObject):
    """@brief Background worker that runs full ingestion for one repository.

    Designed to be moved to a QThread. Internally reuses the exact same
    functions as ingest.py (process_file, walk_repo, ensure_schema).
    All progress is reported via Qt signals, which Qt routes safely to
    the UI thread via its queued connection mechanism.
    """

    repo_started = Signal(str, int)        # (repo_name, total_files)
    progress = Signal(str, int, int, dict) # (repo_name, current, total, result)
    file_processed = Signal(str, str, str) # (repo_name, rel_path, status_key)
    repo_completed = Signal(str, dict)     # (repo_name, final_stats)
    repo_error = Signal(str, str)          # (repo_name, error_message)

    def __init__(
        self,
        repo_path: str,
        config_path: str,
        config_overrides: dict,
        force: bool,
        no_classify: bool,
        workers_override: Optional[int],
    ) -> None:
        """@brief Construct the worker with all ingestion parameters.

        @param repo_path Absolute path to the repository root.
        @param config_path Path to codebrain.toml.
        @param config_overrides Nested dict of user setting overrides.
        @param force Re-index all files regardless of hash.
        @param no_classify Skip LLM classification (embed only).
        @param workers_override Override thread count; None uses config value.
        """
        super().__init__()
        self._repo_path = Path(repo_path)
        self._config_path = config_path
        self._config_overrides = config_overrides
        self._force = force
        self._no_classify = no_classify
        self._workers_override = workers_override
        self._cancelled = False

    def cancel(self) -> None:
        """@brief Request cooperative cancellation of the in-progress ingestion.

        In-flight file workers finish their current file; no new files are started.
        """
        self._cancelled = True

    def run(self) -> None:
        """@brief Entry point invoked when the hosting QThread starts.

        Calls _do_ingest() and catches any unhandled exception to emit repo_error.
        """
        repo_name = self._repo_path.name
        try:
            self._do_ingest(repo_name)
        except Exception as exc:
            self.repo_error.emit(repo_name, str(exc))

    def _effective_config(self) -> dict:
        """@brief Load codebrain.toml and apply user overrides.

        @return Effective merged configuration dictionary.
        """
        cfg = load_config(self._config_path)
        if self._config_overrides:
            cfg = _deep_merge(cfg, self._config_overrides)
        return cfg

    def _do_ingest(self, repo_name: str) -> None:
        """@brief Core ingestion logic executed on the background thread.

        Replicates the orchestration from ingest.py main() but emits Qt
        signals instead of updating a Rich progress bar.

        @param repo_name Repository name (directory basename) for DB records.
        """
        cfg = self._effective_config()
        repo_root = self._repo_path.resolve()
        n_workers = self._workers_override or cfg.get("ingestion", {}).get("workers", 4)

        # Shared HTTP clients — thread-safe (httpx.Client internals).
        embedder = EmbeddingClient(cfg)
        classifier = IntentClassifier(cfg)

        # Connection pool: one slot per worker plus a couple of spares.
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            1, n_workers + 2, cfg["database"]["url"]
        )

        # Apply pgvector to a setup connection, run schema migrations,
        # and create the ingestion_runs record.
        setup_conn = get_db(cfg)
        ensure_schema(setup_conn)
        cur = setup_conn.cursor()
        cur.execute(
            "INSERT INTO ingestion_runs (repo) VALUES (%s) RETURNING id",
            (repo_name,),
        )
        run_id = cur.fetchone()[0]
        setup_conn.commit()
        setup_conn.close()

        # Discover source files respecting excludes and .gitignore.
        files = walk_repo(repo_root, cfg)
        total = len(files)
        self.repo_started.emit(repo_name, total)

        if total == 0:
            self._finalize_run(cfg, run_id, 0, 0, 0, db_pool)
            self.repo_completed.emit(
                repo_name,
                {"indexed": 0, "skipped": 0, "errors": 0, "chunks": 0, "symbols": 0},
            )
            return

        stats = {
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "chunks": 0,
            "symbols": 0,
            "classifier_fallbacks": 0,
        }

        # ASTChunker is not thread-safe (tree-sitter Parser is stateful).
        # One instance is created per thread on demand.
        thread_chunkers: dict[int, ASTChunker] = {}

        def get_chunker() -> ASTChunker:
            tid = threading.get_ident()
            if tid not in thread_chunkers:
                thread_chunkers[tid] = ASTChunker(cfg)
            return thread_chunkers[tid]

        def process(fpath: Path) -> dict:
            return process_file(
                fpath,
                repo_root,
                repo_name,
                cfg,
                embedder,
                classifier,
                get_chunker(),
                db_pool,
                force=self._force,
                no_classify=self._no_classify,
            )

        current = 0
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(process, f): f for f in files}
            for future in as_completed(futures):
                if self._cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                try:
                    result = future.result()
                except Exception as exc:
                    fpath = futures[future]
                    try:
                        rel = str(fpath.relative_to(repo_root))
                    except ValueError:
                        rel = str(fpath)
                    result = {"status": "error", "path": rel, "error": str(exc), "warnings": []}

                status_key = normalize_result_status(result.get("status"))
                stats[status_key] += 1
                stats["chunks"] += result.get("chunks", 0)
                stats["symbols"] += result.get("symbols", 0)
                stats["classifier_fallbacks"] += len(result.get("warnings", []))
                current += 1

                # Qt queues these across threads automatically.
                self.progress.emit(repo_name, current, total, result)
                self.file_processed.emit(
                    repo_name, result.get("path", ""), status_key
                )

        files_processed = stats["indexed"] + stats["skipped"] + stats["errors"]
        self._finalize_run(cfg, run_id, files_processed, stats["chunks"], stats["symbols"], db_pool)
        self.repo_completed.emit(repo_name, stats)

    def _finalize_run(
        self,
        cfg: dict,
        run_id: int,
        files_processed: int,
        chunks: int,
        symbols: int,
        db_pool: psycopg2.pool.ThreadedConnectionPool,
    ) -> None:
        """@brief Update the ingestion_runs record and close the connection pool.

        @param cfg Effective configuration dict.
        @param run_id ID of the ingestion_runs row to update.
        @param files_processed Total files processed (indexed + skipped + errors).
        @param chunks Total chunks created.
        @param symbols Total symbols extracted.
        @param db_pool Connection pool to close after finalization.
        """
        try:
            finish_conn = get_db(cfg)
            cur = finish_conn.cursor()
            cur.execute(
                """UPDATE ingestion_runs
                   SET completed_at = NOW(), files_processed = %s,
                       chunks_created = %s, symbols_found = %s, status = 'completed'
                   WHERE id = %s""",
                (files_processed, chunks, symbols, run_id),
            )
            finish_conn.commit()
            finish_conn.close()
        except Exception:
            pass
        finally:
            try:
                db_pool.closeall()
            except Exception:
                pass


class IngestionEngine(QObject):
    """@brief Manages per-repository ingestion workers and forwards signals to the UI.

    Each call to start_ingestion() creates a new IngestionWorker and moves it
    to a dedicated QThread. The engine tracks active runs and allows cancellation.
    All signals are re-emitted from the engine so UI components only need to
    connect to a single engine instance.

    Threading / lifecycle notes:
    - Workers emit signals from Thread N; lambdas with no QObject affinity are
      direct connections and run on Thread N.
    - _cleanup must run on the main thread so we route it via an internal signal.
    - thread.finished fires after QThread::run() returns; connecting deleteLater()
      there ensures the C++ objects outlive the thread function.
    """

    # Forwarded from active IngestionWorker instances.
    repo_started = Signal(str, int)        # (repo_name, total_files)
    progress = Signal(str, int, int, dict) # (repo_name, current, total, result)
    file_processed = Signal(str, str, str) # (repo_name, path, status_key)
    repo_completed = Signal(str, dict)     # (repo_name, stats)
    repo_error = Signal(str, str)          # (repo_name, error_message)

    # Synthesis signals
    synthesis_started = Signal(str)        # (repo_name)
    synthesis_completed = Signal(str, str) # (repo_name, message)
    synthesis_error = Signal(str, str)     # (repo_name, error_message)

    # Internal: routes _cleanup() back to the main thread via Qt queued delivery.
    _cleanup_requested = Signal(str)       # (repo_name)

    def __init__(self, config_path: str = _DEFAULT_CONFIG) -> None:
        """@brief Construct the engine.

        @param config_path Absolute path to codebrain.toml.
        """
        super().__init__()
        self._config_path = config_path
        self._config_overrides: dict = {}
        # Maps repo_name -> (IngestionWorker, QThread)
        self._active: dict[str, tuple[IngestionWorker, QThread]] = {}
        # Queued connection: emitted from Thread N, delivered on Thread 0.
        self._cleanup_requested.connect(self._cleanup)

    def set_config_overrides(self, overrides: dict) -> None:
        """@brief Replace user setting overrides applied on top of codebrain.toml.

        @param overrides Nested dict of config overrides (from AppState).
        """
        self._config_overrides = overrides

    def start_ingestion(
        self,
        repo_path: str,
        force: bool = False,
        no_classify: bool = False,
        workers: Optional[int] = None,
    ) -> bool:
        """@brief Launch ingestion for one repository on a background QThread.

        No-ops if an ingestion for this repo is already running.

        @param repo_path Absolute path to the repository root.
        @param force Re-index regardless of file hashes.
        @param no_classify Skip LLM classification.
        @param workers Override thread pool size; None uses config value.
        @return True if a new ingestion was started, False if already running.
        """
        repo_name = Path(repo_path).name
        if repo_name in self._active:
            return False

        worker = IngestionWorker(
            repo_path=repo_path,
            config_path=self._config_path,
            config_overrides=copy.deepcopy(self._config_overrides),
            force=force,
            no_classify=no_classify,
            workers_override=workers,
        )
        thread = QThread()
        worker.moveToThread(thread)

        # Forward all worker signals through the engine.
        worker.repo_started.connect(self.repo_started)
        worker.progress.connect(self.progress)
        worker.file_processed.connect(self.file_processed)
        worker.repo_completed.connect(self.repo_completed)
        worker.repo_error.connect(self.repo_error)

        # Ask the thread's event loop to exit once the run finishes.
        # These lambdas are direct connections (no QObject affinity) and run on
        # Thread N, which is safe because QThread::quit() is thread-safe.
        # The lambdas also keep `thread` alive via closure until they are torn down.
        worker.repo_completed.connect(lambda _name, _stats: thread.quit())
        worker.repo_error.connect(lambda _name, _msg: thread.quit())

        # After the event loop exits and the thread function returns, clean up.
        # thread.finished fires on Thread N after QThread::run() returns.
        # deleteLater() schedules C++ destruction via each object's owning thread.
        # _cleanup_requested is queued to Thread 0 (engine's thread) so _cleanup()
        # never runs on Thread N, preventing concurrent access to _active.
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._cleanup_requested.emit(repo_name))

        thread.started.connect(worker.run)
        self._active[repo_name] = (worker, thread)
        thread.start()
        return True

    def cancel_ingestion(self, repo_name: str) -> None:
        """@brief Request cancellation of an in-progress ingestion run.

        @param repo_name Name of the repository whose ingestion to cancel.
        """
        entry = self._active.get(repo_name)
        if entry:
            entry[0].cancel()

    def is_running(self, repo_name: str) -> bool:
        """@brief Check whether ingestion is currently active for a repo.

        @param repo_name Repository name to query.
        @return True if an ingestion worker is active.
        """
        return repo_name in self._active

    def stop_all(self) -> None:
        """@brief Cancel all active ingestion runs."""
        for name in list(self._active.keys()):
            self.cancel_ingestion(name)

    def get_repo_stats(self, repo_name: str) -> Optional[dict]:
        """@brief Query PostgreSQL for aggregate stats for a repository.

        Runs synchronously — call from the main thread only for small datasets.

        @param repo_name Repository name as stored in the files table.
        @return Dict with file_count, chunk_count, symbol_count, languages list,
                or None if the DB is unreachable or the repo has no records.
        """
        try:
            cfg = load_config(self._config_path)
            cfg = _deep_merge(cfg, self._config_overrides)
            conn = psycopg2.connect(cfg["database"]["url"])
            register_vector(conn)
            cur = conn.cursor()
            cur.execute(
                """SELECT
                       COUNT(DISTINCT f.id)  AS file_count,
                       COUNT(DISTINCT cc.id) AS chunk_count,
                       COUNT(DISTINCT s.id)  AS symbol_count
                   FROM files f
                   LEFT JOIN code_chunks cc ON cc.file_id = f.id
                   LEFT JOIN symbols s ON s.file_id = f.id
                   WHERE f.repo = %s""",
                (repo_name,),
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return None

            file_count, chunk_count, symbol_count = row

            cur.execute(
                """SELECT language, COUNT(*) AS cnt
                   FROM files
                   WHERE repo = %s AND language IS NOT NULL
                   GROUP BY language
                   ORDER BY cnt DESC""",
                (repo_name,),
            )
            languages = [{"language": r[0], "count": r[1]} for r in cur.fetchall()]
            conn.close()
            return {
                "file_count": file_count,
                "chunk_count": chunk_count,
                "symbol_count": symbol_count,
                "languages": languages,
            }
        except Exception:
            return None

    def get_ingestion_history(
        self, repo_name: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """@brief Query the ingestion_runs table for past runs.

        @param repo_name Filter to a specific repo; None returns all repos.
        @param limit Maximum number of rows to return.
        @return List of run dicts ordered by started_at descending.
        """
        try:
            cfg = load_config(self._config_path)
            cfg = _deep_merge(cfg, self._config_overrides)
            conn = psycopg2.connect(cfg["database"]["url"])
            cur = conn.cursor()
            if repo_name:
                cur.execute(
                    """SELECT repo, started_at, completed_at, files_processed,
                              chunks_created, symbols_found, status
                       FROM ingestion_runs
                       WHERE repo = %s
                       ORDER BY started_at DESC
                       LIMIT %s""",
                    (repo_name, limit),
                )
            else:
                cur.execute(
                    """SELECT repo, started_at, completed_at, files_processed,
                              chunks_created, symbols_found, status
                       FROM ingestion_runs
                       ORDER BY started_at DESC
                       LIMIT %s""",
                    (limit,),
                )
            cols = ["repo", "started_at", "completed_at", "files_processed",
                    "chunks_created", "symbols_found", "status"]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.close()
            return rows
        except Exception:
            return []

    def get_module_intents(self, repo_name: str) -> list[dict]:
        """@brief Query the module_intents table.

        @param repo_name Filter to a specific repo.
        @return List of module dicts.
        """
        try:
            cfg = load_config(self._config_path)
            cfg = _deep_merge(cfg, self._config_overrides)
            conn = psycopg2.connect(cfg["database"]["url"])
            cur = conn.cursor()
            cur.execute(
                """SELECT module_path, kind, module_name, summary, role, dominant_intent, file_count, chunk_count
                   FROM module_intents
                   WHERE repo = %s
                   ORDER BY kind, module_path""",
                (repo_name,)
            )
            cols = ["module_path", "kind", "module_name", "summary", "role", "dominant_intent", "file_count", "chunk_count"]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.close()
            return rows
        except Exception:
            return []

    def start_synthesis(self, repo_name: str) -> None:
        """@brief Launch module synthesis for one repository on a background thread.

        @param repo_name Repository name to synthesize.
        """
        self.synthesis_started.emit(repo_name)

        def run_synthesis():
            import subprocess
            try:
                # The python script itself handles UPSERT and will clear/overwrite automatically
                result = subprocess.run(
                    [sys.executable, "synthesize_modules.py", "--repo", repo_name, "--mode", "all"],
                    cwd=str(_ROOT),
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    self.synthesis_completed.emit(repo_name, "Synthesis complete")
                else:
                    self.synthesis_error.emit(repo_name, result.stderr.strip() or "Unknown error")
            except Exception as e:
                self.synthesis_error.emit(repo_name, str(e))

        threading.Thread(target=run_synthesis, daemon=True).start()

    def _cleanup(self, repo_name: str) -> None:
        """@brief Remove the active entry for a completed or errored ingestion run.

        Called on the main thread via the queued _cleanup_requested signal, so it
        is safe to mutate _active. The QThread and worker are already scheduled for
        deletion via deleteLater() before this slot runs; no explicit quit/wait needed.

        @param repo_name Repository name whose entry to remove.
        """
        self._active.pop(repo_name, None)
