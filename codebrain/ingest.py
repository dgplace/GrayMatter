#!./.venv/bin/python3
"""
@file ingest.py
@brief CodeBrain ingestion pipeline entrypoint and compatibility facade.
"""
import fnmatch
import hashlib
import os
import posixpath
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional
import click
import networkx as nx
import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector
try:
    import tomllib
except ImportError:
    import tomli as tomllib

# Allow direct script execution (`codebrain/ingest.py ...`).
if __package__ in (None, ""):
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from codebrain.chunker import ASTChunker
from codebrain.classifier import IntentClassifier
from codebrain.embedder import EmbeddingClient
from codebrain.ingestion.clusters import (
    CLUSTER_SUMMARY_MAX_CHARS,
    _build_cluster_embedding_input,
    _cluster_modularity_contribution,
    _detect_communities,
    _parse_cluster_profile,
    materialize_clusters,
)
from codebrain.ingestion.dependencies import (
    _candidate_internal_import_paths,
    _external_package_from_module,
    _external_version_for_package,
    _manifest_versions,
    _resolve_imported_symbol_id,
    _resolve_internal_import_target_file_id,
    _tarjan_strongly_connected_components,
    materialize_dependency_cycles,
)
from codebrain.ingestion.flows import materialize_flows
from codebrain.ingestion.relationships import _clean_swift_type, extract_swift_service_edges, extract_symbol_relationships
from codebrain.ingestion.runtime import (
    ReindexHandler as _RuntimeReindexHandler,
    build_file_processor,
    clear_repo_per_file_data as _runtime_clear_repo_per_file_data,
    complete_ingestion_run,
    create_ingestion_run,
    discover_ingestion_files,
    materialize_clusters_for_repo as _runtime_materialize_clusters_for_repo,
    materialize_cycles_for_repo as _runtime_materialize_cycles_for_repo,
    materialize_flows_for_repo as _runtime_materialize_flows_for_repo,
    normalize_result_status,
    print_detail_samples,
    print_ingestion_header,
    print_ingestion_summary,
    prune_stale_files,
    refresh_cross_file_references,
    resolve_worker_count,
    run_parallel_ingestion,
    run_watch_mode,
    walk_repo as _runtime_walk_repo,
)
from codebrain.ingestion.schema import SCHEMA_PATCHES, ensure_schema, insert_symbol
import resolver

NON_CODE_INTENT_BY_LANGUAGE = {
    "markdown": "documentation",
    "toml": "configuration",
    "yaml": "configuration",
}
DEFAULT_NON_CODE_MAX_BYTES = 262_144
DOC_LINK_EMBED_MAX_CHARS = 6_000
DEADLOCK_RETRY_ATTEMPTS = 2
DEADLOCK_RETRY_BASE_DELAY_SECONDS = 0.05

def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: str = "codebrain.toml") -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local_path = Path(".env/codebrain.toml")
    if local_path.exists():
        with open(local_path, "rb") as f:
            cfg = _deep_merge(cfg, tomllib.load(f))
    return _apply_env_overrides(cfg)


def _apply_env_overrides(cfg: dict) -> dict:
    """Allow container/CI environments to override boundary endpoints."""
    if db_url := os.environ.get("DATABASE_URL"):
        cfg.setdefault("database", {})["url"] = db_url
    if embed_url := os.environ.get("EMBED_BASE_URL"):
        cfg.setdefault("embeddings", {})["base_url"] = embed_url
    if classifier_url := os.environ.get("CLASSIFIER_BASE_URL"):
        cfg.setdefault("classifier", {})["base_url"] = classifier_url
    return cfg


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
    """@brief Resolve the configured language label for a file extension.

    @param path File path whose suffix should be matched.
    @param config Parsed CodeBrain configuration dictionary.
    @return Normalized language label, or `None` when unsupported.
    """
    ext = path.suffix.lstrip(".")
    return config.get("languages", {}).get("extensions", {}).get(ext)


def resolve_repo_name(repo_root: Path, repo_name_override: Optional[str]) -> str:
    """@brief Resolve the persisted repository identifier for an ingestion run.

    @param repo_root Absolute repository root path being indexed.
    @param repo_name_override Optional explicit repo name from CLI input.
    @return Repository identifier used for persistence and query scoping.
    @raises click.BadParameter When override contains path separators.
    """
    if repo_name_override is None:
        return repo_root.name
    normalized = repo_name_override.strip()
    if not normalized:
        return repo_root.name
    if "/" in normalized or "\\" in normalized:
        raise click.BadParameter(
            "repo name must be a simple identifier, not a path",
            param_hint="--repo-name",
        )
    return normalized


