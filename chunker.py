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
        "protocol_declaration": "protocol",
        "enum_declaration": "enum",
    },
    "csharp": {
        "class_declaration": "class",
        "struct_declaration": "struct",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "delegate_declaration": "function",
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
    "csharp": [
        (r"^(?:global\s+)?using\s+static\s+([\w.]+)\s*;", "import"),
        (r"^(?:global\s+)?using\s+\w+\s*=\s*([\w.]+)\s*;", "import"),
        (r"^(?:global\s+)?using\s+([\w.]+)\s*;", "import"),
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
            elif lang == "csharp":
                import tree_sitter_c_sharp as tsl
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
        symbols = self._collect_top_level_symbols(tree.root_node, symbol_types, language)

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
            declared_in_extension = self._is_swift_extension(sym_node)
            member_symbols = []

            if sym_type in {"class", "struct", "extension", "protocol", "interface"} and sym_name:
                member_symbols = self._extract_member_symbols(
                    sym_node,
                    lines,
                    language,
                    file_path,
                    sym_name,
                    declared_in_extension=declared_in_extension,
                )

            # If chunk is too large, keep a compact declaration chunk and sub-chunk members
            if len(chunk_content.split()) > self.chunk_size * 1.5 and sym_type in {"class", "struct", "extension", "protocol", "interface"}:
                sub_chunks = self._sub_chunk_container(
                    sym_node,
                    lines,
                    language,
                    file_path,
                    sym_name,
                    sym_type,
                    docstring,
                    signature,
                    visibility,
                    is_exported,
                    declared_in_extension,
                )
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
                    "container_symbol": None,
                    "visibility": visibility,
                    "is_exported": is_exported,
                    "declared_in_extension": declared_in_extension,
                    "is_primary_declaration": not declared_in_extension,
                    "member_symbols": member_symbols,
                })

        return chunks if chunks else self._fallback_chunk(content, file_path)

    def _collect_top_level_symbols(self, root_node, symbol_types: dict[str, str], language: str) -> list:
        """@brief Collect top-level declarations that should become symbol chunks.

        @param root_node Parsed tree-sitter root node.
        @param symbol_types Symbol type mapping for the active language.
        @param language Language name.
        @return List of tree-sitter nodes representing top-level symbols.
        """
        symbols = []
        if language == "csharp":
            self._collect_csharp_namespace_symbols(root_node, symbols, symbol_types)
            return symbols

        for child in root_node.children:
            node_type = child.type
            if node_type in symbol_types:
                symbols.append(child)
            # Handle decorated definitions (Python)
            elif node_type == "decorated_definition":
                for sub in child.children:
                    if sub.type in symbol_types:
                        symbols.append(child)  # keep the decorator
                        break
        return symbols

    def _collect_csharp_namespace_symbols(self, node, symbols: list, symbol_types: dict[str, str]) -> None:
        """@brief Collect C# top-level declarations from namespace and file scopes.

        @param node Current tree-sitter node to scan.
        @param symbols Mutable output list of declaration nodes.
        @param symbol_types Symbol node types considered first-class declarations.
        @return None.
        """
        namespace_scopes = {
            "compilation_unit",
            "namespace_declaration",
            "file_scoped_namespace_declaration",
            "declaration_list",
        }
        for child in getattr(node, "children", []):
            if child.type in symbol_types:
                symbols.append(child)
                continue
            if child.type in namespace_scopes:
                self._collect_csharp_namespace_symbols(child, symbols, symbol_types)

    def _sub_chunk_container(
        self,
        container_node,
        lines,
        language,
        file_path,
        container_name,
        container_type,
        docstring,
        signature,
        visibility,
        is_exported,
        declared_in_extension,
    ) -> list[dict]:
        """Break a large container into a declaration chunk plus per-member chunks."""
        chunks = []
        start = container_node.start_point[0]
        declaration_content = signature or lines[start].strip()
        chunks.append({
            "content": declaration_content,
            "start_line": start + 1,
            "end_line": start + 1,
            "symbol_name": container_name,
            "symbol_type": container_type,
            "parent_symbol": None,
            "docstring": docstring,
            "signature": signature,
            "qualified_name": f"{file_path}:{container_name}" if container_name else None,
            "container_symbol": None,
            "visibility": visibility,
            "is_exported": is_exported,
            "declared_in_extension": declared_in_extension,
            "is_primary_declaration": not declared_in_extension,
            "member_symbols": [],
        })

        for member in self._extract_member_symbols(
            container_node,
            lines,
            language,
            file_path,
            container_name,
            declared_in_extension=declared_in_extension,
        ):
            self._add_member_chunk(member, lines, chunks)

        return chunks

    def _add_member_chunk(self, member_symbol: dict, lines: list[str], chunks: list[dict]):
        start = member_symbol["start_line"] - 1
        end = member_symbol["end_line"] - 1
        chunks.append({
            "content": "\n".join(lines[start:end + 1]),
            "start_line": member_symbol["start_line"],
            "end_line": member_symbol["end_line"],
            "symbol_name": member_symbol["symbol_name"],
            "symbol_type": member_symbol["symbol_type"],
            "parent_symbol": member_symbol.get("parent_symbol"),
            "docstring": member_symbol.get("docstring"),
            "signature": member_symbol.get("signature"),
            "qualified_name": member_symbol.get("qualified_name"),
            "container_symbol": member_symbol.get("container_symbol"),
            "visibility": member_symbol.get("visibility", "public"),
            "is_exported": member_symbol.get("is_exported", False),
            "declared_in_extension": member_symbol.get("declared_in_extension", False),
            "is_primary_declaration": False,
            "member_symbols": [],
        })

    def _extract_member_symbols(
        self,
        container_node,
        lines,
        language: str,
        file_path: str,
        container_name: str,
        declared_in_extension: bool = False,
    ) -> list[dict]:
        """Extract first-class symbols for methods/properties nested inside a top-level container."""
        member_symbols = []
        member_types = {
            "function_definition": "method",
            "method_definition": "method",
            "function_declaration": "method",
            "method_declaration": "method",
            "constructor_declaration": "method",
            "destructor_declaration": "method",
            "operator_declaration": "method",
            "conversion_operator_declaration": "method",
            "function_item": "method",
            "public_method_definition": "method",
            "protocol_function_declaration": "method",
            "initializer_declaration": "method",
            "deinitializer_declaration": "method",
            "subscript_declaration": "method",
            "indexer_declaration": "method",
            "property_declaration": "property",
        }
        nested_container_types = {
            "class_declaration",
            "protocol_declaration",
            "enum_declaration",
            "struct_declaration",
            "extension_declaration",
            "class_definition",
            "interface_declaration",
            "type_declaration",
            "class_specifier",
            "struct_specifier",
            "enum_specifier",
            "namespace_definition",
            "namespace_declaration",
            "file_scoped_namespace_declaration",
            "record_declaration",
        }

        def visit(node):
            for child in getattr(node, "children", []):
                if child.type in member_types:
                    member_name = self._extract_name(child, language)
                    if not member_name:
                        continue
                    member_symbols.append({
                        "symbol_name": member_name,
                        "symbol_type": member_types[child.type],
                        "parent_symbol": container_name,
                        "container_symbol": container_name,
                        "qualified_name": f"{file_path}:{container_name}.{member_name}",
                        "docstring": self._extract_docstring(child, "\n".join(lines), language),
                        "signature": self._extract_signature(child, lines, language),
                        "start_line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                        "visibility": self._infer_visibility(member_name, child, language),
                        "is_exported": self._is_exported(child, language),
                        "declared_in_extension": declared_in_extension,
                    })
                    continue

                if child.type in nested_container_types:
                    continue

                if getattr(child, "children", None):
                    visit(child)

        visit(container_node)
        return member_symbols

    def _extract_name(self, node, language: str) -> Optional[str]:
        """Extract the name identifier from a syntax node."""
        if node.type == "initializer_declaration":
            return "init"
        if node.type == "deinitializer_declaration":
            return "deinit"
        if node.type == "subscript_declaration":
            return "subscript"
        if node.type == "indexer_declaration":
            return "this[]"
        if node.type in {"operator_declaration", "conversion_operator_declaration"}:
            return "operator"
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
        if language in ("typescript", "javascript", "tsx", "jsx", "csharp"):
            prev = node.prev_sibling
            if prev and prev.type == "comment":
                text = prev.text.decode("utf-8")
                if text.startswith("/**") or text.startswith("///"):
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
        if language == "csharp":
            modifier = self._find_csharp_visibility(node)
            if modifier:
                return modifier

            if node.type in {
                "class_declaration",
                "struct_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
                "delegate_declaration",
            }:
                return "internal"

            parent = node.parent
            while parent is not None:
                if parent.type == "interface_declaration":
                    return "public"
                if parent.type in {"class_declaration", "struct_declaration", "record_declaration"}:
                    return "private"
                parent = parent.parent
            return "private"
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
        if language == "csharp":
            visibility = self._find_csharp_visibility(node)
            if visibility is None:
                visibility = self._infer_visibility(self._extract_name(node, language), node, language)
            return visibility in {"public", "protected", "protected internal"}
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
            "struct": "struct",
            "extension": "extension",
            "enum": "enum",
        }
        for child in node.children:
            if child.type in keyword_map:
                return keyword_map[child.type]
        return sym_type

    def _is_swift_extension(self, node) -> bool:
        return any(child.type == "extension" for child in getattr(node, "children", []))

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

    def _find_csharp_visibility(self, node) -> Optional[str]:
        """@brief Read C# visibility modifiers from declaration nodes.

        @param node Tree-sitter declaration node.
        @return Normalized visibility string when present, otherwise None.
        """
        modifiers = []
        visibility_tokens = {"public", "private", "protected", "internal", "file"}

        for child in getattr(node, "children", []):
            if child.type not in {"modifiers", "modifier"}:
                continue

            for token in getattr(child, "children", []):
                token_type = token.type.removesuffix("_keyword")
                if token_type in visibility_tokens:
                    modifiers.append(token_type)

        has = set(modifiers)
        if "private" in has and "protected" in has:
            return "private protected"
        if "protected" in has and "internal" in has:
            return "protected internal"
        if "public" in has:
            return "public"
        if "private" in has:
            return "private"
        if "protected" in has:
            return "protected"
        if "internal" in has:
            return "internal"
        if "file" in has:
            return "file"
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
