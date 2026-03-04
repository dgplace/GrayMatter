"""
@file tests/test_ingest.py
@brief Unit tests for ingestion-side helper functions.
"""

from pathlib import Path

import ingest


def test_clean_swift_type_strips_optionals_generics_and_modules() -> None:
    """@brief Verify Swift type cleanup normalizes decorated type names."""
    assert ingest._clean_swift_type("App.TrackService<Dependency>?") == "TrackService"


def test_filter_gitignored_paths_preserves_input_when_git_root_is_unknown(monkeypatch) -> None:
    """@brief Verify Git filtering becomes a no-op when the repository root is unavailable."""
    paths = [Path("/repo/a.py"), Path("/repo/b.py")]
    monkeypatch.setattr(ingest, "get_git_root", lambda repo_root: None)

    assert ingest.filter_gitignored_paths(paths, Path("/repo")) == paths


def test_filter_gitignored_paths_removes_reported_ignored_files(monkeypatch, tmp_path) -> None:
    """@brief Verify Git-reported ignored files are removed while preserving order."""
    kept = tmp_path / "kept.py"
    ignored = tmp_path / "ignored.py"
    paths = [kept, ignored]

    class _Result:
        """@brief Minimal subprocess result stub for Git ignore tests."""

        returncode = 0
        stdout = b"ignored.py\0"

    monkeypatch.setattr(ingest, "get_git_root", lambda repo_root: tmp_path)
    monkeypatch.setattr(ingest.subprocess, "run", lambda *args, **kwargs: _Result())

    assert ingest.filter_gitignored_paths(paths, tmp_path) == [kept]


def test_extract_swift_service_edges_captures_typed_members_and_usage() -> None:
    """@brief Verify Swift service edges are derived from properties, init injection, and method calls."""
    content = "\n".join(
        [
            "final class TrackCoordinator {",
            "    private let trackService: TrackService",
            "    private let logger: Logger",
            "    init(trackService: TrackService) {}",
            "",
            "    func refreshTrack() {",
            "        trackService.reload()",
            "    }",
            "}",
        ]
    )
    chunks = [
        {
            "start_line": 1,
            "end_line": 9,
            "symbol_name": "TrackCoordinator",
            "symbol_type": "class",
        },
        {
            "start_line": 4,
            "end_line": 4,
            "symbol_name": "init",
            "symbol_type": "method",
            "parent_symbol": "TrackCoordinator",
        },
        {
            "start_line": 6,
            "end_line": 8,
            "symbol_name": "refreshTrack",
            "symbol_type": "method",
            "parent_symbol": "TrackCoordinator",
        },
    ]

    edges = ingest.extract_swift_service_edges(content, chunks)

    assert edges == [
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "type_reference",
            "line_no": 2,
        },
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "injection",
            "line_no": 4,
        },
        {
            "source_symbol_name": "TrackCoordinator",
            "target_name": "TrackService",
            "kind": "service_usage",
            "line_no": 7,
        },
    ]


def test_extract_symbol_references_deduplicates_and_skips_stopwords() -> None:
    """@brief Verify lexical reference extraction avoids duplicates and ignored keywords."""
    references = ingest.extract_symbol_references(
        [
            {
                "content": "\n".join(
                    [
                        "PhotoService()",
                        "photoStore.load(); photoStore.load()",
                        "if helper() { }",
                    ]
                ),
                "start_line": 10,
                "end_line": 12,
                "symbol_name": "refreshPhotos",
            }
        ]
    )

    assert references == [
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "PhotoService",
            "reference_kind": "type_reference",
            "line_no": 10,
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "load",
            "reference_kind": "member_call",
            "line_no": 11,
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "helper",
            "reference_kind": "call",
            "line_no": 12,
        },
    ]
