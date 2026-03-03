#!/usr/bin/env python3
"""
CodeBrain Ingestion Pipeline
Walks a codebase, parses with tree-sitter, embeds with Ollama, classifies intent, stores in PostgreSQL.
"""

import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import click
import psycopg2
import psycopg2.extras
import psycopg2.pool
from pgvector.psycopg2 import register_vector
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from chunker import ASTChunker
from classifier import IntentClassifier
from embedder import EmbeddingClient

console = Console()


def load_config(path: str = "codebrain.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def get_db(config: dict):
    conn = psycopg2.connect(config["database"]["url"])
    register_vector(conn)
    return conn


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_language(path: Path, config: dict) -> Optional[str]:
    ext = path.suffix.lstrip(".")
    return config.get("languages", {}).get("extensions", {}).get(ext)


def should_exclude(path: Path, repo_root: Path, excludes: list[str]) -> bool:
    rel = str(path.relative_to(repo_root))
    for pattern in excludes:
        if pattern.startswith("*"):
            if rel.endswith(pattern[1:]):
                return True
        elif pattern in rel.split(os.sep):
            return True
    return False


def walk_repo(repo_root: Path, config: dict) -> list[Path]:
    """Walk the repository, respecting excludes and .gitignore."""
    excludes = config.get("ingestion", {}).get("exclude", [])
    supported_exts = set()
    for ext in config.get("languages", {}).get("extensions", {}).keys():
        supported_exts.add(f".{ext}")

    files = []
    for root, dirs, filenames in os.walk(repo_root):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not should_exclude(root_path / d, repo_root, excludes)
        ]
        for fname in filenames:
            fpath = root_path / fname
            if fpath.suffix in supported_exts and not should_exclude(fpath, repo_root, excludes):
                files.append(fpath)
    return files


