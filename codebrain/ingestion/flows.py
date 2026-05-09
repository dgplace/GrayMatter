"""
@file flows.py
@brief Execution-flow detection and flow-membership persistence helpers.
"""

import hashlib
from collections import Counter

import networkx as nx

FLOW_EDGE_KINDS = ("call", "member_call", "instantiation", "service_usage", "injection")
FLOW_MAX_COMPONENTS = 24
FLOW_MIN_COMPONENT_SIZE = 2
FLOW_NAME_MAX_CHARS = 96
FLOW_REASON_MAX_CHARS = 220
INTENT_DISPLAY_NAMES = {
    "api-endpoint": "API",
    "business-logic": "Business Logic",
    "orchestration": "Orchestration",
    "integration": "Integration",
    "data-model": "Data Model",
    "infrastructure": "Infrastructure",
    "middleware": "Middleware",
    "utility": "Utility",
}


def _stable_symbol_locator(meta: dict[str, str]) -> str:
    """@brief Build a deterministic symbol locator used in flow-id hashing."""
    return (
        f"{meta['path']}::{meta['qualified_name']}"
        f"@{meta['start_line']}-{meta['end_line']}"
    )


def _stable_flow_key(repo_name: str, locators: list[str]) -> str:
    """@brief Build a deterministic flow key for a repository-local member set."""
    locator_blob = "\n".join(sorted(locators))
    digest = hashlib.sha1(f"{repo_name}\n{locator_blob}".encode("utf-8")).hexdigest()
    return f"flow:{digest[:16]}"


def _truncate_text(value: str, max_chars: int) -> str:
    """@brief Trim long text payloads while preserving readability."""
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3].rstrip()}..."


def _load_symbol_meta(cur, repo_name: str) -> dict[int, dict[str, str]]:
    """@brief Load repository symbol metadata used for deterministic flow summaries."""
    cur.execute(
        """
        SELECT
            s.id,
            s.name,
            COALESCE(s.qualified_name, s.name) AS qualified_name,
            f.path,
            s.start_line,
            s.end_line,
            COALESCE(NULLIF(cc.intent, ''), '') AS intent,
            COALESCE(NULLIF(cc.intent_detail, ''), NULLIF(s.docstring, ''), '') AS context
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN code_chunks cc ON cc.id = s.chunk_id
        WHERE f.repo = %s
        """,
        (repo_name,),
    )
    meta: dict[int, dict[str, str]] = {}
    for symbol_id, name, qualified_name, path, start_line, end_line, intent, context in cur.fetchall():
        meta[int(symbol_id)] = {
            "name": str(name or "unknown"),
            "qualified_name": str(qualified_name or name or "unknown"),
            "path": str(path or "unknown"),
            "start_line": str(start_line or 0),
            "end_line": str(end_line or 0),
            "intent": str(intent or "").strip(),
            "context": str(context or "").strip(),
        }
    return meta


