"""
@file resolver.py
@brief Reference resolver stage for CodeBrain ingestion.

Builds a uniform resolver-aware symbol reference record shape from parsed chunks,
and supports selective inbound reference refresh for incremental file updates.
"""

import re
from typing import Optional

HEURISTIC_NAME_CONFIDENCE = 0.55
INCREMENTAL_REF_WARNING_THRESHOLD = 50_000
INCREMENTAL_FILE_RATIO_WARNING_THRESHOLD = 0.10

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


def resolve_references(cur, chunks: list[dict]) -> list[dict]:
    """@brief Resolve parsed chunk references into a uniform resolver record shape.

    @param cur Open database cursor.
    @param chunks Chunk dictionaries emitted by the parser/chunker stage.
    @return Resolver records ready for persistence, with target ids, confidence,
            method, and richer reference kind fields populated.
    """
    references = extract_symbol_references(chunks)
    lookup_cache: dict[str, tuple[Optional[int], Optional[int]]] = {}
    resolved_references = []

    for reference in references:
        target_symbol_id, target_file_id = resolve_target_symbol(
            cur,
            reference["target_name"],
            cache=lookup_cache,
        )
        resolution_method = "heuristic_name" if target_symbol_id else "unresolved"
        resolution_confidence = HEURISTIC_NAME_CONFIDENCE if target_symbol_id else 0.0
        resolved_references.append(
            {
                **reference,
                "target_symbol_id": target_symbol_id,
                "target_file_id": target_file_id,
                "resolution_confidence": resolution_confidence,
                "resolution_method": resolution_method,
                "reference_kind_v2": reference["reference_kind"],
            }
        )

    return resolved_references


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
        SELECT sr.id, sr.source_file_id, sr.target_name, sr.reference_kind
        FROM symbol_references sr
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
            "target_name": row[2],
            "reference_kind": row[3],
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


def re_resolve_inbound_references(cur, refresh_plan: Optional[dict]) -> int:
    """@brief Re-resolve only the inbound references captured before a file update.

    @param cur Open database cursor.
    @param refresh_plan Plan returned by `capture_incremental_refresh`.
    @return Number of inbound reference rows updated.
    """
    if not refresh_plan:
        return 0

    rows = refresh_plan.get("rows", [])
    if not rows:
        return 0

    lookup_cache: dict[str, tuple[Optional[int], Optional[int]]] = {}
    updated_rows = 0

    for row in rows:
        target_symbol_id, _ = resolve_target_symbol(
            cur,
            row["target_name"],
            cache=lookup_cache,
        )
        resolution_method = "heuristic_name" if target_symbol_id else "unresolved"
        resolution_confidence = HEURISTIC_NAME_CONFIDENCE if target_symbol_id else 0.0
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
                target_symbol_id,
                resolution_confidence,
                resolution_method,
                row["reference_kind"],
                row["id"],
            ),
        )
        updated_rows += 1

    return updated_rows
