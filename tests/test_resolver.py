"""
@file tests/test_resolver.py
@brief Unit tests for the resolver pipeline stage.
"""

import resolver


class _ResolverCursor:
    """@brief Deterministic cursor stub for resolver unit tests."""

    def __init__(self) -> None:
        self._pending_fetchone = None
        self._pending_fetchall = []
        self.updated_rows = []

    def execute(self, query: str, params=None) -> None:
        """@brief Provide canned responses for resolver SQL calls.

        @param query SQL statement text.
        @param params SQL parameters.
        @return None.
        """
        normalized = " ".join(query.strip().lower().split())
        if normalized.startswith("select s.id, s.file_id from symbols s where lower(s.name) = lower(%s)"):
            target_name = params[0].lower()
            if target_name == "photoservice":
                self._pending_fetchone = (101, 11)
            elif target_name == "helper":
                self._pending_fetchone = (202, 22)
            else:
                self._pending_fetchone = None
            self._pending_fetchall = []
            return

        if normalized.startswith("select id from symbols where file_id = %s"):
            self._pending_fetchall = [(7,), (8,)]
            self._pending_fetchone = None
            return

        if normalized.startswith("select sr.id, sr.source_file_id, sr.target_name, sr.reference_kind from symbol_references sr"):
            self._pending_fetchall = [
                (301, 41, "PhotoService", "type_reference"),
                (302, 42, "MissingService", "call"),
                (303, 43, "helper", "call"),
            ]
            self._pending_fetchone = None
            return

        if normalized.startswith("select count(*) from files where repo = %s"):
            self._pending_fetchone = (20,)
            self._pending_fetchall = []
            return

        if normalized.startswith("update symbol_references set target_symbol_id = %s"):
            self.updated_rows.append(params)
            self._pending_fetchone = None
            self._pending_fetchall = []
            return

        raise AssertionError(f"Unexpected SQL in resolver test stub: {query}")

    def fetchone(self):
        """@brief Return the pending single-row payload."""
        return self._pending_fetchone

    def fetchall(self):
        """@brief Return the pending multi-row payload."""
        return self._pending_fetchall


def test_extract_symbol_references_deduplicates_and_skips_stopwords() -> None:
    """@brief Verify lexical reference extraction avoids duplicates and ignored keywords."""
    references = resolver.extract_symbol_references(
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


def test_resolve_references_returns_uniform_resolver_shape() -> None:
    """@brief Verify resolver records always include method, confidence, and richer kind fields."""
    cur = _ResolverCursor()
    resolved = resolver.resolve_references(
        cur,
        [
            {
                "content": "\n".join(["PhotoService()", "helper()", "missing()"]),
                "start_line": 1,
                "end_line": 3,
                "symbol_name": "refreshPhotos",
            }
        ],
    )

    assert resolved == [
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "PhotoService",
            "reference_kind": "type_reference",
            "line_no": 1,
            "target_symbol_id": 101,
            "target_file_id": 11,
            "resolution_confidence": resolver.HEURISTIC_NAME_CONFIDENCE,
            "resolution_method": "heuristic_name",
            "reference_kind_v2": "type_reference",
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "helper",
            "reference_kind": "call",
            "line_no": 2,
            "target_symbol_id": 202,
            "target_file_id": 22,
            "resolution_confidence": resolver.HEURISTIC_NAME_CONFIDENCE,
            "resolution_method": "heuristic_name",
            "reference_kind_v2": "call",
        },
        {
            "chunk_index": 0,
            "source_symbol_name": "refreshPhotos",
            "target_name": "missing",
            "reference_kind": "call",
            "line_no": 3,
            "target_symbol_id": None,
            "target_file_id": None,
            "resolution_confidence": 0.0,
            "resolution_method": "unresolved",
            "reference_kind_v2": "call",
        },
    ]


def test_capture_incremental_refresh_emits_warning_only_guardrails() -> None:
    """@brief Verify incremental refresh plans warn on high file fan-out without broadening scope."""
    cur = _ResolverCursor()

    plan = resolver.capture_incremental_refresh(cur, "CodeBrain", 5)

    assert [row["id"] for row in plan["rows"]] == [301, 302, 303]
    assert plan["warnings"] == [
        "Resolver incremental refresh touched 3/20 source files; continuing with selective re-resolution."
    ]


def test_reresolve_inbound_references_updates_only_captured_rows() -> None:
    """@brief Verify inbound re-resolution rewrites only the planned reference rows."""
    cur = _ResolverCursor()
    plan = {
        "rows": [
            {"id": 301, "source_file_id": 41, "target_name": "PhotoService", "reference_kind": "type_reference"},
            {"id": 302, "source_file_id": 42, "target_name": "MissingService", "reference_kind": "call"},
            {"id": 303, "source_file_id": 43, "target_name": "helper", "reference_kind": "call"},
        ],
        "warnings": [],
    }

    updated = resolver.re_resolve_inbound_references(cur, plan)

    assert updated == 3
    assert cur.updated_rows == [
        (101, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "type_reference", 301),
        (None, 0.0, "unresolved", "call", 302),
        (202, resolver.HEURISTIC_NAME_CONFIDENCE, "heuristic_name", "call", 303),
    ]
