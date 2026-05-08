#!./.venv/bin/python3
"""
@file ingest.py
@brief CodeBrain ingestion pipeline entrypoint and persistence helpers.

Walks a codebase, parses with tree-sitter, embeds content, classifies intent,
and stores normalized metadata in PostgreSQL. Supports one-shot, forced, and
watch-mode indexing flows.
"""

# Large-file justification: this module still owns the CLI entrypoint, watch-mode
# wiring, schema bootstrap, and per-file transaction boundary so ingestion
# behavior remains in one place while pipeline stages are being split out.

import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    import tomllib
except ImportError:
    import tomli as tomllib

# Allow direct script execution (`codebrain/ingest.py ...`) by ensuring the
# repo root is on sys.path before importing sibling package modules.
if __package__ in (None, ""):
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from codebrain.chunker import ASTChunker
from codebrain.classifier import IntentClassifier
from codebrain.embedder import EmbeddingClient
import resolver

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
        target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
        resolution_confidence REAL,
        resolution_method TEXT,
        reference_kind TEXT NOT NULL,
        reference_kind_v2 TEXT,
        line_no INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS resolution_confidence REAL
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS resolution_method TEXT
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT
    """,
    """
    UPDATE symbol_references
    SET reference_kind_v2 = reference_kind
    WHERE reference_kind_v2 IS NULL
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
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol
    ON symbol_references(target_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_reverse_lookup
    ON symbol_references(target_symbol_id, source_file_id, source_symbol_name)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind
    ON symbol_references(target_name, reference_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name
    ON symbols(file_id, is_primary_declaration, name)
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_relationships (
        id SERIAL PRIMARY KEY,
        source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
        relationship_kind TEXT NOT NULL,
        target_name TEXT NOT NULL,
        external_module TEXT,
        line_no INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS relationship_kind TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS target_name TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS external_module TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS line_no INTEGER
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS imported_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS imported_name TEXT
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS local_alias TEXT
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS is_external BOOLEAN
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS external_version TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deps_target_symbol
    ON dependencies(target_symbol_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deps_reverse_lookup
    ON dependencies(target_symbol_id, source_file_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS dependency_cycles (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        cycle_hash TEXT NOT NULL,
        member_file_ids INTEGER[] NOT NULL,
        member_paths TEXT[] NOT NULL,
        cycle_size INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(repo, cycle_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dependency_cycles_repo
    ON dependency_cycles(repo)
    """,
    """
    CREATE OR REPLACE FUNCTION impact_of(
        input_symbol_id  INTEGER,
        max_depth        INTEGER,
        min_confidence   REAL DEFAULT 0.55
    )
    RETURNS TABLE (
        affected_symbol_id      INTEGER,
        affected_file_id        INTEGER,
        affected_file_path      TEXT,
        affected_symbol_name    TEXT,
        depth                   INTEGER,
        edge_kind               TEXT,
        path_min_confidence     REAL
    ) AS $$
    BEGIN
        RETURN QUERY
        WITH RECURSIVE reverse_edges AS (
            SELECT
                sr.target_symbol_id,
                sr.source_symbol_id,
                sr.relationship_kind AS edge_kind,
                1.0::REAL AS edge_confidence
            FROM symbol_relationships sr
            WHERE sr.target_symbol_id IS NOT NULL

            UNION ALL

            SELECT
                refs.target_symbol_id,
                source_symbols.source_symbol_id,
                COALESCE(refs.reference_kind_v2, refs.reference_kind) AS edge_kind,
                COALESCE(refs.resolution_confidence, 0.55)::REAL AS edge_confidence
            FROM symbol_references refs
            JOIN LATERAL (
                SELECT s.id AS source_symbol_id
                FROM symbols s
                WHERE s.file_id = refs.source_file_id
                  AND refs.source_symbol_name IS NOT NULL
                  AND lower(s.name) = lower(refs.source_symbol_name)
                ORDER BY
                    CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
                    s.start_line
                LIMIT 1
            ) source_symbols ON TRUE
            WHERE refs.target_symbol_id IS NOT NULL

            UNION ALL

            SELECT
                d.target_symbol_id,
                d.source_symbol_id,
                d.kind AS edge_kind,
                1.0::REAL AS edge_confidence
            FROM dependencies d
            WHERE d.target_symbol_id IS NOT NULL
              AND d.source_symbol_id IS NOT NULL
        ),
        walk AS (
            SELECT
                input_symbol_id AS symbol_id,
                0 AS depth,
                NULL::TEXT AS edge_kind,
                1.0::REAL AS path_min_confidence,
                ARRAY[input_symbol_id]::INTEGER[] AS visited

            UNION ALL

            SELECT
                re.source_symbol_id AS symbol_id,
                walk.depth + 1 AS depth,
                re.edge_kind,
                LEAST(walk.path_min_confidence, re.edge_confidence)::REAL AS path_min_confidence,
                walk.visited || re.source_symbol_id AS visited
            FROM walk
            JOIN reverse_edges re
              ON re.target_symbol_id = walk.symbol_id
            WHERE walk.depth < max_depth
              AND re.source_symbol_id IS NOT NULL
              AND re.edge_confidence >= min_confidence
              AND NOT re.source_symbol_id = ANY(walk.visited)
        )
        SELECT
            s.id AS affected_symbol_id,
            f.id AS affected_file_id,
            f.path AS affected_file_path,
            s.name AS affected_symbol_name,
            walk.depth,
            walk.edge_kind,
            walk.path_min_confidence
        FROM walk
        JOIN symbols s ON s.id = walk.symbol_id
        JOIN files f ON f.id = s.file_id
        WHERE walk.depth > 0
        ORDER BY walk.depth, walk.path_min_confidence DESC, f.path, s.name;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_file
    ON symbol_relationships(source_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_symbol
    ON symbol_relationships(source_symbol_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_symbol
    ON symbol_relationships(target_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_reverse_lookup
    ON symbol_relationships(target_symbol_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_kind
    ON symbol_relationships(relationship_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_name
    ON symbol_relationships(target_name)
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

SWIFT_TYPED_PROPERTY_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:\w+\s+)*(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_<>.?[\]]*)",
    re.MULTILINE,
)
SWIFT_INIT_RE = re.compile(r"\binit\s*\((.*?)\)", re.DOTALL)
SWIFT_PARAM_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_<>.?[\]]*)"
)
SWIFT_MEMBER_CALL_RE = re.compile(r"\b([a-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
TS_EXTENDS_RE = re.compile(r"\bextends\s+([^{}]+?)(?:\bimplements\b|{)")
TS_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([^{}]+?){")
PY_CLASS_BASES_RE = re.compile(r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)\s*:", re.IGNORECASE)
JAVA_EXTENDS_RE = re.compile(r"\bextends\s+([^\s{]+)")
JAVA_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([^{}]+?){")
CSHARP_BASES_RE = re.compile(r"\b(?:class|struct|record|interface)\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^{}]+?){")
CPP_BASES_RE = re.compile(r"\b(?:class|struct)\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^{}]+?){")
SWIFT_INHERIT_RE = re.compile(
    r"\b(?:class|struct|enum|protocol|extension)\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^{}]+?)(?:where\b|{)"
)
RELATIONSHIP_MODIFIER_TOKENS = {
    "public",
    "private",
    "protected",
    "internal",
    "fileprivate",
    "open",
    "final",
    "abstract",
    "sealed",
    "static",
    "virtual",
    "override",
    "partial",
    "new",
    "readonly",
    "mutating",
    "nonmutating",
}


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
    """@brief Compatibility wrapper for resolver-owned lexical reference extraction.

    @param chunks Chunk dictionaries emitted by the parser/chunker stage.
    @return Extracted lexical reference records.
    """
    return resolver.extract_symbol_references(chunks)


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


def _split_top_level_csv(raw: str) -> list[str]:
    """@brief Split a comma-separated type list while preserving nested generic groups.

    @param raw Raw inheritance/conformance clause fragment.
    @return Top-level comma-delimited tokens.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    pairs = {"<": ">", "(": ")", "[": "]"}
    closing = set(pairs.values())

    for char in raw:
        if char in pairs:
            depth += 1
            current.append(char)
            continue
        if char in closing:
            depth = max(depth - 1, 0)
            current.append(char)
            continue
        if char == "," and depth == 0:
            token = "".join(current).strip()
            if token:
                parts.append(token)
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _strip_generic_args(raw: str) -> str:
    """@brief Remove top-level generic argument groups from a type expression.

    @param raw Type expression that may include `<...>` generic arguments.
    @return Generic-stripped expression.
    """
    cleaned: list[str] = []
    depth = 0
    for char in raw:
        if char == "<":
            depth += 1
            continue
        if char == ">":
            depth = max(depth - 1, 0)
            continue
        if depth == 0:
            cleaned.append(char)
    return "".join(cleaned)


def _normalize_relationship_target(raw_target: str) -> tuple[Optional[str], Optional[str]]:
    """@brief Normalize an extracted inheritance token into name + optional module.

    @param raw_target Raw token captured from a language-specific inheritance clause.
    @return Tuple of `(target_name, external_module)` where each value may be None.
    """
    candidate = raw_target.strip().rstrip("{").rstrip(":").strip()
    if not candidate:
        return None, None

    if "(" in candidate and candidate.endswith(")"):
        candidate = candidate.split("(", 1)[0].strip()
    candidate = _strip_generic_args(candidate)
    candidate = candidate.replace("&", " ").replace("*", " ").strip()
    candidate = candidate.rstrip("?!").replace("[]", "")
    tokens = [part for part in candidate.split() if part.lower() not in RELATIONSHIP_MODIFIER_TOKENS]
    if not tokens:
        return None, None

    normalized = tokens[-1].strip()
    if not normalized:
        return None, None

    external_module = None
    if "::" in normalized:
        namespace, _, symbol = normalized.rpartition("::")
        external_module = namespace or None
        normalized = symbol
    elif "." in normalized:
        namespace, _, symbol = normalized.rpartition(".")
        external_module = namespace or None
        normalized = symbol

    normalized = normalized.strip()
    if not normalized:
        return None, external_module

    return normalized, external_module


def _relationship_kind_for_list_index(language: str, symbol_type: Optional[str], index: int) -> str:
    """@brief Choose a structural edge kind for inheritance-style lists.

    @param language Language name for the active declaration.
    @param symbol_type Parsed symbol type for the declaration.
    @param index Zero-based index inside the inheritance list.
    @return Relationship kind (`extends`, `implements`, or `mixin`).
    """
    if language == "python":
        return "extends" if index == 0 else "mixin"
    if language == "swift":
        if symbol_type == "class":
            return "extends" if index == 0 else "implements"
        if symbol_type == "protocol":
            return "extends"
        return "implements"
    if language == "csharp":
        if symbol_type == "interface":
            return "extends"
        return "extends" if index == 0 else "implements"
    if language == "cpp":
        return "extends" if index == 0 else "mixin"
    return "extends" if index == 0 else "implements"


def extract_symbol_relationships(chunks: list[dict], language: Optional[str]) -> list[dict]:
    """@brief Extract inheritance/implements/mixin edges from declaration signatures.

    @param chunks Parsed chunk records from the chunker.
    @param language Normalized language label for the file.
    @return Structural relationship rows ready for persistence.
    """
    if not language:
        return []

    relationships = []
    seen: set[tuple[Optional[str], str, str, Optional[str], int]] = set()

    for chunk in chunks:
        symbol_name = chunk.get("symbol_name")
        symbol_type = chunk.get("symbol_type")
        signature = chunk.get("signature")
        if not symbol_name or not signature:
            continue
        if symbol_type not in {"class", "struct", "interface", "protocol", "extension", "enum"}:
            continue

        extracted: list[tuple[str, str]] = []

        if language in {"typescript", "javascript"}:
            extends_match = TS_EXTENDS_RE.search(signature)
            if extends_match:
                for token in _split_top_level_csv(extends_match.group(1)):
                    kind = "mixin" if "(" in token else "extends"
                    extracted.append((kind, token))
            implements_match = TS_IMPLEMENTS_RE.search(signature)
            if implements_match:
                for token in _split_top_level_csv(implements_match.group(1)):
                    extracted.append(("implements", token))
        elif language == "python":
            bases_match = PY_CLASS_BASES_RE.search(signature)
            if bases_match:
                for idx, token in enumerate(_split_top_level_csv(bases_match.group(1))):
                    extracted.append((_relationship_kind_for_list_index(language, symbol_type, idx), token))
        elif language == "java":
            if symbol_type == "interface":
                extends_match = JAVA_EXTENDS_RE.search(signature)
                if extends_match:
                    for token in _split_top_level_csv(extends_match.group(1)):
                        extracted.append(("extends", token))
            else:
                extends_match = JAVA_EXTENDS_RE.search(signature)
                if extends_match:
                    extracted.append(("extends", extends_match.group(1)))
                implements_match = JAVA_IMPLEMENTS_RE.search(signature)
                if implements_match:
                    for token in _split_top_level_csv(implements_match.group(1)):
                        extracted.append(("implements", token))
        elif language == "csharp":
            bases_match = CSHARP_BASES_RE.search(signature)
            if bases_match:
                for idx, token in enumerate(_split_top_level_csv(bases_match.group(1))):
                    extracted.append((_relationship_kind_for_list_index(language, symbol_type, idx), token))
        elif language == "cpp":
            bases_match = CPP_BASES_RE.search(signature)
            if bases_match:
                for idx, token in enumerate(_split_top_level_csv(bases_match.group(1))):
                    extracted.append((_relationship_kind_for_list_index(language, symbol_type, idx), token))
        elif language == "swift":
            inherit_match = SWIFT_INHERIT_RE.search(signature)
            if inherit_match:
                for idx, token in enumerate(_split_top_level_csv(inherit_match.group(1))):
                    extracted.append((_relationship_kind_for_list_index(language, symbol_type, idx), token))

        for relationship_kind, raw_target in extracted:
            target_name, external_module = _normalize_relationship_target(raw_target)
            if not target_name:
                continue
            dedupe_key = (
                symbol_name,
                relationship_kind,
                target_name.lower(),
                external_module.lower() if external_module else None,
                int(chunk["start_line"]),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            relationships.append(
                {
                    "source_symbol_name": symbol_name,
                    "relationship_kind": relationship_kind,
                    "target_name": target_name,
                    "external_module": external_module,
                    "line_no": int(chunk["start_line"]),
                }
            )

    return relationships


def resolve_target_symbol(cur, target_name: str) -> tuple[Optional[int], Optional[int]]:
    """@brief Compatibility wrapper for resolver-owned symbol lookup.

    @param cur Open database cursor.
    @param target_name Symbol name to resolve.
    @return Tuple of `(target_symbol_id, target_file_id)`, both nullable.
    """
    return resolver.resolve_target_symbol(cur, target_name)


def _candidate_internal_import_paths(
    source_rel_path: str,
    module: str,
    language: Optional[str],
) -> list[str]:
    """@brief Build repository-relative candidate file paths for an import.

    @param source_rel_path Source file path relative to the repository root.
    @param module Imported module token from the parser.
    @param language Language label for import semantics.
    @return Ordered candidate file paths that could back the import.
    """
    if not module:
        return []

    if language in {"typescript", "javascript", "tsx", "jsx"}:
        if not module.startswith("."):
            return []
        source_dir = posixpath.dirname(source_rel_path)
        base = posixpath.normpath(posixpath.join(source_dir, module))
        candidates = [base]
        if not posixpath.splitext(base)[1]:
            for extension in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
                candidates.append(f"{base}{extension}")
            for extension in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
                candidates.append(posixpath.join(base, f"index{extension}"))
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language == "java":
        base = module.replace(".", "/")
        if not base:
            return []
        return [f"{base}.java"]

    if language in {"c", "cpp"}:
        source_dir = posixpath.dirname(source_rel_path)
        if module.startswith("/"):
            normalized = posixpath.normpath(module.lstrip("/"))
            return [normalized] if normalized and not normalized.startswith("../") else []
        candidates = [
            posixpath.normpath(posixpath.join(source_dir, module)),
            posixpath.normpath(module),
        ]
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language in {"csharp", "swift"}:
        base = module.replace(".", "/")
        candidates = [base] if base else []
        if base and not posixpath.splitext(base)[1]:
            if language == "csharp":
                candidates.append(f"{base}.cs")
            else:
                candidates.append(f"{base}.swift")
                candidates.append(posixpath.join("Sources", base, f"{module}.swift"))
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language != "python":
        return []

    module_dots = 0
    while module.startswith("."):
        module_dots += 1
        module = module[1:]

    if module_dots > 0:
        base_dir = posixpath.dirname(source_rel_path)
        for _ in range(max(module_dots - 1, 0)):
            base_dir = posixpath.dirname(base_dir)
        module_path = module.replace(".", "/")
        base = posixpath.normpath(posixpath.join(base_dir, module_path)) if module_path else base_dir
    else:
        module_path = module.replace(".", "/")
        if not module_path:
            return []
        base = posixpath.normpath(module_path)

    if not base or base.startswith("../"):
        return []
    return [f"{base}.py", posixpath.join(base, "__init__.py")]


def _resolve_internal_import_target_file_id(
    cur,
    repo_name: str,
    source_rel_path: str,
    module: str,
    language: Optional[str],
) -> Optional[int]:
    """@brief Resolve a dependency module token to an internal target file id.

    @param cur Open database cursor.
    @param repo_name Repository identifier.
    @param source_rel_path Source file path relative to repo root.
    @param module Imported module token.
    @param language Source file language.
    @return Internal target file id when found, otherwise None.
    """
    for candidate in _candidate_internal_import_paths(source_rel_path, module, language):
        cur.execute(
            """
            SELECT id
            FROM files
            WHERE repo = %s
              AND (
                  path = %s
                  OR path LIKE %s
              )
            LIMIT 1
            """,
            (
                repo_name,
                candidate,
                f"{candidate}.%",
            ),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
    return None


def _resolve_imported_symbol_id(
    cur,
    target_file_id: Optional[int],
    imported_name: Optional[str],
) -> Optional[int]:
    """@brief Resolve an imported exported symbol inside a target file.

    @param cur Open database cursor.
    @param target_file_id Internal target file id for the import module.
    @param imported_name Imported exported symbol name.
    @return Symbol id when the imported symbol resolves, otherwise None.
    """
    if target_file_id is None or not imported_name or imported_name in {"*", "default"}:
        return None
    cur.execute(
        """
        SELECT id
        FROM symbols
        WHERE file_id = %s
          AND lower(name) = lower(%s)
          AND is_exported = TRUE
        ORDER BY
            CASE WHEN is_primary_declaration THEN 0 ELSE 1 END,
            CASE WHEN declared_in_extension THEN 1 ELSE 0 END,
            start_line
        LIMIT 1
        """,
        (target_file_id, imported_name),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


@lru_cache(maxsize=32)
def _manifest_versions(repo_root_str: str) -> dict[str, dict[str, str]]:
    """@brief Build per-ecosystem package-version maps from repository manifests.

    @param repo_root_str Absolute repository root path string.
    @return Mapping keyed by ecosystem name (`npm`, `pip`, `maven`).
    """
    repo_root = Path(repo_root_str)
    return {
        "npm": _npm_manifest_versions(repo_root),
        "pip": _pip_manifest_versions(repo_root),
        "maven": _maven_manifest_versions(repo_root),
    }


def _npm_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse npm dependency versions from package.json files.

    @param repo_root Repository root path.
    @return Mapping of npm package name to declared version specifier.
    """
    versions: dict[str, str] = {}
    for package_json in repo_root.rglob("package.json"):
        if "node_modules" in package_json.parts:
            continue
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        for section in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            deps = data.get(section, {})
            if isinstance(deps, dict):
                for name, version in deps.items():
                    if isinstance(name, str) and isinstance(version, str) and name not in versions:
                        versions[name] = version
    return versions


def _pip_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse Python package versions from requirements and pyproject files.

    @param repo_root Repository root path.
    @return Mapping of package name to version or constraint specifier.
    """
    versions: dict[str, str] = {}
    requirement_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*([<>=!~]{1,2}\s*[^;#\s]+)?")

    for requirements_file in repo_root.rglob("requirements.txt"):
        if ".venv" in requirements_file.parts:
            continue
        try:
            for line in requirements_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                    continue
                if "@" in stripped and "://" in stripped:
                    continue
                match = requirement_pattern.match(stripped)
                if not match:
                    continue
                package = match.group(1).lower().replace("_", "-")
                raw_version = (match.group(2) or "").replace(" ", "")
                if package and package not in versions:
                    versions[package] = raw_version or "unversioned"
        except Exception:
            continue

    for pyproject_file in repo_root.rglob("pyproject.toml"):
        try:
            with pyproject_file.open("rb") as handle:
                data = tomllib.load(handle)
        except Exception:
            continue

        project_deps = data.get("project", {}).get("dependencies", [])
        if isinstance(project_deps, list):
            for raw_dep in project_deps:
                if not isinstance(raw_dep, str):
                    continue
                match = requirement_pattern.match(raw_dep)
                if not match:
                    continue
                package = match.group(1).lower().replace("_", "-")
                raw_version = (match.group(2) or "").replace(" ", "")
                if package and package not in versions:
                    versions[package] = raw_version or "unversioned"

        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(poetry_deps, dict):
            for package, raw_version in poetry_deps.items():
                if not isinstance(package, str) or package.lower() == "python":
                    continue
                normalized_package = package.lower().replace("_", "-")
                if isinstance(raw_version, str):
                    versions.setdefault(normalized_package, raw_version or "unversioned")
                elif isinstance(raw_version, dict):
                    version_value = raw_version.get("version")
                    if isinstance(version_value, str):
                        versions.setdefault(normalized_package, version_value or "unversioned")

    return versions


def _maven_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse Maven dependency versions from pom.xml files.

    @param repo_root Repository root path.
    @return Mapping of group-id and group:artifact keys to version strings.
    """
    versions: dict[str, str] = {}
    for pom_file in repo_root.rglob("pom.xml"):
        try:
            root = ET.parse(pom_file).getroot()
        except Exception:
            continue

        namespace_match = re.match(r"^\{(.+)\}", root.tag)
        namespace = {"m": namespace_match.group(1)} if namespace_match else {}
        dependency_query = ".//m:dependencies/m:dependency" if namespace else ".//dependencies/dependency"
        group_query = "m:groupId" if namespace else "groupId"
        artifact_query = "m:artifactId" if namespace else "artifactId"
        version_query = "m:version" if namespace else "version"

        for dep in root.findall(dependency_query, namespace):
            group_node = dep.find(group_query, namespace)
            artifact_node = dep.find(artifact_query, namespace)
            version_node = dep.find(version_query, namespace)
            if group_node is None or artifact_node is None or version_node is None:
                continue
            group_id = (group_node.text or "").strip()
            artifact_id = (artifact_node.text or "").strip()
            version = (version_node.text or "").strip()
            if not group_id or not artifact_id or not version:
                continue
            versions.setdefault(group_id, version)
            versions.setdefault(f"{group_id}:{artifact_id}", version)
    return versions


def _external_package_from_module(module: str, language: Optional[str]) -> str:
    """@brief Normalize an imported module token to an external package name.

    @param module Parsed module token from dependency extraction.
    @param language Source file language.
    @return External package identifier used for storage and version lookup.
    """
    if language in {"typescript", "javascript", "tsx", "jsx"}:
        if module.startswith("@"):
            parts = module.split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else module
        return module.split("/", 1)[0]

    if language == "python":
        return module.split(".", 1)[0].replace("_", "-")

    if language == "java":
        parts = module.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:3])
        return module

    if language in {"c", "cpp"}:
        token = module.strip("<>\"")
        return token.split("/", 1)[0]

    if language in {"csharp", "swift"}:
        return module

    return module


def _external_version_for_package(
    package_name: str,
    module: str,
    language: Optional[str],
    manifest_versions: dict[str, dict[str, str]],
) -> Optional[str]:
    """@brief Resolve external dependency version from manifest maps.

    @param package_name Normalized external package name.
    @param module Full module token.
    @param language Source language.
    @param manifest_versions Cached ecosystem version maps.
    @return Version string when manifest data exists, otherwise None.
    """
    if language in {"typescript", "javascript", "tsx", "jsx"}:
        return manifest_versions.get("npm", {}).get(package_name)

    if language == "python":
        return manifest_versions.get("pip", {}).get(package_name.lower().replace("_", "-"))

    if language == "java":
        maven_versions = manifest_versions.get("maven", {})
        if package_name in maven_versions:
            return maven_versions[package_name]
        prefix_matches = [
            (key, value)
            for key, value in maven_versions.items()
            if ":" not in key and (module == key or module.startswith(f"{key}."))
        ]
        if prefix_matches:
            prefix_matches.sort(key=lambda row: len(row[0]), reverse=True)
            return prefix_matches[0][1]
    return None


def _tarjan_strongly_connected_components(adjacency: dict[int, set[int]]) -> list[list[int]]:
    """@brief Compute strongly connected components for a directed graph.

    @param adjacency Directed graph adjacency keyed by node id.
    @return List of strongly connected components as node-id lists.
    """
    index = 0
    index_map: dict[int, int] = {}
    low_link_map: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[list[int]] = []

    def strong_connect(node: int) -> None:
        nonlocal index
        index_map[node] = index
        low_link_map[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in adjacency.get(node, set()):
            if neighbor not in index_map:
                strong_connect(neighbor)
                low_link_map[node] = min(low_link_map[node], low_link_map[neighbor])
            elif neighbor in on_stack:
                low_link_map[node] = min(low_link_map[node], index_map[neighbor])

        if low_link_map[node] == index_map[node]:
            component: list[int] = []
            while stack:
                current = stack.pop()
                on_stack.remove(current)
                component.append(current)
                if current == node:
                    break
            components.append(component)

    for node in adjacency:
        if node not in index_map:
            strong_connect(node)
    return components


def materialize_dependency_cycles(conn, repo_name: str) -> int:
    """@brief Rebuild dependency cycle rows for a repository using SCC detection.

    @param conn Open database connection.
    @param repo_name Repository identifier.
    @return Number of cycle rows written for the repository.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.source_file_id, sf.path, d.target_file_id, tf.path
        FROM dependencies d
        JOIN files sf ON sf.id = d.source_file_id
        JOIN files tf ON tf.id = d.target_file_id
        WHERE sf.repo = %s
          AND tf.repo = %s
          AND d.target_file_id IS NOT NULL
        """,
        (repo_name, repo_name),
    )
    rows = cur.fetchall()

    adjacency: dict[int, set[int]] = {}
    file_paths: dict[int, str] = {}
    for source_file_id, source_path, target_file_id, target_path in rows:
        source_id = int(source_file_id)
        target_id = int(target_file_id)
        adjacency.setdefault(source_id, set()).add(target_id)
        adjacency.setdefault(target_id, set())
        file_paths[source_id] = source_path
        file_paths[target_id] = target_path

    components = _tarjan_strongly_connected_components(adjacency)
    cycle_rows: list[tuple[str, list[int], list[str], int]] = []
    for component in components:
        component_ids = sorted(component)
        if len(component_ids) == 1:
            node = component_ids[0]
            if node not in adjacency.get(node, set()):
                continue
        member_pairs = sorted(
            ((member_id, file_paths.get(member_id, str(member_id))) for member_id in component_ids),
            key=lambda pair: pair[1],
        )
        member_file_ids = [member_id for member_id, _ in member_pairs]
        member_paths = [path for _, path in member_pairs]
        cycle_hash = hashlib.sha256("\n".join(member_paths).encode("utf-8")).hexdigest()
        cycle_rows.append((cycle_hash, member_file_ids, member_paths, len(member_file_ids)))

    cur.execute("DELETE FROM dependency_cycles WHERE repo = %s", (repo_name,))
    for cycle_hash, member_file_ids, member_paths, cycle_size in cycle_rows:
        cur.execute(
            """
            INSERT INTO dependency_cycles (repo, cycle_hash, member_file_ids, member_paths, cycle_size)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (repo, cycle_hash) DO UPDATE
            SET member_file_ids = EXCLUDED.member_file_ids,
                member_paths = EXCLUDED.member_paths,
                cycle_size = EXCLUDED.cycle_size,
                created_at = NOW()
            """,
            (repo_name, cycle_hash, member_file_ids, member_paths, cycle_size),
        )
    conn.commit()
    return len(cycle_rows)


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
    @return One of `indexed`, `skipped`, `deleted`, or `errors`.
    """
    if status in {"indexed", "skipped", "deleted"}:
        return status
    return "errors"


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
    @return Result dictionary containing status, optional counters/error details,
            and optional processing warning messages under `warnings`.
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
                (language, fpath.stat().st_size, line_count, file_hash,
                 file_summary, file_role, file_embedding, existing[0])
            )
            file_id = existing[0]
            # Order matters under parallel re-ingest: delete from relationship/
            # reference edge tables before symbols/code_chunks so FK cascades are
            # no-ops instead of lock amplification under worker concurrency.
            cur.execute("DELETE FROM symbol_references WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM symbol_relationships WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM dependencies WHERE source_file_id = %s", (file_id,))
            cur.execute("DELETE FROM symbols WHERE file_id = %s", (file_id,))
            cur.execute("DELETE FROM code_chunks WHERE file_id = %s", (file_id,))
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
        if no_classify:
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
            else resolver.build_reference_records(chunks)
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
        return {
            "status": "error",
            "path": rel_path,
            "error": str(e),
            "warnings": processing_warnings if "processing_warnings" in locals() else [],
        }
    finally:
        db_pool.putconn(conn)


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
        no_classify: bool = False,
    ):
        self.repo_root = repo_root
        self.repo_name = repo_name
        self.config = config
        self.embedder = embedder
        self.classifier = classifier
        self.chunker = chunker
        self.db_pool = db_pool
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
            should_exclude(fpath, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            or is_gitignored(fpath, self.repo_root)
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
        # Remove old path (file or directory)
        src_path = Path(event.src_path)
        if not (
            should_exclude(src_path, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            or is_gitignored(src_path, self.repo_root)
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
            # For directories, re-index all files inside the new path
            new_dir_path = Path(event.dest_path)
            for root, _, filenames in os.walk(new_dir_path):
                for fname in filenames:
                    self._handle_change(Path(root) / fname)
        else:
            # Process new path
            self._handle_change(Path(event.dest_path))

    def _handle_change(self, fpath: Path):
        """@brief Re-index a changed file using selective incremental resolution refresh.

        @param fpath Absolute path that changed on disk.
        @return None.
        """
        if (
            not should_exclude(fpath, self.repo_root, self.config.get("ingestion", {}).get("exclude", []))
            and not is_gitignored(fpath, self.repo_root)
        ):
            lang = detect_language(fpath, self.config)
            if lang:
                console.print(f"  [dim]Re-indexing {fpath.name}...[/]")
                watch_result = process_file(
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
    """@brief Remove database records for files that no longer exist on disk.

    @param conn Database connection.
    @param repo_name Repository name.
    @param repo_root Repository root path.
    @param current_files List of files currently present on disk.
    @return List of relative paths that were pruned.
    """
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


def clear_repo_per_file_data(config: dict, repo_name: str) -> None:
    """@brief Serially drop all per-file rows for a repo before a `--force` re-ingest.

    Required to avoid deadlocks under parallel re-ingest. Issuing the five
    DELETEs from a single connection in dependency order means the cascade
    work on `symbol_references` and `symbol_relationships` (from `code_chunks`
    and `symbols`) is fully
    serialized. Subsequent per-file DELETEs in `process_file` then match
    zero rows, take only table-level intent locks, and cannot deadlock.

    @param config Parsed CodeBrain configuration dictionary.
    @param repo_name Repository identifier whose per-file data should be cleared.
    """
    conn = get_db(config)
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

    # Prune stale files from database
    prune_conn = get_db(cfg)
    stale_paths = prune_stale_files(prune_conn, repo_name, repo_root, files)
    if stale_paths:
        console.print(f"  Pruning [bold]{len(stale_paths)}[/] stale files from database")
    prune_conn.close()

    # Under --force, serially pre-clear all per-file rows for this repo before
    # parallel workers run. Concurrent per-file DELETEs on `symbol_references`
    # otherwise deadlock through index-page contention even with disjoint row
    # sets, because the cascade fan-out (code_chunks → symbol_references and
    # symbols → symbol_references SET NULL) interleaves locks across workers.
    # The per-file delete block in process_file becomes a no-op after this.
    if force:
        clear_repo_per_file_data(cfg, repo_name)

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

    if stats["indexed"]:
        console.print("\n  [dim]Refreshing cross-file symbol references...[/]")
        resolve_conn = get_db(cfg)
        try:
            cur = resolve_conn.cursor()
            resolver.refresh_repo_references(cur, repo_name, repo_root=repo_root)
            resolve_conn.commit()
        finally:
            resolve_conn.close()

    console.print("\n  [dim]Materializing dependency cycles...[/]")
    cycle_conn = get_db(cfg)
    try:
        cycle_count = materialize_dependency_cycles(cycle_conn, repo_name)
    finally:
        cycle_conn.close()

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
    console.print(f"  Dependency cycles materialized: {cycle_count}")

    if watch:
        console.print(f"\n[bold cyan]Watching for changes...[/] (Ctrl+C to stop)")

        watch_conn = get_db(cfg)
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


if __name__ == "__main__":
    main()