def process_file(
    fpath: Path,
    repo_root: Path,
    repo_name: str,
    config: dict,
    embedder: EmbeddingClient,
    classifier: IntentClassifier,
    chunker: ASTChunker,
    db_pool: psycopg2.pool.ThreadedConnectionPool,
    force: bool = False,
    no_classify: bool = False,
) -> dict:
    """Process a single file: parse, chunk, embed, classify, store."""
    rel_path = str(fpath.relative_to(repo_root))
    language = detect_language(fpath, config)
    file_hash = sha256_file(fpath)

    conn = db_pool.getconn()
    register_vector(conn)
    try:
        cur = conn.cursor()

        # Check if file already indexed with same hash
        cur.execute(
            "SELECT id, hash FROM files WHERE repo = %s AND path = %s",
            (repo_name, rel_path)
        )
        existing = cur.fetchone()
        if existing and existing[1] == file_hash and not force:
            return {"status": "skipped", "path": rel_path}

        # Read file content
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"status": "error", "path": rel_path, "error": str(e)}

        line_count = content.count("\n") + 1

        # Generate file-level summary + role (one LLM call) and embedding
        if no_classify:
            file_summary, file_role = "", "unknown"
        else:
            file_summary, file_role = classifier.analyze_file(rel_path, content[:3000], language)
        file_embedding = embedder.embed(f"{rel_path}\n{file_summary}")

        # Upsert file record
        if existing:
            cur.execute(
                """UPDATE files SET language=%s, size_bytes=%s, line_count=%s, hash=%s,
                   summary=%s, role=%s, embedding=%s, indexed_at=NOW()
                   WHERE id=%s""",
                (language, fpath.stat().st_size, line_count, file_hash,
                 file_summary, file_role, file_embedding, existing[0])
            )
            file_id = existing[0]
            cur.execute("DELETE FROM code_chunks WHERE file_id = %s", (file_id,))
            cur.execute("DELETE FROM symbols WHERE file_id = %s", (file_id,))
            cur.execute("DELETE FROM dependencies WHERE source_file_id = %s", (file_id,))
        else:
            cur.execute(
                """INSERT INTO files (repo, path, language, size_bytes, line_count, hash, summary, role, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (repo_name, rel_path, language, fpath.stat().st_size, line_count,
                 file_hash, file_summary, file_role, file_embedding)
            )
            file_id = cur.fetchone()[0]

        # Parse and chunk
        chunks = chunker.chunk_file(content, language, rel_path)

        if not chunks:
            conn.commit()
            return {"status": "indexed", "path": rel_path, "chunks": 0, "symbols": 0}

        # --- Batch all embeddings for this file in one call ---
        chunk_embed_texts = [f"# {rel_path}\n{c['content']}" for c in chunks]
        symbol_indices = [i for i, c in enumerate(chunks) if c.get("symbol_name")]
        symbol_embed_texts = [
            f"{chunks[i].get('symbol_type', '')} {chunks[i]['symbol_name']}: {chunks[i].get('docstring', '')}"
            for i in symbol_indices
        ]

        all_embeddings = embedder.embed_batch(chunk_embed_texts + symbol_embed_texts)
        chunk_embeddings = all_embeddings[:len(chunks)]
        symbol_embedding_map = dict(zip(symbol_indices, all_embeddings[len(chunks):]))
        # ------------------------------------------------------

        # --- Batch classify all chunks in one LLM call (or skip) ----------
        if no_classify:
            chunk_classifications = [("utility", "")] * len(chunks)
        else:
            chunk_classifications = classifier.classify_chunks_batch(chunks, language, rel_path)
        # ------------------------------------------------------------------

        chunk_count = 0
        symbol_count = 0

        for i, chunk in enumerate(chunks):
            intent, intent_detail = chunk_classifications[i]

            cur.execute(
                """INSERT INTO code_chunks
                   (file_id, chunk_index, content, start_line, end_line,
                    symbol_name, symbol_type, parent_symbol, intent, intent_detail, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (file_id, i, chunk["content"], chunk["start_line"], chunk["end_line"],
                 chunk.get("symbol_name"), chunk.get("symbol_type"), chunk.get("parent_symbol"),
                 intent, intent_detail, chunk_embeddings[i])
            )
            chunk_id = cur.fetchone()[0]
            chunk_count += 1

            if chunk.get("symbol_name") and i in symbol_embedding_map:
                cur.execute(
                    """INSERT INTO symbols
                       (file_id, chunk_id, name, qualified_name, kind, signature, docstring,
                        start_line, end_line, visibility, is_exported, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (file_id, chunk_id, chunk["symbol_name"], chunk.get("qualified_name"),
                     chunk.get("symbol_type", "unknown"), chunk.get("signature"),
                     chunk.get("docstring"), chunk["start_line"], chunk["end_line"],
                     chunk.get("visibility", "public"), chunk.get("is_exported", False),
                     symbol_embedding_map[i])
                )
                symbol_count += 1

        # Extract and store dependencies
        deps = chunker.extract_dependencies(content, language, rel_path)
        for dep in deps:
            cur.execute(
                """INSERT INTO dependencies (source_file_id, kind, external_module)
                   VALUES (%s, %s, %s)""",
                (file_id, dep["kind"], dep["module"])
            )

        conn.commit()
        return {
            "status": "indexed",
            "path": rel_path,
            "chunks": chunk_count,
            "symbols": symbol_count,
        }

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"status": "error", "path": rel_path, "error": str(e)}
    finally:
        db_pool.putconn(conn)


@click.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--config", default="codebrain.toml", help="Config file path")
@click.option("--force", is_flag=True, help="Re-index all files regardless of hash")
@click.option("--watch", is_flag=True, help="Watch for changes and re-index")
@click.option("--workers", default=None, type=int, help="Override worker count")
@click.option("--no-classify", is_flag=True, help="Skip LLM classification (embed only, much faster)")
def main(repo_path: str, config: str, force: bool, watch: bool, workers: Optional[int], no_classify: bool):
    """Ingest a codebase into CodeBrain."""
    cfg = load_config(config)
    repo_root = Path(repo_path).resolve()
    repo_name = repo_root.name

    n_workers = workers or cfg.get("ingestion", {}).get("workers", 4)
    if workers:
        cfg.setdefault("ingestion", {})["workers"] = workers

    console.print(f"\n[bold cyan]CodeBrain[/] — Ingesting [bold]{repo_name}[/]")
    console.print(f"  Database: {cfg['database']['url'].split('@')[1]}")
    console.print(f"  Embedding model: {cfg['embeddings']['model']}")
    console.print(f"  Classifier model: {cfg['classifier']['model'] if not no_classify else '[dim]skipped[/]'}")
    console.print(f"  Workers: {n_workers}")

    # Shared HTTP clients (thread-safe); one chunker per thread created below
    embedder = EmbeddingClient(cfg)
    classifier = IntentClassifier(cfg)

    # Connection pool — one connection slot per worker plus a couple spare
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        1, n_workers + 2, cfg["database"]["url"]
    )

    # Create ingestion run
    setup_conn = get_db(cfg)
    cur = setup_conn.cursor()
    cur.execute(
        "INSERT INTO ingestion_runs (repo) VALUES (%s) RETURNING id",
        (repo_name,)
    )
    run_id = cur.fetchone()[0]
    setup_conn.commit()
    setup_conn.close()

    # Walk repository
    files = walk_repo(repo_root, cfg)
    console.print(f"  Found [bold]{len(files)}[/] source files\n")

    stats = {"indexed": 0, "skipped": 0, "errors": 0, "chunks": 0, "symbols": 0}

    # Each thread gets its own ASTChunker (tree-sitter parsers are not thread-safe)
    thread_chunkers: dict[int, ASTChunker] = {}

    def get_chunker() -> ASTChunker:
        tid = id(os.getpid())  # unique per thread via threading.get_ident below
        import threading
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
            force=force,
            no_classify=no_classify,
        )

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
                result = future.result()
                stats[result["status"]] = stats.get(result["status"], 0) + 1
                if result.get("chunks"):
                    stats["chunks"] += result["chunks"]
                if result.get("symbols"):
                    stats["symbols"] += result["symbols"]
                progress.update(
                    task, advance=1,
                    description=f"[dim]{result.get('path', '')[:60]}[/]"
                )

    # Update ingestion run
    finish_conn = get_db(cfg)
    cur = finish_conn.cursor()
    cur.execute(
        """UPDATE ingestion_runs
           SET completed_at=NOW(), files_processed=%s, chunks_created=%s,
               symbols_found=%s, status='completed'
           WHERE id=%s""",
        (stats["indexed"], stats["chunks"], stats["symbols"], run_id)
    )
    finish_conn.commit()
    finish_conn.close()
    db_pool.closeall()

    console.print(f"\n[bold green]✓ Done[/]")
    console.print(f"  Files indexed: {stats['indexed']}")
    console.print(f"  Files skipped (unchanged): {stats['skipped']}")
    console.print(f"  Errors: {stats['errors']}")
    console.print(f"  Chunks created: {stats['chunks']}")
    console.print(f"  Symbols extracted: {stats['symbols']}")

    if watch:
        console.print(f"\n[bold cyan]Watching for changes...[/] (Ctrl+C to stop)")
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        watch_conn = get_db(cfg)
        watch_chunker = ASTChunker(cfg)
        watch_pool = psycopg2.pool.ThreadedConnectionPool(1, 2, cfg["database"]["url"])

        class ReindexHandler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory:
                    fpath = Path(event.src_path)
                    if not should_exclude(fpath, repo_root, cfg.get("ingestion", {}).get("exclude", [])):
                        lang = detect_language(fpath, cfg)
                        if lang:
                            console.print(f"  [dim]Re-indexing {fpath.name}...[/]")
                            process_file(fpath, repo_root, repo_name, cfg,
                                         embedder, classifier, watch_chunker, watch_pool)

        observer = Observer()
        observer.schedule(ReindexHandler(), str(repo_root), recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
        watch_pool.closeall()


if __name__ == "__main__":
    main()
