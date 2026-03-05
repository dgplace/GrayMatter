"""
@file tests/test_chunker.py
@brief Unit tests for chunking and dependency extraction helpers.
"""

import pytest

from chunker import ASTChunker


def test_chunk_file_falls_back_when_language_is_missing() -> None:
    """@brief Verify line-based chunking is used when no language is provided."""
    chunker = ASTChunker({"ingestion": {"chunk_size": 2, "overlap": 0}})

    chunks = chunker.chunk_file("alpha beta\ngamma delta", None, "demo.txt")

    assert chunks == [
        {"content": "alpha beta", "start_line": 1, "end_line": 1},
        {"content": "gamma delta", "start_line": 2, "end_line": 2},
    ]


def test_extract_dependencies_returns_swift_imports() -> None:
    """@brief Verify Swift import statements are extracted as dependency edges."""
    chunker = ASTChunker({"ingestion": {"chunk_size": 32, "overlap": 0}})

    deps = chunker.extract_dependencies(
        "import Foundation\nimport MapKit\nlet value = 1",
        "swift",
        "demo.swift",
    )

    assert deps == [
        {"module": "Foundation", "kind": "import", "raw": "import Foundation"},
        {"module": "MapKit", "kind": "import", "raw": "import MapKit"},
    ]


def test_extract_dependencies_returns_empty_for_unknown_language() -> None:
    """@brief Verify dependency extraction ignores unsupported languages."""
    chunker = ASTChunker({"ingestion": {"chunk_size": 32, "overlap": 0}})

    assert chunker.extract_dependencies("import nowhere", "unknown", "demo.txt") == []


def test_extract_dependencies_returns_csharp_using_directives() -> None:
    """@brief Verify C# using directives are extracted as dependency edges."""
    chunker = ASTChunker({"ingestion": {"chunk_size": 32, "overlap": 0}})

    deps = chunker.extract_dependencies(
        "\n".join(
            [
                "using System;",
                "using static System.Math;",
                "global using Demo.Core;",
                "using Alias = Demo.Services;",
                "Console.WriteLine(\"ok\");",
            ]
        ),
        "csharp",
        "Program.cs",
    )

    assert deps == [
        {"module": "System", "kind": "import", "raw": "using System;"},
        {"module": "System.Math", "kind": "import", "raw": "using static System.Math;"},
        {"module": "Demo.Core", "kind": "import", "raw": "global using Demo.Core;"},
        {"module": "Demo.Services", "kind": "import", "raw": "using Alias = Demo.Services;"},
    ]


def test_chunk_file_extracts_csharp_namespace_symbols() -> None:
    """@brief Verify C# classes and members are extracted under namespace scopes."""
    pytest.importorskip("tree_sitter_c_sharp")

    chunker = ASTChunker({"ingestion": {"chunk_size": 128, "overlap": 0}})
    content = "\n".join(
        [
            "using System;",
            "namespace Demo.Services;",
            "",
            "public class Greeter",
            "{",
            "    public string SayHello(string name)",
            "    {",
            "        return $\"Hello {name}\";",
            "    }",
            "}",
        ]
    )

    chunks = chunker.chunk_file(content, "csharp", "Greeter.cs")
    class_chunk = next(chunk for chunk in chunks if chunk.get("symbol_name") == "Greeter")
    member_names = [member["symbol_name"] for member in class_chunk.get("member_symbols", [])]

    assert class_chunk["visibility"] == "public"
    assert class_chunk["is_exported"] is True
    assert "SayHello" in member_names
