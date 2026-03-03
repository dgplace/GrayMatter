"""
AST-aware code chunker using tree-sitter.
Splits code at natural boundaries (functions, classes) rather than arbitrary line counts.
"""

import re
from typing import Optional

import tree_sitter

# Language-specific node types that represent top-level symbols
SYMBOL_NODE_TYPES = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": "decorated",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "export_statement": "export",
        "lexical_declaration": "variable",  # const/let at top level
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "export_statement": "export",
        "lexical_declaration": "variable",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "class",
        "enum_item": "enum",
        "impl_item": "impl",
        "trait_item": "interface",
        "type_item": "type",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "function",
        "type_declaration": "type",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "function",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "class",
        "enum_specifier": "enum",
        "type_definition": "type",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "class",
        "enum_specifier": "enum",
        "namespace_definition": "namespace",
    },
    "swift": {
        "function_declaration": "function",
        "class_declaration": "class",
        "protocol_declaration": "interface",
    },
}

# Patterns for extracting imports by language
IMPORT_PATTERNS = {
    "python": [
        (r"^import\s+([\w.]+)", "import"),
        (r"^from\s+([\w.]+)\s+import", "import"),
    ],
    "typescript": [
        (r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", "import"),
        (r"require\(['\"]([^'\"]+)['\"]\)", "import"),
    ],
    "javascript": [
        (r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", "import"),
        (r"require\(['\"]([^'\"]+)['\"]\)", "import"),
    ],
    "rust": [
        (r"use\s+([\w:]+)", "import"),
        (r"extern\s+crate\s+(\w+)", "import"),
    ],
    "go": [
        (r"\"([^\"]+)\"", "import"),  # inside import blocks
    ],
    "java": [
        (r"import\s+([\w.]+)", "import"),
    ],
    "c": [
        (r'#include\s+[<"]([^>"]+)[>"]', "import"),
    ],
    "cpp": [
        (r'#include\s+[<"]([^>"]+)[>"]', "import"),
    ],
    "swift": [
        (r"^import\s+([\w.]+)", "import"),
    ],
}

# Map language names to tree-sitter language modules
LANGUAGE_MODULES = {}

def _get_ts_language(lang: str):
    """Lazily load tree-sitter language."""
    if lang not in LANGUAGE_MODULES:
        try:
            if lang == "python":
                import tree_sitter_python as tsl
            elif lang == "typescript":
                import tree_sitter_typescript as tsl
                LANGUAGE_MODULES[lang] = tree_sitter.Language(tsl.language_typescript())
                return LANGUAGE_MODULES[lang]
            elif lang == "tsx":
                import tree_sitter_typescript as tsl
                LANGUAGE_MODULES[lang] = tree_sitter.Language(tsl.language_tsx())
                return LANGUAGE_MODULES[lang]
            elif lang == "javascript":
                import tree_sitter_javascript as tsl
            elif lang == "rust":
                import tree_sitter_rust as tsl
            elif lang == "go":
                import tree_sitter_go as tsl
            elif lang == "java":
                import tree_sitter_java as tsl
            elif lang == "c":
                import tree_sitter_c as tsl
            elif lang == "cpp":
                import tree_sitter_cpp as tsl
            elif lang == "swift":
                import tree_sitter_swift as tsl
            else:
                return None
            LANGUAGE_MODULES[lang] = tree_sitter.Language(tsl.language())
        except ImportError:
            return None
    return LANGUAGE_MODULES.get(lang)


class ASTChunker:
    def __init__(self, config: dict):
        self.chunk_size = config.get("ingestion", {}).get("chunk_size", 512)
        self.overlap = config.get("ingestion", {}).get("overlap", 64)
        self.parser = tree_sitter.Parser()

    def chunk_file(self, content: str, language: Optional[str], file_path: str) -> list[dict]:
        """
        Split a file into semantically meaningful chunks.
        Uses tree-sitter for supported languages, falls back to line-based chunking.
        """
        if not language:
            return self._fallback_chunk(content, file_path)

        ts_lang = _get_ts_language(language)
        if not ts_lang:
            return self._fallback_chunk(content, file_path)

        try:
            self.parser.language = ts_lang
            tree = self.parser.parse(content.encode("utf-8"))
            return self._ast_chunk(tree, content, language, file_path)
        except Exception:
            return self._fallback_chunk(content, file_path)

    def _ast_chunk(self, tree, content: str, language: str, file_path: str) -> list[dict]:
        """Chunk based on AST structure."""
        lines = content.split("\n")
        chunks = []
        symbol_types = SYMBOL_NODE_TYPES.get(language, {})

        # Collect top-level symbol nodes
        symbols = []
        for child in tree.root_node.children:
            node_type = child.type
            if node_type in symbol_types:
                symbols.append(child)
            # Handle decorated definitions (Python)
            elif node_type == "decorated_definition":
                for sub in child.children:
                    if sub.type in symbol_types:
                        symbols.append(child)  # keep the decorator
                        break

        if not symbols:
            return self._fallback_chunk(content, file_path)

        # Extract "glue" code (imports, constants between symbols)
        covered_lines = set()
        for sym in symbols:
            for line_no in range(sym.start_point[0], sym.end_point[0] + 1):
                covered_lines.add(line_no)

        # Glue chunk: everything NOT covered by a symbol
        glue_lines = []
        glue_start = 0
        for i, line in enumerate(lines):
            if i not in covered_lines:
                if not glue_lines:
                    glue_start = i
                glue_lines.append(line)

        if glue_lines:
            glue_content = "\n".join(glue_lines).strip()
            if glue_content:
                chunks.append({
                    "content": glue_content,
                    "start_line": glue_start + 1,
                    "end_line": glue_start + len(glue_lines),
                    "symbol_name": None,
                    "symbol_type": None,
                })

        # Symbol chunks
        for sym_node in symbols:
            start = sym_node.start_point[0]
            end = sym_node.end_point[0]
            chunk_content = "\n".join(lines[start:end + 1])

            # Extract symbol metadata
            sym_name = self._extract_name(sym_node, language)
            sym_type = self._resolve_symbol_type(sym_node, language, symbol_types)
            if sym_node.type == "decorated_definition":
                for sub in sym_node.children:
                    if sub.type in symbol_types:
                        sym_type = symbol_types[sub.type]
                        if not sym_name:
                            sym_name = self._extract_name(sub, language)
                        break

            docstring = self._extract_docstring(sym_node, content, language)
            signature = self._extract_signature(sym_node, lines, language)
            visibility = self._infer_visibility(sym_name, sym_node, language)
            is_exported = self._is_exported(sym_node, language)

            # If chunk is too large, sub-chunk by methods
            if len(chunk_content.split()) > self.chunk_size * 1.5 and sym_type == "class":
                sub_chunks = self._sub_chunk_class(sym_node, lines, language, file_path, sym_name)
                chunks.extend(sub_chunks)
            else:
                chunks.append({
                    "content": chunk_content,
                    "start_line": start + 1,
                    "end_line": end + 1,
                    "symbol_name": sym_name,
                    "symbol_type": sym_type,
                    "parent_symbol": None,
                    "docstring": docstring,
                    "signature": signature,
                    "qualified_name": f"{file_path}:{sym_name}" if sym_name else None,
                    "visibility": visibility,
                    "is_exported": is_exported,
                })

        return chunks if chunks else self._fallback_chunk(content, file_path)

    def _sub_chunk_class(self, class_node, lines, language, file_path, class_name) -> list[dict]:
        """Break a large class into per-method chunks."""
        chunks = []
        method_types = {
            "function_definition",
            "method_definition",
            "function_declaration",
            "method_declaration",
            "function_item",
            "public_method_definition",
            "protocol_function_declaration",
        }
        body_node_types = {"body", "class_body", "protocol_body", "enum_class_body"}

        for child in class_node.children:
            if child.type in method_types or (
                child.type in body_node_types and hasattr(child, "children")
            ):
                # If it's a body node, look inside for methods
                if child.type in body_node_types:
                    for sub in child.children:
                        if sub.type in method_types:
                            self._add_method_chunk(sub, lines, language, file_path, class_name, chunks)
                else:
                    self._add_method_chunk(child, lines, language, file_path, class_name, chunks)

        return chunks

    def _add_method_chunk(self, node, lines, language, file_path, class_name, chunks):
        start = node.start_point[0]
        end = node.end_point[0]
        name = self._extract_name(node, language)
        chunks.append({
            "content": "\n".join(lines[start:end + 1]),
            "start_line": start + 1,
            "end_line": end + 1,
            "symbol_name": name,
            "symbol_type": "method",
            "parent_symbol": class_name,
            "qualified_name": f"{file_path}:{class_name}.{name}" if name else None,
            "visibility": self._infer_visibility(name, node, language),
            "is_exported": False,
        })

    def _extract_name(self, node, language: str) -> Optional[str]:
        """Extract the name identifier from a syntax node."""
        for child in node.children:
            if child.type in (
                "identifier",
                "property_identifier",
                "type_identifier",
                "simple_identifier",
                "user_type",
            ):
                return child.text.decode("utf-8")
            if child.type == "name":
                return child.text.decode("utf-8")
        for child in node.children:
            nested = self._extract_name(child, language) if child.children else None
            if nested:
                return nested
        return None

    def _extract_docstring(self, node, content: str, language: str) -> Optional[str]:
        """Extract docstring/JSDoc from a symbol node."""
        # Python: first child of body is expression_statement with string
        if language == "python":
            for child in node.children:
                if child.type == "block":
                    for sub in child.children:
                        if sub.type == "expression_statement":
                            for s in sub.children:
                                if s.type == "string":
                                    return s.text.decode("utf-8").strip('"\' \n')
                            break
                    break
        # JS/TS: look for preceding comment node
        if language in ("typescript", "javascript", "tsx", "jsx"):
            prev = node.prev_sibling
            if prev and prev.type == "comment":
                text = prev.text.decode("utf-8")
                if text.startswith("/**"):
                    return text
        return None

    def _extract_signature(self, node, lines: list[str], language: str) -> Optional[str]:
        """Extract the function/class signature (first line or up to opening brace)."""
        start = node.start_point[0]
        first_line = lines[start].strip()
        return first_line if first_line else None

    def _infer_visibility(self, name: Optional[str], node, language: str) -> str:
        if not name:
            return "public"
        if language == "python" and name.startswith("_"):
            return "private" if name.startswith("__") and not name.endswith("__") else "protected"
        if language == "swift":
            modifier = self._find_swift_visibility(node)
            return modifier or "internal"
        if language in ("typescript", "javascript"):
            # Check for access modifiers in children
            for child in node.children:
                if child.type in ("public", "private", "protected"):
                    return child.type
            if name.startswith("#"):
                return "private"
        return "public"

    def _is_exported(self, node, language: str) -> bool:
        if language == "swift":
            return self._find_swift_visibility(node) in {"public", "open"}
        if language in ("typescript", "javascript", "tsx", "jsx"):
            if node.type == "export_statement":
                return True
            parent = node.parent
            if parent and parent.type == "export_statement":
                return True
        return False

    def _resolve_symbol_type(self, node, language: str, symbol_types: dict[str, str]) -> str:
        sym_type = symbol_types.get(node.type, "unknown")
        if language != "swift" or node.type != "class_declaration":
            return sym_type

        keyword_map = {
            "class": "class",
            "struct": "class",
            "extension": "class",
            "enum": "enum",
        }
        for child in node.children:
            if child.type in keyword_map:
                return keyword_map[child.type]
        return sym_type

    def _find_swift_visibility(self, node) -> Optional[str]:
        for child in node.children:
            if child.type != "modifiers":
                continue
            for modifier in child.children:
                if modifier.type != "visibility_modifier":
                    continue
                for token in modifier.children:
                    if token.type in {"open", "public", "internal", "fileprivate", "private"}:
                        return token.type
        return None

    def _fallback_chunk(self, content: str, file_path: str) -> list[dict]:
        """Simple line-based chunking for unsupported languages."""
        lines = content.split("\n")
        chunks = []
        chunk_lines = []
        start_line = 1

        for i, line in enumerate(lines, 1):
            chunk_lines.append(line)
            if len(" ".join(chunk_lines).split()) >= self.chunk_size:
                chunks.append({
                    "content": "\n".join(chunk_lines),
                    "start_line": start_line,
                    "end_line": i,
                })
                # Overlap
                overlap_start = max(0, len(chunk_lines) - self.overlap // 10)
                chunk_lines = chunk_lines[overlap_start:]
                start_line = i - len(chunk_lines) + 1

        if chunk_lines:
            chunks.append({
                "content": "\n".join(chunk_lines),
                "start_line": start_line,
                "end_line": len(lines),
            })

        return chunks

    def extract_dependencies(self, content: str, language: Optional[str], file_path: str) -> list[dict]:
        """Extract import/dependency information from source code."""
        if not language or language not in IMPORT_PATTERNS:
            return []

        deps = []
        patterns = IMPORT_PATTERNS[language]

        for line in content.split("\n"):
            line = line.strip()
            for pattern, kind in patterns:
                match = re.search(pattern, line)
                if match:
                    module = match.group(1)
                    deps.append({"module": module, "kind": kind, "raw": line})
                    break

        return deps
