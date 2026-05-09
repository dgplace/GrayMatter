"""
@file relationships.py
@brief Language-level symbol relationship and Swift service-edge extraction helpers.
"""

import re
from typing import Optional

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
TYPE_ANNOTATION_PRIMITIVES = {
    "any",
    "array",
    "auto",
    "bool",
    "boolean",
    "char",
    "double",
    "float",
    "int",
    "integer",
    "long",
    "never",
    "none",
    "null",
    "number",
    "object",
    "self",
    "short",
    "signed",
    "str",
    "string",
    "this",
    "uint",
    "uintptr_t",
    "unsigned",
    "usize",
    "value",
    "var",
    "void",
}
TS_RETURN_RE = re.compile(r"\)\s*:\s*([^{=]+)")
PYTHON_RETURN_RE = re.compile(r"->\s*([^:]+):")
SWIFT_RETURN_RE = re.compile(r"->\s*([^{]+)")
SCOPED_METHOD_NAME_RE = re.compile(r"([A-Za-z_~][A-Za-z0-9_~]*(?:::[A-Za-z_~][A-Za-z0-9_~]*)*)\s*$")
SWIFT_FIELD_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:\w+\s+)*(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=/{]+)"
)
TS_FIELD_RE = re.compile(
    r"^\s*(?:public|private|protected|readonly|static|final|abstract|\s)*(?:let|const|var)?\s*([A-Za-z_$][A-Za-z0-9_$]*)[!?]?\s*:\s*([^=;{]+)"
)
PYTHON_FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=#]+)")
JAVA_FIELD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|volatile|transient|synchronized|\s)+([A-Za-z_][A-Za-z0-9_<>,.?\[\]]*)\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:[=;])"
)
CSHARP_FIELD_RE = re.compile(
    r"^\s*(?:public|private|protected|internal|static|readonly|volatile|required|sealed|new|partial|\s)+([A-Za-z_][A-Za-z0-9_<>,.?\[\]]*)\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:[=;{])"
)
CPP_FIELD_RE = re.compile(
    r"^\s*(?:mutable|const|constexpr|static|unsigned|signed|long|short|struct|class|\s)*([A-Za-z_][A-Za-z0-9_:<>,*&\[\]\s]*)\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:[=;])"
)


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


def _normalize_type_expression(raw_type: str) -> str:
    """@brief Normalize a type expression before target extraction.

    @param raw_type Raw language-level type annotation.
    @return Normalized expression with wrappers and nullability markers removed.
    """
    normalized = raw_type.strip()
    if not normalized:
        return ""
    normalized = normalized.rstrip(";,")
    normalized = normalized.replace("[]", "")
    normalized = normalized.strip()
    if normalized.endswith("?") or normalized.endswith("!"):
        normalized = normalized[:-1].strip()
    return normalized


def _extract_type_targets(raw_type: str) -> list[tuple[str, Optional[str]]]:
    """@brief Parse normalized type expressions into relationship targets.

    @param raw_type Raw type expression from a return or field annotation.
    @return List of `(target_name, external_module)` tuples.
    """
    normalized = _normalize_type_expression(raw_type)
    if not normalized:
        return []
    normalized = normalized.replace("|", ",").replace("&", ",")
    candidates = _split_top_level_csv(normalized)
    expanded_candidates = list(candidates)
    for candidate in candidates:
        if "<" not in candidate or ">" not in candidate:
            continue
        inner = candidate[candidate.find("<") + 1:candidate.rfind(">")]
        expanded_candidates.extend(_split_top_level_csv(inner))
    targets: list[tuple[str, Optional[str]]] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for candidate in expanded_candidates:
        target_name, external_module = _normalize_relationship_target(candidate)
        if not target_name:
            continue
        if target_name.lower() in TYPE_ANNOTATION_PRIMITIVES:
            continue
        key = (target_name.lower(), external_module.lower() if external_module else None)
        if key in seen:
            continue
        seen.add(key)
        targets.append((target_name, external_module))
    return targets


def _extract_return_type_signature(signature: str, language: str) -> Optional[str]:
    """@brief Extract a function/method return type annotation from a signature line.

    @param signature Declaration signature.
    @param language Normalized language label.
    @return Return-type expression string when found, otherwise None.
    """
    if language == "typescript":
        match = TS_RETURN_RE.search(signature)
        return match.group(1).strip() if match else None
    if language == "python":
        match = PYTHON_RETURN_RE.search(signature)
        return match.group(1).strip() if match else None
    if language == "swift":
        match = SWIFT_RETURN_RE.search(signature)
        return match.group(1).strip() if match else None
    if language not in {"java", "csharp", "cpp"} or "(" not in signature:
        return None

    prefix = signature.split("(", 1)[0].strip()
    name_match = SCOPED_METHOD_NAME_RE.search(prefix)
    if not name_match:
        return None
    raw_return = prefix[:name_match.start()].strip()
    if not raw_return:
        return None
    raw_tokens = [token for token in raw_return.split() if token.lower() not in RELATIONSHIP_MODIFIER_TOKENS]
    if not raw_tokens:
        return None
    return " ".join(raw_tokens)