def _load_flow_graph(cur, repo_name: str, symbol_meta: dict[int, dict[str, str]]) -> nx.DiGraph:
    """@brief Build a directed call-style graph used for flow component detection."""
    graph = nx.DiGraph()
    kind_list = ",".join(f"'{kind}'" for kind in FLOW_EDGE_KINDS)
    cur.execute(
        f"""
        WITH reference_edges AS (
            SELECT
                source_symbol.id AS source_symbol_id,
                sr.target_symbol_id AS target_symbol_id,
                COUNT(*)::REAL AS edge_weight
            FROM symbol_references sr
            JOIN files source_file ON source_file.id = sr.source_file_id AND source_file.repo = %s
            LEFT JOIN LATERAL (
                SELECT s.id
                FROM symbols s
                WHERE s.file_id = sr.source_file_id
                  AND (
                    (sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name))
                    OR (sr.source_symbol_name IS NULL AND s.start_line <= sr.line_no AND s.end_line >= sr.line_no)
                  )
                ORDER BY
                  CASE
                    WHEN sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name) THEN 0
                    ELSE 1
                  END,
                  CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
                  ABS(s.start_line - sr.line_no)
                LIMIT 1
            ) source_symbol ON TRUE
            JOIN symbols target_symbol ON target_symbol.id = sr.target_symbol_id
            JOIN files target_file ON target_file.id = target_symbol.file_id AND target_file.repo = %s
            WHERE source_symbol.id IS NOT NULL
              AND sr.target_symbol_id IS NOT NULL
              AND COALESCE(sr.reference_kind_v2, sr.reference_kind) IN ({kind_list})
            GROUP BY source_symbol.id, sr.target_symbol_id
        ),
        relationship_edges AS (
            SELECT
                sr.source_symbol_id,
                sr.target_symbol_id,
                COUNT(*)::REAL AS edge_weight
            FROM symbol_relationships sr
            JOIN symbols source_symbol ON source_symbol.id = sr.source_symbol_id
            JOIN files source_file ON source_file.id = source_symbol.file_id AND source_file.repo = %s
            JOIN symbols target_symbol ON target_symbol.id = sr.target_symbol_id
            JOIN files target_file ON target_file.id = target_symbol.file_id AND target_file.repo = %s
            WHERE sr.source_symbol_id IS NOT NULL
              AND sr.target_symbol_id IS NOT NULL
              AND sr.relationship_kind IN ({kind_list})
            GROUP BY sr.source_symbol_id, sr.target_symbol_id
        ),
        all_edges AS (
            SELECT source_symbol_id, target_symbol_id, edge_weight FROM reference_edges
            UNION ALL
            SELECT source_symbol_id, target_symbol_id, edge_weight FROM relationship_edges
        )
        SELECT
            source_symbol_id,
            target_symbol_id,
            SUM(edge_weight)::REAL AS edge_weight
        FROM all_edges
        WHERE source_symbol_id != target_symbol_id
        GROUP BY source_symbol_id, target_symbol_id
        """,
        (repo_name, repo_name, repo_name, repo_name),
    )

    for source_symbol_id, target_symbol_id, edge_weight in cur.fetchall():
        source_id = int(source_symbol_id)
        target_id = int(target_symbol_id)
        if source_id not in symbol_meta or target_id not in symbol_meta:
            continue
        graph.add_node(source_id)
        graph.add_node(target_id)
        if graph.has_edge(source_id, target_id):
            graph[source_id][target_id]["weight"] += float(edge_weight)
        else:
            graph.add_edge(source_id, target_id, weight=float(edge_weight))
    return graph


def _component_intent(component: set[int], symbol_meta: dict[int, dict[str, str]]) -> str:
    """@brief Pick the dominant intent label for a flow component."""
    counts = Counter(symbol_meta[symbol_id]["intent"] for symbol_id in component if symbol_meta[symbol_id]["intent"])
    if not counts:
        return "orchestration"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _member_role(graph: nx.DiGraph, symbol_id: int, component: set[int]) -> str:
    """@brief Infer a symbol's role in the flow from local in/out degree balance."""
    subgraph = graph.subgraph(component)
    in_degree = subgraph.in_degree(symbol_id, weight="weight")
    out_degree = subgraph.out_degree(symbol_id, weight="weight")
    if out_degree > 0 and in_degree == 0:
        return "entrypoint"
    if in_degree > 0 and out_degree == 0:
        return "terminal"
    if in_degree > 0 and out_degree > 0:
        return "orchestrator"
    return "isolated"


def _member_reason(graph: nx.DiGraph, symbol_id: int, component: set[int], symbol_meta: dict[int, dict[str, str]]) -> str:
    """@brief Build a short deterministic reason explaining flow membership."""
    role = _member_role(graph, symbol_id, component)
    context = symbol_meta[symbol_id]["context"]
    if context:
        return _truncate_text(context, FLOW_REASON_MAX_CHARS)

    subgraph = graph.subgraph(component)
    in_degree = int(subgraph.in_degree(symbol_id))
    out_degree = int(subgraph.out_degree(symbol_id))
    return f"Role: {role}; linked by {in_degree} inbound and {out_degree} outbound call edges in this flow."


