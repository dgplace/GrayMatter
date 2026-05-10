"""
@file tests/test_framework_diagnostics.py
@brief Unit tests for callback framework diagnostics detection and persistence.
"""

from codebrain.ingestion import framework_diagnostics


class _FrameworkCursor:
    """@brief Cursor stub returning deterministic dependency/reference rows."""

    def __init__(self) -> None:
        self._pending_fetchall: list[tuple] = []
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, query: str, params=None) -> None:
        """@brief Route known SQL reads/writes to canned row payloads.

        @param query SQL statement text.
        @param params SQL parameters.
        """
        normalized = " ".join(query.strip().lower().split())
        self.executed.append((normalized, params))
        if "from dependencies" in normalized:
            self._pending_fetchall = [
                (10, "express", ""),
                (11, "@nestjs/common", ""),
                (12, "org.springframework.web.bind.annotation", ""),
                (13, "qtcore", ""),
            ]
            return
        if "from symbol_references" in normalized:
            self._pending_fetchall = [
                (10, "useeffect"),
                (12, "requestmapping"),
                (14, "addeventlistener"),
                (15, "eventemitter"),
            ]
            return
        self._pending_fetchall = []

    def fetchall(self) -> list[tuple]:
        """@brief Return prepared batch rows."""
        return self._pending_fetchall

    def close(self) -> None:
        """@brief No-op cursor close for test parity with production cursor use."""
        return None


class _FrameworkConn:
    """@brief Connection stub exposing one reusable cursor and commit counter."""

    def __init__(self) -> None:
        self.cursor_instance = _FrameworkCursor()
        self.commits = 0

    def cursor(self) -> _FrameworkCursor:
        """@brief Return the reusable cursor instance."""
        return self.cursor_instance

    def commit(self) -> None:
        """@brief Increment commit counter."""
        self.commits += 1


def test_callback_framework_registry_has_seeded_entries_with_null_extractors() -> None:
    """@brief Verify CODEBRAIN-10 registry includes required seeded framework names."""
    names = {entry["framework"] for entry in framework_diagnostics.CALLBACK_FRAMEWORK_REGISTRY}
    assert {
        "Express",
        "FastAPI",
        "Flask",
        "React (useEffect)",
        "Node EventEmitter",
        "DOM addEventListener",
        "NestJS",
        "Spring",
        "Qt signals/slots",
    }.issubset(names)
    assert all(entry["extractor_module"] is None for entry in framework_diagnostics.CALLBACK_FRAMEWORK_REGISTRY)


def test_detect_callback_frameworks_aggregates_dependency_and_reference_signals() -> None:
    """@brief Verify framework detection merges dependency and reference evidence per file."""
    cur = _FrameworkCursor()
    detected = framework_diagnostics.detect_callback_frameworks(cur, "repo")
    detected_map = {row["framework"]: row for row in detected}

    assert detected_map["Express"]["affected_file_count"] == 1
    assert detected_map["React (useEffect)"]["affected_file_ids"] == [10]
    assert detected_map["NestJS"]["affected_file_ids"] == [11]
    assert detected_map["Spring"]["affected_file_ids"] == [12]
    assert detected_map["Qt signals/slots"]["affected_file_ids"] == [13]
    assert detected_map["DOM addEventListener"]["affected_file_ids"] == [14]
    assert detected_map["Node EventEmitter"]["affected_file_ids"] == [15]


def test_materialize_missing_extractor_diagnostics_deletes_then_inserts() -> None:
    """@brief Verify persistence clears prior missing-extractor rows before insert/upsert."""
    conn = _FrameworkConn()
    inserted = framework_diagnostics.materialize_missing_extractor_diagnostics(conn, "repo")

    assert inserted >= 1
    assert conn.commits == 1
    normalized_queries = [query for query, _params in conn.cursor_instance.executed]
    assert any(query.startswith("delete from ingestion_diagnostics") for query in normalized_queries)
    assert any(query.startswith("insert into ingestion_diagnostics") for query in normalized_queries)