def non_code_max_bytes(config: dict) -> int:
    """@brief Return the per-file size cap used for non-code content.

    @param config Parsed CodeBrain configuration dictionary.
    @return Maximum non-code file size in bytes.
    """
    value = config.get("ingestion", {}).get("non_code_max_bytes", DEFAULT_NON_CODE_MAX_BYTES)
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return DEFAULT_NON_CODE_MAX_BYTES
    return max(cap, 1)


def forced_non_code_intent(language: Optional[str]) -> Optional[str]:
    """@brief Return the deterministic intent used for non-code files.

    @param language Detected language label for the file.
    @return Intent name when language is a supported non-code type, else `None`.
    """
    if not language:
        return None
    return NON_CODE_INTENT_BY_LANGUAGE.get(language)


def _normalize_doc_link_content(content: Optional[str]) -> Optional[str]:
    """@brief Normalize prose content before persisting doc_links rows.

    @param content Raw documentation text payload.
    @return Trimmed content, or `None` when payload is empty/whitespace.
    """
    if content is None:
        return None
    normalized = content.strip()
    if not normalized:
        return None
    return normalized


def _is_readme_doc_source(language: Optional[str], rel_path: str) -> bool:
    """@brief Decide whether a file should emit `source='readme'` doc links.

    Treats repository-level Markdown docs as readme-style prose so they can be
    associated with file-level targets in `doc_links`.

    @param language Detected file language.
    @param rel_path Repository-relative file path.
    @return True when the file is a README markdown file or top-level markdown doc.
    """
    if language != "markdown":
        return False
    normalized_path = rel_path.replace("\\", "/")
    basename = posixpath.basename(normalized_path).lower()
    return basename.startswith("readme") or "/" not in normalized_path


def _is_deadlock_error(error: Exception) -> bool:
    """@brief Return whether an exception represents a PostgreSQL deadlock.

    @param error Exception raised during SQL execution.
    @return True when the error is a deadlock and safe to retry.
    """
    if isinstance(error, psycopg2.errors.DeadlockDetected):
        return True
    return "deadlock detected" in str(error).lower()


def _build_doc_link_embedding_input(rel_path: str, source: str, content: str) -> str:
    """@brief Build bounded embedding input text for doc_links payloads.

    @param rel_path Repository-relative source path.
    @param source Doc link source label such as `docstring` or `readme`.
    @param content Normalized prose content.
    @return Embedding prompt text clipped to the configured max size.
    """
    return f"{source} {rel_path}\n{content[:DOC_LINK_EMBED_MAX_CHARS]}"


