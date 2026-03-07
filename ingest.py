#!./.venv/bin/python3
"""
CodeBrain Ingestion Pipeline
Walks a codebase, parses with tree-sitter, embeds with Ollama, classifies intent, stores in PostgreSQL.
 options: --debug, --force, --watch
"""

import hashlib
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
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


SCHEMA_PATCHES = [
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS container_symbol TEXT
    """,
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS declared_in_extension BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS is_primary_declaration BOOLEAN NOT NULL DEFAULT TRUE
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_references (
        id SERIAL PRIMARY KEY,
        source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_chunk_id INTEGER REFERENCES code_chunks(id) ON DELETE CASCADE,
        source_symbol_name TEXT,
        target_name TEXT NOT NULL,
        reference_kind TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_container
    ON symbols(container_symbol)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_primary
    ON symbols(is_primary_declaration)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_file
    ON symbol_references(source_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_chunk
    ON symbol_references(source_chunk_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name
    ON symbol_references(target_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_kind
    ON symbol_references(reference_kind)
    """,
    """
    CREATE TABLE IF NOT EXISTS module_intents (
      repo            TEXT NOT NULL,
      module_path     TEXT NOT NULL,
      kind            TEXT NOT NULL DEFAULT 'directory',
      module_name     TEXT,
      summary         TEXT,
      role            TEXT,
      dominant_intent TEXT,
      file_count      INTEGER NOT NULL DEFAULT 0,
      chunk_count     INTEGER NOT NULL DEFAULT 0,
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (repo, module_path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind)
    """,
    """
    ALTER TABLE module_intents
    ADD COLUMN IF NOT EXISTS member_symbols TEXT[]
    """,
]

