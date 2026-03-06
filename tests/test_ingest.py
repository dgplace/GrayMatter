"""
@file tests/test_ingest.py
@brief Unit tests for ingestion-side helper functions.
"""

from pathlib import Path

import ingest


class _FakeCursor:
    """@brief Minimal cursor stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._pending_fetch: tuple | None = None

    def execute(self, query: str, params=None) -> None:
        """@brief Record a deterministic fetch response for known SQL patterns.

        @param query SQL statement text.
        @param params SQL parameters (unused).
        """
        normalized = " ".join(query.strip().lower().split())
        if normalized.startswith("select id, hash from files"):
            self._pending_fetch = None
        elif normalized.startswith("insert into files"):
            self._pending_fetch = (1,)
        else:
            self._pending_fetch = None

    def fetchone(self):
        """@brief Return the prepared fetch payload."""
        return self._pending_fetch


class _FakeConn:
    """@brief Minimal psycopg2 connection stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._cursor = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        """@brief Return a reusable fake cursor."""
        return self._cursor

    def commit(self) -> None:
        """@brief No-op commit for the fake connection."""
        return None

    def rollback(self) -> None:
        """@brief No-op rollback for the fake connection."""
        return None


class _FakePool:
    """@brief Minimal connection pool stub for process_file warning-path tests."""

    def __init__(self) -> None:
        self._conn = _FakeConn()

    def getconn(self) -> _FakeConn:
        """@brief Return a fake connection."""
        return self._conn

    def putconn(self, conn: _FakeConn) -> None:
        """@brief No-op pool return method.

        @param conn Connection being returned.
        """
        return None


class _FakeEmbedder:
    """@brief Minimal embedding client stub returning deterministic vectors."""

    def embed(self, text: str) -> list[float]:
        """@brief Return a single deterministic embedding vector.

        @param text Input text to embed.
        @return Dummy vector for test assertions.
        """
        return [0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """@brief Return one deterministic vector per input text.

        @param texts Batch of embedding inputs.
        @return Dummy vectors for test assertions.
        """
        return [[0.0] for _ in texts]


class _FakeClassifier:
    """@brief Minimal classifier stub that emits one warning and fallback values."""

    def analyze_file(self, file_path: str, code: str, language: str, on_warning=None) -> tuple[str, str]:
        """@brief Emit a warning and return fallback summary/role values.

        @param file_path Relative file path.
        @param code File contents.
        @param language File language label.
        @param on_warning Optional warning callback.
        @return Empty summary and unknown role.
        """
        if on_warning:
            on_warning(f"Classifier file analysis fallback for {file_path}: test")
        return "", "unknown"

    def classify_chunks_batch(
        self,
        chunks: list[dict],
        language: str,
        file_path: str,
        on_warning=None,
    ) -> list[tuple[str, str]]:
        """@brief Return fallback chunk classifications.

        @param chunks Chunk dictionaries.
        @param language File language label.
        @param file_path Relative file path.
        @param on_warning Optional warning callback.
        @return Utility fallback classifications matching chunk count.
        """
        return [("utility", "")] * len(chunks)


class _FakeChunker:
    """@brief Minimal chunker stub that emits no chunks."""

    def chunk_file(self, content: str, language: str, rel_path: str) -> list[dict]:
        """@brief Return no chunks to keep the test path focused on warnings.

        @param content File contents.
        @param language File language label.
        @param rel_path Relative file path.
        @return Empty chunk list.
        """
        return []

    def extract_dependencies(self, content: str, language: str, rel_path: str) -> list[dict]:
        """@brief Return no dependencies.

        @param content File contents.
        @param language File language label.
        @param rel_path Relative file path.
        @return Empty dependency list.
        """
        return []


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


def test_normalize_result_status_maps_error_variants_to_errors() -> None:
    """@brief Verify worker result status values are normalized to summary keys."""
    assert ingest.normalize_result_status("indexed") == "indexed"
    assert ingest.normalize_result_status("skipped") == "skipped"
    assert ingest.normalize_result_status("error") == "errors"
    assert ingest.normalize_result_status("errors") == "errors"
    assert ingest.normalize_result_status("unexpected-value") == "errors"
    assert ingest.normalize_result_status(None) == "errors"


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


def test_process_file_includes_classifier_warnings(monkeypatch, tmp_path) -> None:
    """@brief Verify classifier fallback messages are returned in process_file results."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fpath = repo_root / "demo.py"
    fpath.write_text("print('x')\n", encoding="utf-8")

    monkeypatch.setattr(ingest, "register_vector", lambda conn: None)

    result = ingest.process_file(
        fpath=fpath,
        repo_root=repo_root,
        repo_name="repo",
        config={"languages": {"extensions": {"py": "python"}}},
        embedder=_FakeEmbedder(),
        classifier=_FakeClassifier(),
        chunker=_FakeChunker(),
        db_pool=_FakePool(),
        force=True,
        no_classify=False,
    )

    assert result["status"] == "indexed"
    assert len(result.get("warnings", [])) == 1
    assert "Classifier file analysis fallback for demo.py" in result["warnings"][0]