def _persist_doc_links(
    cur,
    embedder: EmbeddingClient,
    repo_name: str,
    rel_path: str,
    source_file_id: int,
    rows: list[dict],
) -> int:
    """@brief Persist prose links with embeddings for a single source file.

    @param cur Open database cursor scoped to the file transaction.
    @param embedder Shared embedding client.
    @param repo_name Repository identifier.
    @param rel_path Repository-relative source path.
    @param source_file_id File id that produced the doc links.
    @param rows Row payloads (`source`, `target_kind`, `target_id`, `content`).
    @return Number of inserted rows.
    @raises ValueError When embedding batch cardinality does not match rows.
    """
    if not rows:
        return 0

    embedding_inputs = [
        _build_doc_link_embedding_input(rel_path, row["source"], row["content"])
        for row in rows
    ]
    embeddings = embedder.embed_batch(embedding_inputs)
    if len(embeddings) != len(rows):
        raise ValueError(
            f"doc_links embedding cardinality mismatch for {rel_path}: "
            f"expected {len(rows)}, got {len(embeddings)}"
        )

    for row, embedding in zip(rows, embeddings):
        cur.execute(
            """INSERT INTO doc_links
               (repo, source_file_id, source, source_path, target_kind, target_id, content, embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                repo_name,
                source_file_id,
                row["source"],
                rel_path,
                row["target_kind"],
                row["target_id"],
                row["content"],
                embedding,
            ),
        )
    return len(rows)


def should_exclude(path: Path, repo_root: Path, excludes: list[str]) -> bool:
    """@brief Decide whether a path is excluded by ingestion patterns.

    Patterns containing glob metacharacters (`*`, `?`, `[`) are matched against
    each path segment with `fnmatch`, gitignore-style — so `*.triplibrary`
    prunes any segment ending in `.triplibrary` regardless of depth. Plain
    patterns (no glob chars) require an exact segment match, preserving
    existing behavior for entries like `node_modules`, `.git`, `target`.
    """
    segments = str(path.relative_to(repo_root)).split(os.sep)
    for pattern in excludes:
        if any(ch in pattern for ch in "*?["):
            if any(fnmatch.fnmatch(seg, pattern) for seg in segments):
                return True
        elif pattern in segments:
            return True
    return False


@lru_cache(maxsize=32)
def get_git_root(repo_root: str) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    git_root = result.stdout.strip()
    return Path(git_root).resolve() if git_root else None


def filter_gitignored_paths(paths: list[Path], repo_root: Path) -> list[Path]:
    """Drop paths ignored by Git, preserving input order."""
    if not paths:
        return paths

    git_root = get_git_root(str(repo_root))
    if git_root is None:
        return paths

    rel_paths = [path.relative_to(git_root).as_posix() for path in paths]
    payload = ("\0".join(rel_paths) + "\0").encode()

    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "check-ignore", "--stdin", "-z"],
            input=payload,
            capture_output=True,
            check=False,
        )
    except OSError:
        return paths

    if result.returncode not in (0, 1):
        return paths

    ignored = {
        rel_path
        for rel_path in result.stdout.decode("utf-8", errors="ignore").split("\0")
        if rel_path
    }
    return [path for path in paths if path.relative_to(git_root).as_posix() not in ignored]


def is_gitignored(path: Path, repo_root: Path) -> bool:
    return len(filter_gitignored_paths([path], repo_root)) == 0



def extract_symbol_references(chunks: list[dict]) -> list[dict]:
    return resolver.extract_symbol_references(chunks)


def resolve_target_symbol(cur, target_name: str) -> tuple[Optional[int], Optional[int]]:
    return resolver.resolve_target_symbol(cur, target_name)



# Large-function justification: `process_file` is the single per-file
# transaction boundary for ingestion, so chunk persistence, symbol persistence,
# dependency extraction, resolver invocation, and warning propagation remain
# co-located to keep rollback behavior explicit.
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
    incremental_update: bool = False,
    _deadlock_retry_attempt: int = 0,
) -> dict:
    """@brief Parse, classify, embed, and persist one file.

    @param fpath Absolute path to the file being indexed.
    @param repo_root Absolute repository root used for relative path storage.
    @param repo_name Repository name persisted in database records.
    @param config Parsed CodeBrain configuration dictionary.
    @param embedder Shared embedding client.
    @param classifier Shared classifier client.
    @param chunker Thread-local chunker instance.
    @param db_pool Shared database connection pool.
    @param force Whether to bypass file hash skip checks.
    @param no_classify Whether to skip classifier calls.
    @param incremental_update Whether to re-resolve impacted inbound references
            for a single changed file instead of doing batch-style indexing.
    @param _deadlock_retry_attempt Internal retry counter for deadlock recovery.
    @return Result dictionary containing status, optional counters/error details,
            and optional processing warning messages under `warnings`.
    """
    rel_path = str(fpath.relative_to(repo_root))
    language = detect_language(fpath, config)
    file_size_bytes = fpath.stat().st_size
    file_hash = sha256_file(fpath)

    conn = db_pool.getconn()
    register_vector(conn)
    retry_deadlock = False
    retry_error: Optional[Exception] = None
    try:
        cur = conn.cursor()

        # Check if file already indexed with same hash
        cur.execute(
            "SELECT id, hash FROM files WHERE repo = %s AND path = %s",
            (repo_name, rel_path)
        )
        existing = cur.fetchone()
        forced_intent = forced_non_code_intent(language)
        max_non_code_bytes = non_code_max_bytes(config)
        if forced_intent and file_size_bytes > max_non_code_bytes:
            if existing:
                cur.execute("DELETE FROM files WHERE id = %s", (existing[0],))
            conn.commit()
            return {
                "status": "skipped",
                "path": rel_path,
                "warnings": [
                    (
                        f"Skipped non-code file over cap ({file_size_bytes} bytes > "
                        f"{max_non_code_bytes} bytes): {rel_path}"
                    )
                ],
            }
        if existing and existing[1] == file_hash and not force:
            return {"status": "skipped", "path": rel_path}

        # Read file content
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"status": "error", "path": rel_path, "error": str(e)}

        line_count = content.count("\n") + 1

        # Generate file-level summary + role (one LLM call) and embedding
        processing_warnings: list[str] = []

        if no_classify:
            file_summary, file_role = "", "unknown"
        else:
            file_summary, file_role = classifier.analyze_file(
                rel_path,
                content[:3000],
                language,
                on_warning=processing_warnings.append,
            )
        file_embedding = embedder.embed(f"{rel_path}\n{file_summary}")

        # Upsert file record
        incremental_refresh = None
        if existing:
            if incremental_update:
                incremental_refresh = resolver.capture_incremental_refresh(cur, repo_name, existing[0])
                processing_warnings.extend(incremental_refresh["warnings"])
            cur.execute(
                """UPDATE files SET language=%s, size_bytes=%s, line_count=%s, hash=%s,
                   summary=%s, role=%s, embedding=%s, indexed_at=NOW()
                   WHERE id=%s""",
                (language, file_size_bytes, line_count, file_hash,
                 file_summary, file_role, file_embedding, existing[0])
            )
            file_id = existing[0]
            # Order matters under parallel re-ingest: delete from relationship/
            # reference edge tables before symbols/code_chunks so FK cascades are
            # no-ops instead of lock amplification under worker concurrency.
            cur.execute("DELETE FROM symbol_references WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM symbol_relationships WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM dependencies WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM doc_links WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM symbols WHERE file_id = %s", (file_id,))
            cur.execute("DELETE FROM code_chunks WHERE file_id = %s", (file_id,))
        else:
            cur.execute(
                """INSERT INTO files (repo, path, language, size_bytes, line_count, hash, summary, role, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (repo_name, rel_path, language, file_size_bytes, line_count,
                 file_hash, file_summary, file_role, file_embedding)
            )
            file_id = cur.fetchone()[0]

        # Parse and chunk
        chunks = chunker.chunk_file(content, language, rel_path)

        if not chunks:
            if incremental_refresh:
                resolver.re_resolve_inbound_references(
                    cur,
                    incremental_refresh,
                    repo_name=repo_name,
                    repo_root=repo_root,
                )
            conn.commit()
            return {
                "status": "indexed",
                "path": rel_path,
                "chunks": 0,
                "symbols": 0,
                "warnings": processing_warnings,
            }

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
        if forced_intent:
            chunk_classifications = [(forced_intent, "")] * len(chunks)
        elif no_classify:
            chunk_classifications = [("utility", "")] * len(chunks)
        else:
            chunk_classifications = classifier.classify_chunks_batch(
                chunks,
                language,
                rel_path,
                on_warning=processing_warnings.append,
            )
        # ------------------------------------------------------------------

        chunk_count = 0
        symbol_count = 0
        doc_link_rows: list[dict] = []
        chunk_ids = {}
        container_symbol_ids: dict[str, int] = {}
        file_symbol_ids: dict[str, int] = {}

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
            chunk_ids[i] = chunk_id
            chunk_count += 1

            if chunk.get("symbol_name") and i in symbol_embedding_map:
                parent_qname = (
                    f"{rel_path}:{chunk['parent_symbol']}"
                    if chunk.get("parent_symbol")
                    else None
                )
                symbol_id = insert_symbol(
                    cur,
                    file_id,
                    chunk_id,
                    {
                        "name": chunk["symbol_name"],
                        "qualified_name": chunk.get("qualified_name"),
                        "kind": chunk.get("symbol_type", "unknown"),
                        "signature": chunk.get("signature"),
                        "docstring": chunk.get("docstring"),
                        "start_line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                        "container_symbol": chunk.get("container_symbol") or chunk.get("parent_symbol"),
                        "visibility": chunk.get("visibility", "public"),
                        "is_exported": chunk.get("is_exported", False),
                        "declared_in_extension": chunk.get("declared_in_extension", False),
                        "is_primary_declaration": chunk.get("is_primary_declaration", True),
                    },
                    symbol_embedding_map[i],
                    parent_id=container_symbol_ids.get(parent_qname) if parent_qname else None,
                )
                if chunk.get("qualified_name"):
                    container_symbol_ids[chunk["qualified_name"]] = symbol_id
                file_symbol_ids.setdefault(chunk["symbol_name"], symbol_id)
                symbol_count += 1
                normalized_docstring = _normalize_doc_link_content(chunk.get("docstring"))
                if normalized_docstring:
                    doc_link_rows.append(
                        {
                            "source": "docstring",
                            "target_kind": "symbol",
                            "target_id": symbol_id,
                            "content": normalized_docstring,
                        }
                    )

                for member_symbol in chunk.get("member_symbols", []):
                    member_id = insert_symbol(
                        cur,
                        file_id,
                        chunk_id,
                        {
                            "name": member_symbol["symbol_name"],
                            "qualified_name": member_symbol.get("qualified_name"),
                            "kind": member_symbol.get("symbol_type", "unknown"),
                            "signature": member_symbol.get("signature"),
                            "docstring": member_symbol.get("docstring"),
                            "start_line": member_symbol["start_line"],
                            "end_line": member_symbol["end_line"],
                            "container_symbol": member_symbol.get("container_symbol"),
                            "visibility": member_symbol.get("visibility", "public"),
                            "is_exported": member_symbol.get("is_exported", False),
                            "declared_in_extension": member_symbol.get("declared_in_extension", False),
                            "is_primary_declaration": False,
                        },
                        chunk_embeddings[i],
                        parent_id=symbol_id,
                    )
                    file_symbol_ids.setdefault(member_symbol["symbol_name"], member_id)
                    symbol_count += 1
                    normalized_member_docstring = _normalize_doc_link_content(member_symbol.get("docstring"))
                    if normalized_member_docstring:
                        doc_link_rows.append(
                            {
                                "source": "docstring",
                                "target_kind": "symbol",
                                "target_id": member_id,
                                "content": normalized_member_docstring,
                            }
                        )

        normalized_readme_content = _normalize_doc_link_content(content)
        if normalized_readme_content and _is_readme_doc_source(language, rel_path):
            doc_link_rows.append(
                {
                    "source": "readme",
                    "target_kind": "file",
                    "target_id": file_id,
                    "content": normalized_readme_content,
                }
            )

        structural_edges = extract_symbol_relationships(chunks, language)
        for edge in structural_edges:
            source_symbol_id = file_symbol_ids.get(edge["source_symbol_name"])
            if source_symbol_id is None:
                continue
            target_symbol_id, _ = resolve_target_symbol(cur, edge["target_name"])
            cur.execute(
                """INSERT INTO symbol_relationships
                   (source_file_id, source_symbol_id, target_symbol_id, relationship_kind, target_name, external_module, line_no)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    file_id,
                    source_symbol_id,
                    target_symbol_id,
                    edge["relationship_kind"],
                    edge["target_name"],
                    edge["external_module"],
                    edge["line_no"],
                ),
            )

        # Extract and store dependencies
        manifest_versions = _manifest_versions(str(repo_root))
        deps = chunker.extract_dependencies(content, language, rel_path)
        for dep in deps:
            module = dep.get("module")
            imported_name = dep.get("imported_name")
            local_alias = dep.get("local_alias")
            target_file_id = _resolve_internal_import_target_file_id(
                cur,
                repo_name,
                rel_path,
                module or "",
                language,
            ) if module else None
            is_external = target_file_id is None
            if language in {"c", "cpp"} and dep.get("raw", "").startswith("#include \""):
                is_external = False
            imported_symbol_id = _resolve_imported_symbol_id(cur, target_file_id, imported_name)
            external_module = None
            external_version = None
            if is_external and module:
                external_module = _external_package_from_module(module, language)
                external_version = _external_version_for_package(
                    external_module,
                    module,
                    language,
                    manifest_versions,
                )
            cur.execute(
                """INSERT INTO dependencies
                   (source_file_id, target_file_id, kind, external_module, external_version, imported_name, local_alias, imported_symbol_id, is_external)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    file_id,
                    target_file_id,
                    dep["kind"],
                    external_module,
                    external_version,
                    imported_name,
                    local_alias,
                    imported_symbol_id,
                    is_external,
                )
            )

        if language == "swift":
            for edge in extract_swift_service_edges(content, chunks):
                target_symbol_id, target_file_id = resolve_target_symbol(cur, edge["target_name"])
                cur.execute(
                    """INSERT INTO dependencies
                       (source_file_id, target_file_id, source_symbol_id, target_symbol_id, kind, external_module)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        file_id,
                        target_file_id,
                        file_symbol_ids.get(edge.get("source_symbol_name", "")),
                        target_symbol_id,
                        edge["kind"],
                        edge["target_name"],
                    ),
                )

        # Persist unresolved rows during parallel ingest; refresh cross-file
        # target ids in a later serial pass once all symbols are stable.
        reference_records = (
            resolver.resolve_references(
                cur,
                chunks,
                language=language,
                file_path=rel_path,
                source_file_id=file_id,
                repo_root=repo_root,
                repo_name=repo_name,
            )
            if incremental_update
            else resolver.build_reference_records(chunks, language=language)
        )
        for reference in reference_records:
            cur.execute(
                """INSERT INTO symbol_references
                   (source_file_id, source_chunk_id, source_symbol_name, target_name,
                    target_symbol_id, resolution_confidence, resolution_method,
                    reference_kind, reference_kind_v2, line_no)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    file_id,
                    chunk_ids.get(reference["chunk_index"]),
                    reference.get("source_symbol_name"),
                    reference["target_name"],
                    reference["target_symbol_id"],
                    reference["resolution_confidence"],
                    reference["resolution_method"],
                    reference["reference_kind"],
                    reference["reference_kind_v2"],
                    reference["line_no"],
                ),
            )

        _persist_doc_links(
            cur=cur,
            embedder=embedder,
            repo_name=repo_name,
            rel_path=rel_path,
            source_file_id=file_id,
            rows=doc_link_rows,
        )

        if incremental_refresh:
            resolver.re_resolve_inbound_references(
                cur,
                incremental_refresh,
                repo_name=repo_name,
                repo_root=repo_root,
            )

        conn.commit()
        return {
            "status": "indexed",
            "path": rel_path,
            "chunks": chunk_count,
            "symbols": symbol_count,
            "warnings": processing_warnings,
        }

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if _is_deadlock_error(e) and _deadlock_retry_attempt < DEADLOCK_RETRY_ATTEMPTS:
            retry_deadlock = True
            retry_error = e
        else:
            deadlock_suffix = ""
            if _is_deadlock_error(e):
                deadlock_suffix = (
                    f" (deadlock retries exhausted after {_deadlock_retry_attempt} attempts)"
                )
            return {
                "status": "error",
                "path": rel_path,
                "error": f"{e}{deadlock_suffix}",
                "warnings": processing_warnings if "processing_warnings" in locals() else [],
            }
    finally:
        db_pool.putconn(conn)

    if retry_deadlock:
        # Small bounded backoff gives concurrent workers time to release locks.
        time.sleep(DEADLOCK_RETRY_BASE_DELAY_SECONDS * (2 ** _deadlock_retry_attempt))
        return process_file(
            fpath=fpath,
            repo_root=repo_root,
            repo_name=repo_name,
            config=config,
            embedder=embedder,
            classifier=classifier,
            chunker=chunker,
            db_pool=db_pool,
            force=force,
            no_classify=no_classify,
            incremental_update=incremental_update,
            _deadlock_retry_attempt=_deadlock_retry_attempt + 1,
        )
    if retry_error is not None:
        return {
            "status": "error",
            "path": rel_path,
            "error": str(retry_error),
            "warnings": processing_warnings if "processing_warnings" in locals() else [],
        }
    return {"status": "error", "path": rel_path, "error": "Unknown ingestion failure"}