def _component_sort_key(component: set[int], symbol_meta: dict[int, dict[str, str]]) -> tuple[str, ...]:
    """@brief Provide deterministic ordering for candidate flow components."""
    return tuple(sorted(_stable_symbol_locator(symbol_meta[symbol_id]) for symbol_id in component))


def _flow_name(intent: str, anchor_name: str) -> str:
    """@brief Build a deterministic human-readable flow name."""
    intent_label = INTENT_DISPLAY_NAMES.get(intent, intent.replace("-", " ").title())
    return _truncate_text(f"{intent_label} Flow: {anchor_name}", FLOW_NAME_MAX_CHARS)


def _flow_summary(intent: str, component: set[int], symbol_meta: dict[int, dict[str, str]]) -> str:
    """@brief Build a concise summary for a flow component."""
    intent_label = INTENT_DISPLAY_NAMES.get(intent, intent.replace("-", " ").title())
    file_paths = sorted({symbol_meta[symbol_id]["path"] for symbol_id in component})
    preview = ", ".join(file_paths[:3])
    suffix = "" if len(file_paths) <= 3 else f", +{len(file_paths) - 3} more"
    return (
        f"{intent_label} execution path spanning {len(component)} symbols across "
        f"{len(file_paths)} files ({preview}{suffix})."
    )


def _anchor_symbol(graph: nx.DiGraph, component: set[int], symbol_meta: dict[int, dict[str, str]]) -> int:
    """@brief Pick a stable representative symbol for naming the flow."""
    subgraph = graph.subgraph(component)
    return sorted(
        component,
        key=lambda symbol_id: (
            -float(subgraph.in_degree(symbol_id, weight="weight") + subgraph.out_degree(symbol_id, weight="weight")),
            symbol_meta[symbol_id]["qualified_name"],
            symbol_meta[symbol_id]["path"],
        ),
    )[0]


def materialize_flows(conn, repo_name: str) -> int:
    """@brief Rebuild deterministic repository flow and flow-membership rows.

    @param conn Open database connection.
    @param repo_name Repository identifier.
    @return Number of persisted flows.
    """
    cur = conn.cursor()
    symbol_meta = _load_symbol_meta(cur, repo_name)
    graph = _load_flow_graph(cur, repo_name, symbol_meta)

    cur.execute(
        """
        DELETE FROM flow_members
        WHERE flow_id IN (SELECT id FROM flows WHERE repo = %s)
        """,
        (repo_name,),
    )
    cur.execute("DELETE FROM flows WHERE repo = %s", (repo_name,))

    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        conn.commit()
        return 0

    components = [
        set(component)
        for component in nx.weakly_connected_components(graph)
        if len(component) >= FLOW_MIN_COMPONENT_SIZE
    ]
    components.sort(
        key=lambda component: (
            -float(graph.subgraph(component).size(weight="weight")),
            -len(component),
            _component_sort_key(component, symbol_meta),
        )
    )

    flow_count = 0
    for component in components[:FLOW_MAX_COMPONENTS]:
        intent = _component_intent(component, symbol_meta)
        anchor_symbol_id = _anchor_symbol(graph, component, symbol_meta)
        anchor_name = symbol_meta[anchor_symbol_id]["name"]
        locators = [_stable_symbol_locator(symbol_meta[symbol_id]) for symbol_id in component]
        flow_key = _stable_flow_key(repo_name, locators)
        flow_name = _flow_name(intent, anchor_name)
        flow_summary = _flow_summary(intent, component, symbol_meta)

        cur.execute(
            """
            INSERT INTO flows (repo, flow_key, name, summary, dominant_intent)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (repo_name, flow_key, flow_name, flow_summary, intent),
        )
        flow_id = int(cur.fetchone()[0])

        for symbol_id in sorted(component, key=lambda current: _stable_symbol_locator(symbol_meta[current])):
            cur.execute(
                """
                INSERT INTO flow_members (flow_id, symbol_id, role, reason)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    flow_id,
                    symbol_id,
                    _member_role(graph, symbol_id, component),
                    _member_reason(graph, symbol_id, component, symbol_meta),
                ),
            )

        flow_count += 1

    conn.commit()
    return flow_count
