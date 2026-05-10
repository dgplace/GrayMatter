"""
@file runtime.py
@brief Ingestion runtime orchestration, watch-mode, and summary helpers.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import psycopg2.pool
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import resolver
from codebrain.chunker import ASTChunker
from codebrain.classifier import IntentClassifier
from codebrain.embedder import EmbeddingClient

console = Console()


def walk_repo(
    repo_root: Path,
    config: dict,
    should_exclude_fn: Callable[[Path, Path, list[str]], bool],
    filter_gitignored_paths_fn: Callable[[list[Path], Path], list[Path]],
) -> list[Path]:
    """@brief Walk the repository, respecting excludes and .gitignore."""
    excludes = config.get("ingestion", {}).get("exclude", [])
    supported_exts = set()
    for ext in config.get("languages", {}).get("extensions", {}).keys():
        supported_exts.add(f".{ext}")

    files = []
    for root, dirs, filenames in os.walk(repo_root):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not should_exclude_fn(root_path / d, repo_root, excludes)
        ]
        for fname in filenames:
            fpath = root_path / fname
            if fpath.suffix in supported_exts and not should_exclude_fn(fpath, repo_root, excludes):
                files.append(fpath)
    return filter_gitignored_paths_fn(files, repo_root)


def normalize_result_status(status: Optional[str]) -> str:
    """@brief Normalize per-file status to a summary counter key."""
    if status in {"indexed", "skipped", "deleted"}:
        return status
    return "errors"


class ReindexHandler(FileSystemEventHandler):
    """@brief Watchdog handler to re-index files on creation, modification, or deletion."""

    def __init__(
        self,
        repo_root: Path,
        repo_name: str,
        config: dict,
        embedder: EmbeddingClient,
        classifier: IntentClassifier,
        chunker: ASTChunker,
        db_pool: psycopg2.pool.ThreadedConnectionPool,
        process_file_fn: Callable[..., dict],
        should_exclude_fn: Callable[[Path, Path, list[str]], bool],
        is_gitignored_fn: Callable[[Path, Path], bool],
        detect_language_fn: Callable[[Path, dict], Optional[str]],
        no_classify: bool = False,
    ):
        self.repo_root = repo_root
        self.repo_name = repo_name
        self.config = config
        self.embedder = embedder
        self.classifier = classifier
        self.chunker = chunker
        self.db_pool = db_pool
        self.process_file_fn = process_file_fn
        self.should_exclude_fn = should_exclude_fn
        self.is_gitignored_fn = is_gitignored_fn
        self.detect_language_fn = detect_language_fn
        self.no_classify = no_classify

    def on_created(self, event):
        if not event.is_directory:
            self._handle_change(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._handle_change(Path(event.src_path))

    def on_deleted(self, event):
        fpath = Path(event.src_path)
        if (
            self.should_exclude_fn(fpath, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            or self.is_gitignored_fn(fpath, self.repo_root)
        ):
            return

        try:
            rel_path = str(fpath.relative_to(self.repo_root))
        except ValueError:
            return

        conn = self.db_pool.getconn()
        try:
            cur = conn.cursor()
            if event.is_directory:
                console.print(f"  [dim]Removing directory {rel_path} from index...[/]")
                cur.execute(
                    "DELETE FROM files WHERE repo = %s AND path LIKE %s",
                    (self.repo_name, f"{rel_path}/%")
                )
            else:
                console.print(f"  [dim]Removing {rel_path} from index...[/]")
                cur.execute(
                    "DELETE FROM files WHERE repo = %s AND path = %s",
                    (self.repo_name, rel_path)
                )
            conn.commit()
        finally:
            self.db_pool.putconn(conn)

    def on_moved(self, event):
        src_path = Path(event.src_path)
        if not (
            self.should_exclude_fn(src_path, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            or self.is_gitignored_fn(src_path, self.repo_root)
        ):
            try:
                rel_src_path = str(src_path.relative_to(self.repo_root))
                conn = self.db_pool.getconn()
                try:
                    cur = conn.cursor()
                    if event.is_directory:
                        cur.execute(
                            "DELETE FROM files WHERE repo = %s AND (path = %s OR path LIKE %s)",
                            (self.repo_name, rel_src_path, f"{rel_src_path}/%")
                        )
                    else:
                        cur.execute(
                            "DELETE FROM files WHERE repo = %s AND path = %s",
                            (self.repo_name, rel_src_path)
                        )
                    conn.commit()
                finally:
                    self.db_pool.putconn(conn)
            except ValueError:
                pass

        if event.is_directory:
            new_dir_path = Path(event.dest_path)
            for root, _, filenames in os.walk(new_dir_path):
                for fname in filenames:
                    self._handle_change(Path(root) / fname)
        else:
            self._handle_change(Path(event.dest_path))

    def _handle_change(self, fpath: Path):
        """@brief Re-index a changed file using selective incremental resolution refresh."""
        if (
            not self.should_exclude_fn(fpath, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            and not self.is_gitignored_fn(fpath, self.repo_root)
        ):
            lang = self.detect_language_fn(fpath, self.config)
            if lang:
                console.print(f"  [dim]Re-indexing {fpath.name}...[/]")
                watch_result = self.process_file_fn(
                    fpath,
                    self.repo_root,
                    self.repo_name,
                    self.config,
                    self.embedder,
                    self.classifier,
                    self.chunker,
                    self.db_pool,
                    no_classify=self.no_classify,
                    incremental_update=True,
                )
                if watch_result.get("error"):
                    console.print(
                        f"  [red]✗[/] [dim]{watch_result.get('path', fpath.name)}[/]: "
                        f"{watch_result['error']}"
                    )
                for warning in watch_result.get("warnings", []):
                    console.print(
                        f"  [yellow]![/] [dim]{watch_result.get('path', fpath.name)}[/]: {warning}"
                    )


def prune_stale_files(conn, repo_name: str, repo_root: Path, current_files: list[Path]) -> list[str]:
    """@brief Remove database records for files that no longer exist on disk."""
    cur = conn.cursor()
    cur.execute("SELECT path FROM files WHERE repo = %s", (repo_name,))
    db_paths = {row[0] for row in cur.fetchall()}
    current_paths = {str(f.relative_to(repo_root)) for f in current_files}
    stale_paths = db_paths - current_paths

    if stale_paths:
        for path in stale_paths:
            cur.execute("DELETE FROM files WHERE repo = %s AND path = %s", (repo_name, path))
        conn.commit()
    return list(stale_paths)


def clear_repo_per_file_data(config: dict, repo_name: str, get_db_fn: Callable[[dict], object]) -> None:
    """@brief Serially drop all per-file rows for a repo before a `--force` re-ingest."""
    conn = get_db_fn(config)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM symbol_references "
            "WHERE source_file_id IN (SELECT id FROM files WHERE repo = %s)",
            (repo_name,),
        )
        cur.execute(
            "DELETE FROM symbol_relationships "
            "WHERE source_file_id IN (SELECT id FROM files WHERE repo = %s)",
            (repo_name,),
        )
        cur.execute(
            "DELETE FROM dependencies "
            "WHERE source_file_id IN (SELECT id FROM files WHERE repo = %s)",
            (repo_name,),
        )
        cur.execute(
            "DELETE FROM symbols "
            "WHERE file_id IN (SELECT id FROM files WHERE repo = %s)",
            (repo_name,),
        )
        cur.execute(
            "DELETE FROM code_chunks "
            "WHERE file_id IN (SELECT id FROM files WHERE repo = %s)",
            (repo_name,),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_worker_count(cfg: dict, workers: Optional[int]) -> int:
    """@brief Resolve effective worker count and persist explicit overrides in config."""
    resolved = workers or cfg.get("ingestion", {}).get("workers", 4)
    if workers:
        cfg.setdefault("ingestion", {})["workers"] = workers
    return resolved


def print_ingestion_header(
    repo_name: str,
    cfg: dict,
    n_workers: int,
    no_classify: bool,
    debug: bool,
) -> None:
    """@brief Emit runtime configuration summary for an ingestion run."""
    console.print(f"\n[bold cyan]CodeBrain[/] — Ingesting [bold]{repo_name}[/]")
    console.print(f"  Database: {cfg['database']['url'].split('@')[1]}")
    console.print(f"  Embedding model: {cfg['embeddings']['model']}")
    console.print(f"  Classifier model: {cfg['classifier']['model'] if not no_classify else '[dim]skipped[/]'}")
    console.print(f"  Workers: {n_workers}")
    if debug:
        console.print("  Debug: [bold]enabled[/]")
        embed_base_url = (
            cfg.get("embeddings", {}).get("base_url")
            or cfg.get("embeddings", {}).get("ollama_url")
            or "http://localhost:11434"
        )
        console.print(f"  Embedding base URL: {embed_base_url}")
        console.print(f"  Classifier base URL: {cfg.get('classifier', {}).get('base_url', '')}")


def create_ingestion_run(
    cfg: dict,
    repo_name: str,
    get_db_fn: Callable[[dict], object],
    ensure_schema_fn: Callable[[object], None],
) -> int:
    """@brief Create an ingestion run row after ensuring schema readiness."""
    setup_conn = get_db_fn(cfg)
    try:
        ensure_schema_fn(setup_conn)
        cur = setup_conn.cursor()
        cur.execute(
            "INSERT INTO ingestion_runs (repo) VALUES (%s) RETURNING id",
            (repo_name,),
        )
        run_id = cur.fetchone()[0]
        setup_conn.commit()
        return run_id
    finally:
        setup_conn.close()


def discover_ingestion_files(
    cfg: dict,
    repo_name: str,
    repo_root: Path,
    force: bool,
    walk_repo_fn: Callable[[Path, dict], list[Path]],
    prune_stale_files_fn: Callable[[object, str, Path, list[Path]], list[str]],
    clear_repo_per_file_data_fn: Callable[[dict, str], None],
    get_db_fn: Callable[[dict], object],
) -> list[Path]:
    """@brief Collect ingestable files, prune stale rows, and apply force pre-clear when needed."""
    files = walk_repo_fn(repo_root, cfg)
    console.print(f"  Found [bold]{len(files)}[/] source files\n")

    prune_conn = get_db_fn(cfg)
    try:
        stale_paths = prune_stale_files_fn(prune_conn, repo_name, repo_root, files)
        if stale_paths:
            console.print(f"  Pruning [bold]{len(stale_paths)}[/] stale files from database")
    finally:
        prune_conn.close()

    if force:
        clear_repo_per_file_data_fn(cfg, repo_name)

    return files


def build_file_processor(
    repo_root: Path,
    repo_name: str,
    cfg: dict,
    embedder: EmbeddingClient,
    classifier: IntentClassifier,
    db_pool: psycopg2.pool.ThreadedConnectionPool,
    force: bool,
    no_classify: bool,
    process_file_fn: Callable[..., dict],
) -> Callable[[Path], dict]:
    """@brief Build the per-file worker callable with thread-local chunkers."""
    import threading

    thread_local = threading.local()

    def process(fpath: Path) -> dict:
        chunker = getattr(thread_local, "chunker", None)
        if chunker is None:
            chunker = ASTChunker(cfg)
            thread_local.chunker = chunker
        return process_file_fn(
            fpath,
            repo_root,
            repo_name,
            cfg,
            embedder,
            classifier,
            chunker,
            db_pool,
            force=force,
            no_classify=no_classify,
        )

    return process


def run_parallel_ingestion(
    files: list[Path],
    n_workers: int,
    process: Callable[[Path], dict],
    debug: bool,
) -> tuple[dict[str, int], list[tuple[str, str]], list[tuple[str, str]]]:
    """@brief Execute parallel file processing and aggregate run statistics."""
    stats = {
        "indexed": 0,
        "skipped": 0,
        "errors": 0,
        "chunks": 0,
        "symbols": 0,
        "classifier_fallbacks": 0,
    }
    error_details: list[tuple[str, str]] = []
    classifier_warning_details: list[tuple[str, str]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Indexing...", total=len(files))

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(process, fpath): fpath for fpath in files}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "status": "error",
                        "path": str(futures[future]),
                        "error": f"Worker exception: {e}",
                        "warnings": [],
                    }

                status_key = normalize_result_status(result.get("status"))
                stats[status_key] += 1
                if status_key == "errors":
                    error_path = result.get("path", "<unknown>")
                    if result.get("status") not in {"error", "errors"} and not result.get("error"):
                        error_msg = f"Unknown status '{result.get('status')}'"
                    else:
                        error_msg = result.get("error", "Unknown ingestion failure")
                    error_details.append((error_path, error_msg))
                    if debug:
                        console.print(f"  [red]✗[/] [dim]{error_path}[/]: {error_msg}")
                if result.get("chunks"):
                    stats["chunks"] += result["chunks"]
                if result.get("symbols"):
                    stats["symbols"] += result["symbols"]
                warnings = result.get("warnings", [])
                if warnings:
                    warn_path = result.get("path", "<unknown>")
                    for warning in warnings:
                        classifier_warning_details.append((warn_path, warning))
                        if debug:
                            console.print(f"  [yellow]![/] [dim]{warn_path}[/]: {warning}")
                    stats["classifier_fallbacks"] += len(warnings)
                progress.update(
                    task,
                    advance=1,
                    description=f"[dim]{result.get('path', '')[:60]}[/]",
                )

    return stats, error_details, classifier_warning_details


def print_detail_samples(title: str, icon: str, color: str, details: list[tuple[str, str]]) -> None:
    """@brief Print up to five detail rows and summarize remaining count."""
    if not details:
        return
    console.print(f"\n  [bold {color}]{title}:[/]")
    for detail_path, detail_msg in details[:5]:
        console.print(f"  [{color}]{icon}[/] [dim]{detail_path}[/]: {detail_msg}")
    if len(details) > 5:
        console.print(f"  [dim]... and {len(details) - 5} more[/]")


def refresh_cross_file_references(
    cfg: dict,
    repo_name: str,
    repo_root: Path,
    indexed_count: int,
    get_db_fn: Callable[[dict], object],
) -> None:
    """@brief Refresh unresolved cross-file references after parallel ingest completes."""
    if indexed_count <= 0:
        return
    console.print("\n  [dim]Refreshing cross-file symbol references...[/]")
    resolve_conn = get_db_fn(cfg)
    try:
        cur = resolve_conn.cursor()
        resolver.refresh_repo_references(cur, repo_name, repo_root=repo_root)
        resolve_conn.commit()
    finally:
        resolve_conn.close()


def materialize_cycles_for_repo(
    cfg: dict,
    repo_name: str,
    get_db_fn: Callable[[dict], object],
    materialize_dependency_cycles_fn: Callable[[object, str], int],
) -> int:
    """@brief Rebuild persisted dependency cycle materialization for a repository."""
    console.print("\n  [dim]Materializing dependency cycles...[/]")
    cycle_conn = get_db_fn(cfg)
    try:
        return materialize_dependency_cycles_fn(cycle_conn, repo_name)
    finally:
        cycle_conn.close()


def materialize_framework_diagnostics_for_repo(
    cfg: dict,
    repo_name: str,
    get_db_fn: Callable[[dict], object],
    materialize_missing_extractor_diagnostics_fn: Callable[[object, str], int],
) -> int:
    """@brief Rebuild callback-framework missing-extractor diagnostics for a repository."""
    console.print("\n  [dim]Materializing callback framework diagnostics...[/]")
    diagnostics_conn = get_db_fn(cfg)
    try:
        return materialize_missing_extractor_diagnostics_fn(diagnostics_conn, repo_name)
    finally:
        diagnostics_conn.close()


def materialize_clusters_for_repo(
    cfg: dict,
    repo_name: str,
    embedder: EmbeddingClient,
    classifier: IntentClassifier,
    no_classify: bool,
    get_db_fn: Callable[[dict], object],
    materialize_clusters_fn: Callable[..., tuple[int, str]],
) -> tuple[int, str]:
    """@brief Rebuild persisted clusters for a repository after ingestion."""
    console.print("\n  [dim]Materializing Leiden clusters...[/]")
    cluster_conn = get_db_fn(cfg)
    try:
        synthesis_cfg = cfg.get("synthesis", {})
        cluster_resolution = float(synthesis_cfg.get("resolution", 1.0) or 1.0)
        return materialize_clusters_fn(
            conn=cluster_conn,
            repo_name=repo_name,
            embedder=embedder,
            classifier=classifier,
            no_classify=no_classify,
            resolution=cluster_resolution,
        )
    finally:
        cluster_conn.close()


def materialize_flows_for_repo(
    cfg: dict,
    repo_name: str,
    get_db_fn: Callable[[dict], object],
    materialize_flows_fn: Callable[[object, str], int],
) -> int:
    """@brief Rebuild persisted execution flows for a repository after ingestion."""
    console.print("\n  [dim]Materializing execution flows...[/]")
    flow_conn = get_db_fn(cfg)
    try:
        return materialize_flows_fn(flow_conn, repo_name)
    finally:
        flow_conn.close()


def complete_ingestion_run(cfg: dict, run_id: int, stats: dict[str, int], get_db_fn: Callable[[dict], object]) -> None:
    """@brief Mark an ingestion run completed with final counters."""
    finish_conn = get_db_fn(cfg)
    try:
        cur = finish_conn.cursor()
        files_processed = stats["indexed"] + stats["skipped"] + stats["errors"]
        cur.execute(
            """UPDATE ingestion_runs
               SET completed_at=NOW(), files_processed=%s, chunks_created=%s,
                   symbols_found=%s, status='completed'
               WHERE id=%s""",
            (files_processed, stats["chunks"], stats["symbols"], run_id),
        )
        finish_conn.commit()
    finally:
        finish_conn.close()


def print_ingestion_summary(
    stats: dict[str, int],
    cycle_count: int,
    cluster_count: int,
    cluster_granularity: str,
    flow_count: int,
) -> None:
    """@brief Print final ingestion counters plus cycle/cluster/flow materialization counts."""
    console.print(f"\n[bold green]✓ Done[/]")
    console.print(f"  Files indexed: {stats['indexed']}")
    console.print(f"  Files skipped (unchanged): {stats['skipped']}")
    console.print(f"  Errors: {stats['errors']}")
    console.print(f"  Classifier fallbacks: {stats['classifier_fallbacks']}")
    console.print(f"  Chunks created: {stats['chunks']}")
    console.print(f"  Symbols extracted: {stats['symbols']}")
    console.print(f"  Dependency cycles materialized: {cycle_count}")
    console.print(f"  Clusters materialized ({cluster_granularity}): {cluster_count}")
    console.print(f"  Execution flows materialized: {flow_count}")


def run_watch_mode(
    watch: bool,
    cfg: dict,
    repo_root: Path,
    repo_name: str,
    embedder: EmbeddingClient,
    classifier: IntentClassifier,
    no_classify: bool,
    process_file_fn: Callable[..., dict],
    should_exclude_fn: Callable[[Path, Path, list[str]], bool],
    is_gitignored_fn: Callable[[Path, Path], bool],
    detect_language_fn: Callable[[Path, dict], Optional[str]],
) -> None:
    """@brief Start long-running watch mode when requested."""
    if not watch:
        return

    console.print(f"\n[bold cyan]Watching for changes...[/] (Ctrl+C to stop)")
    watch_chunker = ASTChunker(cfg)
    watch_pool = psycopg2.pool.ThreadedConnectionPool(1, 2, cfg["database"]["url"])
    handler = ReindexHandler(
        repo_root=repo_root,
        repo_name=repo_name,
        config=cfg,
        embedder=embedder,
        classifier=classifier,
        chunker=watch_chunker,
        db_pool=watch_pool,
        process_file_fn=process_file_fn,
        should_exclude_fn=should_exclude_fn,
        is_gitignored_fn=is_gitignored_fn,
        detect_language_fn=detect_language_fn,
        no_classify=no_classify,
    )

    observer = Observer()
    observer.schedule(handler, str(repo_root), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    watch_pool.closeall()