def walk_repo(repo_root: Path, config: dict) -> list[Path]:
    return _runtime_walk_repo(
        repo_root=repo_root,
        config=config,
        should_exclude_fn=should_exclude,
        filter_gitignored_paths_fn=filter_gitignored_paths,
    )


def clear_repo_per_file_data(config: dict, repo_name: str) -> None:
    _runtime_clear_repo_per_file_data(config=config, repo_name=repo_name, get_db_fn=get_db)


class ReindexHandler(_RuntimeReindexHandler):
    def __init__(
        self,
        repo_root: Path,
        repo_name: str,
        config: dict,
        embedder: EmbeddingClient,
        classifier: IntentClassifier,
        chunker: ASTChunker,
        db_pool: psycopg2.pool.ThreadedConnectionPool,
        no_classify: bool = False,
    ):
        super().__init__(
            repo_root=repo_root,
            repo_name=repo_name,
            config=config,
            embedder=embedder,
            classifier=classifier,
            chunker=chunker,
            db_pool=db_pool,
            process_file_fn=process_file,
            should_exclude_fn=should_exclude,
            is_gitignored_fn=is_gitignored,
            detect_language_fn=detect_language,
            no_classify=no_classify,
        )


@click.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--config", default="codebrain.toml", help="Config file path")
@click.option("--force", is_flag=True, help="Re-index all files regardless of hash")
@click.option("--watch", is_flag=True, help="Watch for changes and re-index")
@click.option("--workers", default=None, type=int, help="Override worker count")
@click.option("--no-classify", is_flag=True, help="Skip LLM classification (embed only, much faster)")
@click.option("--debug", is_flag=True, help="Print per-file error details during ingestion")
@click.option("--repo-name", default=None, help="Optional repository identifier override for indexed rows")
def main(
    repo_path: str,
    config: str,
    force: bool,
    watch: bool,
    workers: Optional[int],
    no_classify: bool,
    debug: bool,
    repo_name: Optional[str],
):
    """@brief Ingest a repository into CodeBrain."""
    cfg = load_config(config)
    repo_root = Path(repo_path).resolve()
    resolved_repo_name = resolve_repo_name(repo_root, repo_name)
    n_workers = resolve_worker_count(cfg, workers)
    print_ingestion_header(resolved_repo_name, cfg, n_workers, no_classify, debug)

    embedder = EmbeddingClient(cfg)
    classifier = IntentClassifier(cfg)
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, n_workers + 2, cfg["database"]["url"])
    run_id = create_ingestion_run(
        cfg=cfg,
        repo_name=resolved_repo_name,
        get_db_fn=get_db,
        ensure_schema_fn=ensure_schema,
    )
    files = discover_ingestion_files(
        cfg=cfg,
        repo_name=resolved_repo_name,
        repo_root=repo_root,
        force=force,
        walk_repo_fn=walk_repo,
        prune_stale_files_fn=prune_stale_files,
        clear_repo_per_file_data_fn=clear_repo_per_file_data,
        get_db_fn=get_db,
    )
    process = build_file_processor(
        repo_root=repo_root,
        repo_name=resolved_repo_name,
        cfg=cfg,
        embedder=embedder,
        classifier=classifier,
        db_pool=db_pool,
        force=force,
        no_classify=no_classify,
        process_file_fn=process_file,
    )

    try:
        stats, error_details, classifier_warning_details = run_parallel_ingestion(
            files=files,
            n_workers=n_workers,
            process=process,
            debug=debug,
        )
        print_detail_samples("Error samples", "✗", "red", error_details)
        print_detail_samples("Classifier fallback samples", "!", "yellow", classifier_warning_details)
        refresh_cross_file_references(
            cfg=cfg,
            repo_name=resolved_repo_name,
            repo_root=repo_root,
            indexed_count=stats["indexed"],
            get_db_fn=get_db,
        )
        cycle_count = _runtime_materialize_cycles_for_repo(
            cfg=cfg,
            repo_name=resolved_repo_name,
            get_db_fn=get_db,
            materialize_dependency_cycles_fn=materialize_dependency_cycles,
        )
        cluster_count, cluster_granularity = _runtime_materialize_clusters_for_repo(
            cfg=cfg,
            repo_name=resolved_repo_name,
            embedder=embedder,
            classifier=classifier,
            no_classify=no_classify,
            get_db_fn=get_db,
            materialize_clusters_fn=materialize_clusters,
        )
        flow_count = _runtime_materialize_flows_for_repo(
            cfg=cfg,
            repo_name=resolved_repo_name,
            get_db_fn=get_db,
            materialize_flows_fn=materialize_flows,
        )
        complete_ingestion_run(cfg=cfg, run_id=run_id, stats=stats, get_db_fn=get_db)
    finally:
        db_pool.closeall()

    print_ingestion_summary(stats, cycle_count, cluster_count, cluster_granularity, flow_count)
    run_watch_mode(
        watch=watch,
        cfg=cfg,
        repo_root=repo_root,
        repo_name=resolved_repo_name,
        embedder=embedder,
        classifier=classifier,
        no_classify=no_classify,
        process_file_fn=process_file,
        should_exclude_fn=should_exclude,
        is_gitignored_fn=is_gitignored,
        detect_language_fn=detect_language,
    )
if __name__ == "__main__":
    main()
