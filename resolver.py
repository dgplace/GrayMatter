"""
@file resolver.py
@brief Reference resolver stage for CodeBrain ingestion.

Builds a uniform resolver-aware symbol reference record shape from parsed chunks,
and supports selective inbound reference refresh for incremental file updates.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

HEURISTIC_NAME_CONFIDENCE = 0.55
EXACT_MATCH_CONFIDENCE = 1.0
INCREMENTAL_REF_WARNING_THRESHOLD = 50_000
INCREMENTAL_FILE_RATIO_WARNING_THRESHOLD = 0.10
SCIP_DEFINITION_SYMBOL_ROLE = 1
SCIP_TYPESCRIPT_LANGUAGES = frozenset({"typescript", "tsx", "javascript", "jsx"})

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


class ReferenceResolverStrategy:
    """@brief Strategy interface for language-specific exact reference resolution."""

    name = "base"
    supported_languages: frozenset[str] = frozenset()

    def supports(self, language: Optional[str]) -> bool:
        """@brief Return whether the strategy can resolve the given language.

        @param language CodeBrain language label for the source file.
        @return True when this strategy can attempt exact resolution.
        """
        return language in self.supported_languages

    def build_exact_match_index(self, repo_root: Path, rows: list[dict]) -> dict[tuple[str, int, str], dict]:
        """@brief Build exact source-reference matches for a resolver batch.

        @param repo_root Repository root being indexed.
        @param rows Resolver batch rows sharing a compatible language.
        @return Mapping from `(source_path, line_no, target_name)` to target
                location metadata.
        """
        raise NotImplementedError


class TypeScriptScipResolverStrategy(ReferenceResolverStrategy):
    """@brief Resolve TypeScript-family references with scip-typescript occurrences."""

    name = "scip_typescript"
    supported_languages = SCIP_TYPESCRIPT_LANGUAGES

    def build_exact_match_index(self, repo_root: Path, rows: list[dict]) -> dict[tuple[str, int, str], dict]:
        """@brief Map TypeScript source references to exact declaration locations.

        @param repo_root Repository root being indexed.
        @param rows Resolver batch rows for TypeScript-family source files.
        @return Exact-match mapping keyed by source path, line number, and target
                symbol name.
        """
        tsconfig_paths = _find_typescript_configs(repo_root)
        if (
            not tsconfig_paths
            or not _has_node_modules(repo_root, tsconfig_paths)
            or not _has_scip_tools()
        ):
            return {}

        scip_index = self._load_scip_index(repo_root)
        definition_index = _build_scip_definition_index(scip_index)
        candidate_paths = {
            _normalize_relative_path(row["source_path"])
            for row in rows
            if row.get("source_path")
        }
        candidate_names = {row["target_name"] for row in rows}
        exact_matches: dict[tuple[str, int, str], dict] = {}

        for document in scip_index.get("documents", []):
            relative_path = _normalize_relative_path(document.get("relative_path", ""))
            if relative_path not in candidate_paths:
                continue

            for occurrence in document.get("occurrences", []):
                symbol_roles = occurrence.get("symbol_roles", 0)
                if symbol_roles & SCIP_DEFINITION_SYMBOL_ROLE:
                    continue

                symbol = occurrence.get("symbol")
                target_name = _extract_scip_target_name(symbol)
                if not symbol or not target_name or target_name not in candidate_names:
                    continue

                target_location = definition_index.get(symbol)
                if not target_location:
                    continue

                key = _reference_key(relative_path, occurrence["range"][0] + 1, target_name)
                exact_matches[key] = {
                    "target_path": target_location["path"],
                    "target_line": target_location["line_no"],
                    "resolution_method": self.name,
                }

        return exact_matches

    def _load_scip_index(self, repo_root: Path) -> dict:
        """@brief Run scip-typescript and print the resulting SCIP index as JSON.

        @param repo_root Repository root being indexed.
        @return Parsed JSON object emitted by `scip print --json`.
        """
        if not _has_scip_tools():
            raise RuntimeError(
                "scip-typescript and scip must be available on PATH; "
                "run ingestion through the codebrain indexer container."
            )

        with tempfile.TemporaryDirectory(prefix="codebrain-scip-") as tmpdir:
            index_path = Path(tmpdir) / "index.scip"
            index_cmd = [
                "scip-typescript",
                "index",
                "--cwd",
                str(repo_root),
                "--output",
                str(index_path),
                "--no-progress-bar",
            ]
            index_result = subprocess.run(
                index_cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if index_result.returncode != 0:
                stderr = (index_result.stderr or index_result.stdout).strip()
                raise RuntimeError(f"scip-typescript index failed for {repo_root}: {stderr}")

            print_cmd = ["scip", "print", "--json", str(index_path)]
            print_result = subprocess.run(
                print_cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if print_result.returncode != 0:
                stderr = (print_result.stderr or print_result.stdout).strip()
                raise RuntimeError(f"scip print failed for {repo_root}: {stderr}")

        return json.loads(print_result.stdout)


RESOLVER_STRATEGIES: tuple[ReferenceResolverStrategy, ...] = (
    TypeScriptScipResolverStrategy(),
)


def _normalize_relative_path(path: str) -> str:
    """@brief Normalize repository-relative paths to a stable POSIX form."""
    return Path(path).as_posix() if path else ""


def _reference_key(source_path: str, line_no: int, target_name: str) -> tuple[str, int, str]:
    """@brief Build a stable lookup key for a source reference row."""
    return (_normalize_relative_path(source_path), line_no, target_name)


def _find_typescript_configs(repo_root: Path) -> list[Path]:
    """@brief Discover tsconfig files while skipping heavy generated directories."""
    configs = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in {".git", "dist", "build", "node_modules"}]
        if "tsconfig.json" in filenames:
            configs.append(Path(current_root) / "tsconfig.json")
    return configs


def _has_scip_tools() -> bool:
    """@brief Return whether the SCIP CLI tools are available on PATH."""
    return shutil.which("scip-typescript") is not None and shutil.which("scip") is not None


def _has_node_modules(repo_root: Path, tsconfig_paths: list[Path]) -> bool:
    """@brief Check whether the repository has installed Node dependencies."""
    if (repo_root / "node_modules").is_dir():
        return True

    for tsconfig_path in tsconfig_paths:
        current = tsconfig_path.parent
        while True:
            if (current / "node_modules").is_dir():
                return True
            if current == repo_root or current.parent == current:
                break
            current = current.parent
    return False


def _extract_scip_target_name(symbol: Optional[str]) -> Optional[str]:
    """@brief Derive a declaration name from a SCIP symbol descriptor."""
    if not symbol:
        return None

    tail = symbol.rsplit("/", 1)[-1]
    if not tail:
        return None

    sanitized = tail.replace("().", "").replace("()", "")
    matches = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sanitized)
    return matches[-1] if matches else None


def _build_scip_definition_index(scip_index: dict) -> dict[str, dict]:
    """@brief Index SCIP definition occurrences by symbol descriptor."""
    definitions: dict[str, dict] = {}
    for document in scip_index.get("documents", []):
        relative_path = _normalize_relative_path(document.get("relative_path", ""))
        for occurrence in document.get("occurrences", []):
            if not occurrence.get("symbol"):
                continue
            if not occurrence.get("symbol_roles", 0) & SCIP_DEFINITION_SYMBOL_ROLE:
                continue
            definitions[occurrence["symbol"]] = {
                "path": relative_path,
                "line_no": occurrence["range"][0] + 1,
            }
    return definitions


def _resolve_symbol_at_location(
    cur,
    repo_name: str,
    target_path: str,
    target_name: str,
    target_line: int,
    cache: Optional[dict[tuple[str, str, int], tuple[Optional[int], Optional[int]]]] = None,
) -> tuple[Optional[int], Optional[int]]:
    """@brief Resolve a declaration by repository path, symbol name, and line.

    @param cur Open database cursor.
    @param repo_name Repository name owning the symbols.
    @param target_path Repository-relative file path.
    @param target_name Declaration name from SCIP.
    @param target_line 1-based declaration line from SCIP.
    @param cache Optional lookup cache reused across a batch.
    @return Tuple of `(target_symbol_id, target_file_id)`, both nullable.
    """
    cache_key = (_normalize_relative_path(target_path), target_name, target_line)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    cur.execute(
        """
        SELECT s.id, s.file_id
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE f.repo = %s
          AND f.path = %s
          AND s.name = %s
          AND s.start_line <= %s
          AND s.end_line >= %s
        ORDER BY
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
            (s.end_line - s.start_line) ASC,
            s.start_line
        LIMIT 1
        """,
        (repo_name, _normalize_relative_path(target_path), target_name, target_line, target_line),
    )
    row = cur.fetchone()
    resolved = (row[0], row[1]) if row else (None, None)
    if cache is not None:
        cache[cache_key] = resolved
    return resolved


def _collect_exact_matches(repo_root: Optional[Path], rows: list[dict]) -> dict[tuple[str, int, str], dict]:
    """@brief Collect exact resolver matches from all applicable strategies."""
    if repo_root is None:
        return {}

    exact_matches: dict[tuple[str, int, str], dict] = {}
    for strategy in RESOLVER_STRATEGIES:
        candidate_rows = [row for row in rows if strategy.supports(row.get("language"))]
        if candidate_rows:
            exact_matches.update(strategy.build_exact_match_index(repo_root, candidate_rows))
    return exact_matches


def _build_resolved_reference(
    reference: dict,
    target_symbol_id: Optional[int],
    target_file_id: Optional[int],
    resolution_confidence: float,
    resolution_method: str,
) -> dict:
    """@brief Build the uniform resolver record returned to ingest callers."""
    resolved = {
        key: value
        for key, value in reference.items()
        if key not in {"source_path", "language"}
    }
    resolved.update(
        {
            "target_symbol_id": target_symbol_id,
            "target_file_id": target_file_id,
            "resolution_confidence": resolution_confidence,
            "resolution_method": resolution_method,
            "reference_kind_v2": reference["reference_kind"],
        }
    )
    return resolved


def _resolve_reference_rows(
    cur,
    rows: list[dict],
    repo_name: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> list[dict]:
    """@brief Resolve a batch of reference rows with exact strategies then fallback."""
    exact_matches = _collect_exact_matches(repo_root, rows)
    exact_lookup_cache: dict[tuple[str, str, int], tuple[Optional[int], Optional[int]]] = {}
    name_lookup_cache: dict[str, tuple[Optional[int], Optional[int]]] = {}
    resolved_rows = []

    for row in rows:
        exact_target_symbol_id = None
        exact_target_file_id = None
        exact_match = exact_matches.get(
            _reference_key(row.get("source_path", ""), row["line_no"], row["target_name"])
        )

        if exact_match and repo_name:
            exact_target_symbol_id, exact_target_file_id = _resolve_symbol_at_location(
                cur,
                repo_name,
                exact_match["target_path"],
                row["target_name"],
                exact_match["target_line"],
                cache=exact_lookup_cache,
            )
            if exact_target_symbol_id:
                resolved_rows.append(
                    _build_resolved_reference(
                        row,
                        exact_target_symbol_id,
                        exact_target_file_id,
                        EXACT_MATCH_CONFIDENCE,
                        exact_match["resolution_method"],
                    )
                )
                continue

        target_symbol_id, target_file_id = resolve_target_symbol(
            cur,
            row["target_name"],
            cache=name_lookup_cache,
        )
        resolution_method = "heuristic_name" if target_symbol_id else "unresolved"
        resolution_confidence = HEURISTIC_NAME_CONFIDENCE if target_symbol_id else 0.0
        resolved_rows.append(
            _build_resolved_reference(
                row,
                target_symbol_id,
                target_file_id,
                resolution_confidence,
                resolution_method,
            )
        )

    return resolved_rows


def extract_symbol_references(chunks: list[dict]) -> list[dict]:
    """@brief Extract lexical/call references from parsed chunks.

    @param chunks Chunk dictionaries emitted by the parser/chunker stage.
    @return Reference records with source symbol, chunk index, target name, kind,
            and line number.
    """
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
                    references.append(
                        {
                            "chunk_index": chunk_index,
                            "source_symbol_name": source_symbol_name,
                            "target_name": target_name,
                            "reference_kind": ref_kind,
                            "line_no": line_no,
                        }
                    )

    return references


def resolve_target_symbol(
    cur,
    target_name: str,
    cache: Optional[dict[str, tuple[Optional[int], Optional[int]]]] = None,
) -> tuple[Optional[int], Optional[int]]:
    """@brief Resolve a target symbol name to a preferred symbol/file id pair.

    @param cur Open database cursor.
    @param target_name Symbol name to resolve.
    @param cache Optional lower-cased name lookup cache reused across a batch.
    @return Tuple of `(target_symbol_id, target_file_id)`, both nullable.
    """
    cache_key = target_name.lower()
    if cache is not None and cache_key in cache:
        return cache[cache_key]

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
    resolved = (row[0], row[1]) if row else (None, None)
    if cache is not None:
        cache[cache_key] = resolved
    return resolved


def resolve_references(
    cur,
    chunks: list[dict],
    language: Optional[str] = None,
    file_path: Optional[str] = None,
    repo_root: Optional[Path] = None,
    repo_name: Optional[str] = None,
) -> list[dict]:
    """@brief Resolve parsed chunk references into a uniform resolver record shape.

    @param cur Open database cursor.
    @param chunks Chunk dictionaries emitted by the parser/chunker stage.
    @param language Language label for the source file.
    @param file_path Repository-relative source path for the file.
    @param repo_root Repository root on disk.
    @param repo_name Repository identifier stored in the database.
    @return Resolver records ready for persistence, with target ids, confidence,
            method, and richer reference kind fields populated.
    """
    references = extract_symbol_references(chunks)
    rows = [
        {
            **reference,
            "source_path": _normalize_relative_path(file_path or ""),
            "language": language,
        }
        for reference in references
    ]
    return _resolve_reference_rows(cur, rows, repo_name=repo_name, repo_root=repo_root)


def build_reference_records(chunks: list[dict]) -> list[dict]:
    """@brief Build unresolved resolver records for later cross-file resolution.

    @param chunks Chunk dictionaries emitted by the parser/chunker stage.
    @return Resolver records with a stable unresolved shape suitable for
            persistence before a later resolution pass.
    """
    references = extract_symbol_references(chunks)
    unresolved_references = []

    for reference in references:
        unresolved_references.append(
            {
                **reference,
                "target_symbol_id": None,
                "target_file_id": None,
                "resolution_confidence": 0.0,
                "resolution_method": "unresolved",
                "reference_kind_v2": reference["reference_kind"],
            }
        )

    return unresolved_references


def refresh_repo_references(cur, repo_name: str, repo_root: Optional[Path] = None) -> int:
    """@brief Re-resolve all symbol references for a repository in a serial pass.

    @param cur Open database cursor.
    @param repo_name Repository whose references should be refreshed.
    @param repo_root Repository root on disk, used by exact resolver strategies.
    @return Number of reference rows updated.
    """
    cur.execute(
        """
        SELECT sr.id, f.path, f.language, sr.target_name, sr.reference_kind, sr.line_no
        FROM symbol_references sr
        JOIN files f ON f.id = sr.source_file_id
        WHERE f.repo = %s
        ORDER BY sr.id
        """,
        (repo_name,),
    )
    rows = [
        {
            "id": row[0],
            "source_path": row[1],
            "language": row[2],
            "target_name": row[3],
            "reference_kind": row[4],
            "line_no": row[5],
        }
        for row in cur.fetchall()
    ]
    if not rows:
        return 0

    resolved_rows = _resolve_reference_rows(cur, rows, repo_name=repo_name, repo_root=repo_root)
    updated_rows = 0

    for row in resolved_rows:
        cur.execute(
            """
            UPDATE symbol_references
            SET target_symbol_id = %s,
                resolution_confidence = %s,
                resolution_method = %s,
                reference_kind_v2 = %s
            WHERE id = %s
            """,
            (
                row["target_symbol_id"],
                row["resolution_confidence"],
                row["resolution_method"],
                row["reference_kind"],
                row["id"],
            ),
        )
        updated_rows += 1

    return updated_rows


def capture_incremental_refresh(cur, repo_name: str, file_id: int) -> dict:
    """@brief Capture inbound references that must be re-resolved after a file update.

    @param cur Open database cursor.
    @param repo_name Repository name owning the changed file.
    @param file_id Database id of the file being updated.
    @return Refresh plan containing impacted reference rows and warning messages.
    """
    cur.execute(
        """
        SELECT id
        FROM symbols
        WHERE file_id = %s
        ORDER BY id
        """,
        (file_id,),
    )
    symbol_ids = [row[0] for row in cur.fetchall()]
    if not symbol_ids:
        return {"rows": [], "warnings": []}

    cur.execute(
        """
        SELECT sr.id, sr.source_file_id, f.path, f.language, sr.target_name, sr.reference_kind, sr.line_no
        FROM symbol_references sr
        JOIN files f ON f.id = sr.source_file_id
        WHERE sr.target_symbol_id = ANY(%s)
          AND sr.source_file_id <> %s
        ORDER BY sr.id
        """,
        (symbol_ids, file_id),
    )
    rows = [
        {
            "id": row[0],
            "source_file_id": row[1],
            "source_path": row[2],
            "language": row[3],
            "target_name": row[4],
            "reference_kind": row[5],
            "line_no": row[6],
        }
        for row in cur.fetchall()
    ]
    if not rows:
        return {"rows": [], "warnings": []}

    cur.execute("SELECT COUNT(*) FROM files WHERE repo = %s", (repo_name,))
    total_repo_files = cur.fetchone()[0] or 0
    affected_source_files = len({row["source_file_id"] for row in rows})
    warnings = []

    if len(rows) > INCREMENTAL_REF_WARNING_THRESHOLD:
        warnings.append(
            "Resolver incremental refresh invalidated "
            f"{len(rows)} inbound refs; continuing with selective re-resolution."
        )
    if total_repo_files and affected_source_files > total_repo_files * INCREMENTAL_FILE_RATIO_WARNING_THRESHOLD:
        warnings.append(
            "Resolver incremental refresh touched "
            f"{affected_source_files}/{total_repo_files} source files; continuing with selective re-resolution."
        )

    return {"rows": rows, "warnings": warnings}


def re_resolve_inbound_references(
    cur,
    refresh_plan: Optional[dict],
    repo_name: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> int:
    """@brief Re-resolve only the inbound references captured before a file update.

    @param cur Open database cursor.
    @param refresh_plan Plan returned by `capture_incremental_refresh`.
    @param repo_name Repository identifier stored in the database.
    @param repo_root Repository root on disk, used by exact resolver strategies.
    @return Number of inbound reference rows updated.
    """
    if not refresh_plan:
        return 0

    rows = refresh_plan.get("rows", [])
    if not rows:
        return 0

    resolved_rows = _resolve_reference_rows(cur, rows, repo_name=repo_name, repo_root=repo_root)
    updated_rows = 0

    for row in resolved_rows:
        cur.execute(
            """
            UPDATE symbol_references
            SET target_symbol_id = %s,
                resolution_confidence = %s,
                resolution_method = %s,
                reference_kind_v2 = %s
            WHERE id = %s
            """,
            (
                row["target_symbol_id"],
                row["resolution_confidence"],
                row["resolution_method"],
                row["reference_kind"],
                row["id"],
            ),
        )
        updated_rows += 1

    return updated_rows