REFERENCE_PATTERNS = [
    (re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b"), "type_reference"),
    (re.compile(r"(?<![.\w])([a-z_][A-Za-z0-9_]*)\s*\("), "call"),
    (re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\("), "member_call"),
]

REFERENCE_STOPWORDS = {
    "as", "catch", "class", "defer", "else", "enum", "extension", "for", "func",
    "guard", "if", "import", "init", "in", "let", "private", "protocol", "public",
    "return", "struct", "subscript", "switch", "throw", "try", "var", "where", "while",
}

SWIFT_TYPED_PROPERTY_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:\w+\s+)*(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_<>.?[\]]*)",
    re.MULTILINE,
)
SWIFT_INIT_RE = re.compile(r"\binit\s*\((.*?)\)", re.DOTALL)
SWIFT_PARAM_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_<>.?[\]]*)"
)
SWIFT_MEMBER_CALL_RE = re.compile(r"\b([a-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def ensure_schema(conn) -> None:
    cur = conn.cursor()
    try:
        for statement in SCHEMA_PATCHES:
            cur.execute(statement)
        conn.commit()
    finally:
        cur.close()


def insert_symbol(cur, file_id: int, chunk_id: Optional[int], symbol: dict, embedding, parent_id: Optional[int] = None) -> int:
    cur.execute(
        """INSERT INTO symbols
           (file_id, chunk_id, name, qualified_name, kind, signature, docstring,
            start_line, end_line, parent_id, container_symbol, visibility, is_exported,
            declared_in_extension, is_primary_declaration, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            file_id,
            chunk_id,
            symbol["name"],
            symbol.get("qualified_name"),
            symbol.get("kind", "unknown"),
            symbol.get("signature"),
            symbol.get("docstring"),
            symbol["start_line"],
            symbol["end_line"],
            parent_id,
            symbol.get("container_symbol"),
            symbol.get("visibility", "public"),
            symbol.get("is_exported", False),
            symbol.get("declared_in_extension", False),
            symbol.get("is_primary_declaration", True),
            embedding,
        ),
    )
    return cur.fetchone()[0]


def extract_symbol_references(chunks: list[dict]) -> list[dict]:
    references = []

    for chunk_index, chunk in enumerate(chunks):
        source_symbol_name = chunk.get("symbol_name") or chunk.get("parent_symbol")
        seen = set()

        for offset, line in enumerate(chunk["content"].split("\n")):
            line_no = chunk["start_line"] + offset
            for pattern, ref_kind in REFERENCE_PATTERNS:
                for match in pattern.finditer(line):
                    target_name = match.group(1)
                    if (
                        not target_name
                        or target_name in REFERENCE_STOPWORDS
                        or target_name == source_symbol_name
                    ):
                        continue

                    key = (line_no, target_name, ref_kind)
                    if key in seen:
                        continue
                    seen.add(key)
                    references.append({
                        "chunk_index": chunk_index,
                        "source_symbol_name": source_symbol_name,
                        "target_name": target_name,
                        "reference_kind": ref_kind,
                        "line_no": line_no,
                    })

    return references


def _line_number_for_offset(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _chunk_for_line(chunks: list[dict], line_no: int) -> Optional[dict]:
    candidates = [
        chunk
        for chunk in chunks
        if chunk["start_line"] <= line_no <= chunk["end_line"]
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda chunk: chunk["end_line"] - chunk["start_line"])


def _clean_swift_type(type_name: str) -> str:
    cleaned = type_name.strip()
    cleaned = cleaned.rstrip("?!")
    cleaned = re.sub(r"<.*?>", "", cleaned)
    cleaned = cleaned.split(".")[-1]
    return cleaned


def _is_service_like_type(type_name: str) -> bool:
    if not type_name:
        return False
    return type_name.endswith(("Service", "Manager", "Coordinator", "Resolver", "Store"))


def extract_swift_service_edges(content: str, chunks: list[dict]) -> list[dict]:
    """Extract Swift service-style dependency edges from typed properties and initializer injection."""
    typed_members: dict[str, str] = {}
    edges = []
    seen = set()

    for match in SWIFT_TYPED_PROPERTY_RE.finditer(content):
        member_name = match.group(1)
        type_name = _clean_swift_type(match.group(2))
        if not _is_service_like_type(type_name):
            continue
        line_no = _line_number_for_offset(content, match.start())
        owner_chunk = _chunk_for_line(chunks, line_no)
        source_symbol_name = owner_chunk.get("symbol_name") if owner_chunk else None
        if owner_chunk and owner_chunk.get("symbol_type") == "method" and owner_chunk.get("parent_symbol"):
            source_symbol_name = owner_chunk["parent_symbol"]
        typed_members[member_name] = type_name
        key = (line_no, source_symbol_name, type_name, "type_reference")
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source_symbol_name": source_symbol_name,
            "target_name": type_name,
            "kind": "type_reference",
            "line_no": line_no,
        })

    for match in SWIFT_INIT_RE.finditer(content):
        params = match.group(1)
        line_no = _line_number_for_offset(content, match.start())
        owner_chunk = _chunk_for_line(chunks, line_no)
        source_symbol_name = owner_chunk.get("symbol_name") if owner_chunk else None
        if owner_chunk and owner_chunk.get("symbol_type") == "method" and owner_chunk.get("parent_symbol"):
            source_symbol_name = owner_chunk["parent_symbol"]

        for param_match in SWIFT_PARAM_RE.finditer(params):
            param_name = param_match.group(1)
            type_name = _clean_swift_type(param_match.group(2))
            if not _is_service_like_type(type_name):
                continue
            typed_members.setdefault(param_name, type_name)
            key = (line_no, source_symbol_name, type_name, "injection")
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "source_symbol_name": source_symbol_name,
                "target_name": type_name,
                "kind": "injection",
                "line_no": line_no,
            })

    for match in SWIFT_MEMBER_CALL_RE.finditer(content):
        member_name = match.group(1)
        type_name = typed_members.get(member_name)
        if not type_name:
            continue
        line_no = _line_number_for_offset(content, match.start())
        owner_chunk = _chunk_for_line(chunks, line_no)
        source_symbol_name = owner_chunk.get("symbol_name") if owner_chunk else None
        if owner_chunk and owner_chunk.get("parent_symbol"):
            source_symbol_name = owner_chunk["parent_symbol"]
        key = (line_no, source_symbol_name, type_name, "service_usage")
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source_symbol_name": source_symbol_name,
            "target_name": type_name,
            "kind": "service_usage",
            "line_no": line_no,
        })

    return edges


def resolve_target_symbol(cur, target_name: str) -> tuple[Optional[int], Optional[int]]:
    cur.execute(
        """
        SELECT s.id, s.file_id
        FROM symbols s
        WHERE lower(s.name) = lower(%s)
        ORDER BY
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
            s.is_exported DESC,
            s.start_line
        LIMIT 1
        """,
        (target_name,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


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
    return filter_gitignored_paths(files, repo_root)


def normalize_result_status(status: Optional[str]) -> str:
    """@brief Normalize per-file status to a summary counter key.

    @param status Raw status label returned by a worker result.
    @return One of `indexed`, `skipped`, or `errors`.
    """
    if status in {"indexed", "skipped"}:
        return status
    return "errors"


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
    @return Result dictionary containing status, optional counters/error details,
            and optional classifier warning messages under `warnings`.
    """
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
        classifier_warnings: list[str] = []

        if no_classify:
            file_summary, file_role = "", "unknown"
        else:
            file_summary, file_role = classifier.analyze_file(
                rel_path,
                content[:3000],
                language,
                on_warning=classifier_warnings.append,
            )
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
            cur.execute("DELETE FROM symbol_references WHERE source_file_id = %s", (file_id,))
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
            return {
                "status": "indexed",
                "path": rel_path,
                "chunks": 0,
                "symbols": 0,
                "warnings": classifier_warnings,
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
        if no_classify:
            chunk_classifications = [("utility", "")] * len(chunks)
        else:
            chunk_classifications = classifier.classify_chunks_batch(
                chunks,
                language,
                rel_path,
                on_warning=classifier_warnings.append,
            )
        # ------------------------------------------------------------------

        chunk_count = 0
        symbol_count = 0
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

        # Extract and store dependencies
        deps = chunker.extract_dependencies(content, language, rel_path)
        for dep in deps:
            cur.execute(
                """INSERT INTO dependencies (source_file_id, kind, external_module)
                   VALUES (%s, %s, %s)""",
                (file_id, dep["kind"], dep["module"])
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

        # Extract and store lexical/call references for later symbol resolution.
        for reference in extract_symbol_references(chunks):
            cur.execute(
                """INSERT INTO symbol_references
                   (source_file_id, source_chunk_id, source_symbol_name, target_name, reference_kind, line_no)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    file_id,
                    chunk_ids.get(reference["chunk_index"]),
                    reference.get("source_symbol_name"),
                    reference["target_name"],
                    reference["reference_kind"],
                    reference["line_no"],
                ),
            )

        conn.commit()
        return {
            "status": "indexed",
            "path": rel_path,
            "chunks": chunk_count,
            "symbols": symbol_count,
            "warnings": classifier_warnings,
        }

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "status": "error",
            "path": rel_path,
            "error": str(e),
            "warnings": classifier_warnings if "classifier_warnings" in locals() else [],
        }
    finally:
        db_pool.putconn(conn)


@click.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--config", default="codebrain.toml", help="Config file path")
@click.option("--force", is_flag=True, help="Re-index all files regardless of hash")
@click.option("--watch", is_flag=True, help="Watch for changes and re-index")
@click.option("--workers", default=None, type=int, help="Override worker count")
@click.option("--no-classify", is_flag=True, help="Skip LLM classification (embed only, much faster)")
@click.option("--debug", is_flag=True, help="Print per-file error details during ingestion")
def main(
    repo_path: str,
    config: str,
    force: bool,
    watch: bool,
    workers: Optional[int],
    no_classify: bool,
    debug: bool,
):
    """@brief Ingest a repository into CodeBrain.

    @param repo_path Repository path to index.
    @param config Configuration file path.
    @param force Re-index files even when hashes match.
    @param watch Keep watching and re-index changed files.
    @param workers Optional worker override.
    @param no_classify Skip classifier calls.
    @param debug Print per-file errors and worker failures.
    """
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
    if debug:
        console.print("  Debug: [bold]enabled[/]")
        embed_base_url = (
            cfg.get("embeddings", {}).get("base_url")
            or cfg.get("embeddings", {}).get("ollama_url")
            or "http://localhost:11434"
        )
        console.print(f"  Embedding base URL: {embed_base_url}")
        console.print(f"  Classifier base URL: {cfg.get('classifier', {}).get('base_url', '')}")

    # Shared HTTP clients (thread-safe); one chunker per thread created below
    embedder = EmbeddingClient(cfg)
    classifier = IntentClassifier(cfg)

    # Connection pool — one connection slot per worker plus a couple spare
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        1, n_workers + 2, cfg["database"]["url"]
    )

    # Create ingestion run
    setup_conn = get_db(cfg)
    ensure_schema(setup_conn)
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
                    error_path = result.get("path", "<unknown>")
                    for warning in warnings:
                        classifier_warning_details.append((error_path, warning))
                        if debug:
                            console.print(f"  [yellow]![/] [dim]{error_path}[/]: {warning}")
                    stats["classifier_fallbacks"] += len(warnings)
                progress.update(
                    task, advance=1,
                    description=f"[dim]{result.get('path', '')[:60]}[/]"
                )

    if error_details:
        console.print("\n  [bold red]Error samples:[/]")
        for error_path, error_msg in error_details[:5]:
            console.print(f"  [red]✗[/] [dim]{error_path}[/]: {error_msg}")
        if len(error_details) > 5:
            console.print(f"  [dim]... and {len(error_details) - 5} more[/]")

    if classifier_warning_details:
        console.print("\n  [bold yellow]Classifier fallback samples:[/]")
        for warn_path, warn_msg in classifier_warning_details[:5]:
            console.print(f"  [yellow]![/] [dim]{warn_path}[/]: {warn_msg}")
        if len(classifier_warning_details) > 5:
            console.print(f"  [dim]... and {len(classifier_warning_details) - 5} more[/]")

    # Update ingestion run
    finish_conn = get_db(cfg)
    cur = finish_conn.cursor()
    files_processed = stats["indexed"] + stats["skipped"] + stats["errors"]
    cur.execute(
        """UPDATE ingestion_runs
           SET completed_at=NOW(), files_processed=%s, chunks_created=%s,
               symbols_found=%s, status='completed'
           WHERE id=%s""",
        (files_processed, stats["chunks"], stats["symbols"], run_id)
    )
    finish_conn.commit()
    finish_conn.close()
    db_pool.closeall()

    console.print(f"\n[bold green]✓ Done[/]")
    console.print(f"  Files indexed: {stats['indexed']}")
    console.print(f"  Files skipped (unchanged): {stats['skipped']}")
    console.print(f"  Errors: {stats['errors']}")
    console.print(f"  Classifier fallbacks: {stats['classifier_fallbacks']}")
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
                    if (
                        not should_exclude(fpath, repo_root, cfg.get("ingestion", {}).get("exclude", []))
                        and not is_gitignored(fpath, repo_root)
                    ):
                        lang = detect_language(fpath, cfg)
                        if lang:
                            console.print(f"  [dim]Re-indexing {fpath.name}...[/]")
                            watch_result = process_file(
                                fpath,
                                repo_root,
                                repo_name,
                                cfg,
                                embedder,
                                classifier,
                                watch_chunker,
                                watch_pool,
                                no_classify=no_classify,
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
