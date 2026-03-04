"""
@file tests/test_chunker.py
@brief Unit tests for chunking and dependency extraction helpers.
"""

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