def _extract_field_type_from_line(line: str, language: str) -> Optional[str]:
    """@brief Extract a field/property type annotation from a declaration line.

    @param line Source line text.
    @param language Normalized language label.
    @return Field/property type expression when present, otherwise None.
    """
    matchers = {
        "typescript": TS_FIELD_RE,
        "python": PYTHON_FIELD_RE,
        "java": JAVA_FIELD_RE,
        "csharp": CSHARP_FIELD_RE,
        "cpp": CPP_FIELD_RE,
        "swift": SWIFT_FIELD_RE,
    }
    matcher = matchers.get(language)
    if matcher is None:
        return None
    match = matcher.search(line)
    if not match:
        return None
    captured = match.group(2 if language in {"typescript", "python", "swift"} else 1).strip()
    if language in {"java", "csharp", "cpp"} and captured.lower() in {"class", "struct", "interface", "enum", "record", "delegate"}:
        return None
    return captured


def _extract_type_annotation_edges(chunks: list[dict], language: str) -> list[dict]:
    """@brief Extract returns/field_type edges from symbol signatures and bodies.

    @param chunks Parsed chunk records from the chunker.
    @param language Normalized language label for the file.
    @return Relationship rows with `returns` or `field_type` kinds.
    """
    edges: list[dict] = []
    seen: set[tuple[Optional[str], str, str, Optional[str], int]] = set()

    def add_edge(source_symbol_name: Optional[str], relationship_kind: str, target_type: str, line_no: int) -> None:
        if not source_symbol_name:
            return
        for target_name, external_module in _extract_type_targets(target_type):
            dedupe_key = (
                source_symbol_name,
                relationship_kind,
                target_name.lower(),
                external_module.lower() if external_module else None,
                int(line_no),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            edges.append(
                {
                    "source_symbol_name": source_symbol_name,
                    "relationship_kind": relationship_kind,
                    "target_name": target_name,
                    "external_module": external_module,
                    "line_no": int(line_no),
                }
            )

    for chunk in chunks:
        symbol_name = chunk.get("symbol_name")
        symbol_type = chunk.get("symbol_type")
        signature = chunk.get("signature")
        start_line = int(chunk.get("start_line") or 0)
        if symbol_name and signature:
            if symbol_type in {"function", "method"}:
                return_type = _extract_return_type_signature(signature, language)
                if return_type:
                    add_edge(symbol_name, "returns", return_type, start_line)
            if symbol_type in {"property", "variable"}:
                field_type = _extract_field_type_from_line(signature, language)
                if field_type:
                    add_edge(symbol_name, "field_type", field_type, start_line)

        if symbol_type in {"class", "struct", "interface", "protocol", "extension"} and symbol_name:
            for offset, line in enumerate(chunk.get("content", "").splitlines()):
                field_type = _extract_field_type_from_line(line, language)
                if field_type:
                    add_edge(symbol_name, "field_type", field_type, start_line + offset)

        for member_symbol in chunk.get("member_symbols", []):
            member_name = member_symbol.get("symbol_name")
            member_type = member_symbol.get("symbol_type")
            member_signature = member_symbol.get("signature")
            member_line = int(member_symbol.get("start_line") or 0)
            if not member_name or not member_signature:
                continue
            if member_type in {"function", "method"}:
                return_type = _extract_return_type_signature(member_signature, language)
                if return_type:
                    add_edge(member_name, "returns", return_type, member_line)
            if member_type in {"property", "variable"}:
                field_type = _extract_field_type_from_line(member_signature, language)
                if field_type:
                    add_edge(member_name, "field_type", field_type, member_line)

    return edges


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

    for edge in _extract_type_annotation_edges(chunks, language):
        if edge.get("source_symbol_name") is None:
            continue
        dedupe_key = (
            edge["source_symbol_name"],
            edge["relationship_kind"],
            edge["target_name"].lower(),
            edge["external_module"].lower() if edge["external_module"] else None,
            int(edge["line_no"]),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        relationships.append(edge)

    return relationships
