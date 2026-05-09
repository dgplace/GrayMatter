"""
@file clusters.py
@brief Repository graph clustering and cluster-profile persistence helpers.
"""

import re

import networkx as nx
from rich.console import Console

from codebrain.classifier import IntentClassifier
from codebrain.embedder import EmbeddingClient

console = Console()

CLUSTER_SUMMARY_MAX_CHARS = 3_000
CLUSTER_MEMBER_CONTEXT_LIMIT = 30
CLUSTER_CLASS_KINDS = ("class", "struct", "interface", "protocol", "enum")
MIN_SYMBOL_CLUSTER_NODES = 5


def _normalize_doc_link_content(content: str | None) -> str | None:
    """@brief Normalize persisted cluster-summary content for doc_links rows."""
    if content is None:
        return None
    normalized = content.strip()
    if not normalized:
        return None
    return normalized

def _build_symbol_cluster_graph(cur, repo_name: str) -> tuple[nx.Graph, dict[int, dict[str, str]]]:
    """@brief Build a symbol-level weighted graph for Leiden clustering.

    @param cur Database cursor.
    @param repo_name Repository identifier.
    @return Tuple of undirected graph and symbol metadata keyed by symbol id.
    """
    kind_placeholders = ",".join(f"'{kind}'" for kind in CLUSTER_CLASS_KINDS)
    cur.execute(
        f"""
        SELECT
            s.id,
            s.name,
            s.kind,
            f.path,
            COALESCE(NULLIF(s.docstring, ''), NULLIF(cc.intent_detail, ''), NULLIF(f.summary, '')) AS context
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN code_chunks cc ON cc.id = s.chunk_id
        WHERE f.repo = %s
          AND s.parent_id IS NULL
          AND s.kind IN ({kind_placeholders})
        """,
        (repo_name,),
    )
    symbol_meta: dict[int, dict[str, str]] = {}
    for symbol_id, name, kind, path, context in cur.fetchall():
        symbol_meta[int(symbol_id)] = {
            "label": f"{name} ({kind}, {path})",
            "context": (context or "").strip(),
            "path": path,
        }

    graph = nx.Graph()
    for symbol_id in symbol_meta:
        graph.add_node(symbol_id)
    if len(symbol_meta) < MIN_SYMBOL_CLUSTER_NODES:
        return graph, symbol_meta

    cur.execute(
        """
        WITH dep_edges AS (
            SELECT
                d.source_symbol_id AS src,
                d.target_symbol_id AS tgt,
                COUNT(*)::REAL AS edge_weight
            FROM dependencies d
            JOIN symbols ss ON ss.id = d.source_symbol_id
            JOIN files sf ON sf.id = ss.file_id
            JOIN symbols ts ON ts.id = d.target_symbol_id
            JOIN files tf ON tf.id = ts.file_id
            WHERE d.source_symbol_id IS NOT NULL
              AND d.target_symbol_id IS NOT NULL
              AND sf.repo = %s
              AND tf.repo = %s
            GROUP BY d.source_symbol_id, d.target_symbol_id
        ),
        ref_edges AS (
            SELECT
                source_symbol.id AS src,
                sr.target_symbol_id AS tgt,
                COUNT(*)::REAL AS edge_weight
            FROM symbol_references sr
            JOIN files sf ON sf.id = sr.source_file_id
            JOIN LATERAL (
                SELECT s.id
                FROM symbols s
                WHERE s.file_id = sr.source_file_id
                  AND (
                    (sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name))
                    OR (sr.source_symbol_name IS NULL AND s.start_line <= sr.line_no AND s.end_line >= sr.line_no)
                  )
                ORDER BY
                    CASE
                        WHEN sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name)
                            THEN 0
                        ELSE 1
                    END,
                    CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
                    ABS(s.start_line - sr.line_no)
                LIMIT 1
            ) source_symbol ON TRUE
            JOIN symbols ts ON ts.id = sr.target_symbol_id
            JOIN files tf ON tf.id = ts.file_id
            WHERE sf.repo = %s
              AND tf.repo = %s
              AND sr.target_symbol_id IS NOT NULL
            GROUP BY source_symbol.id, sr.target_symbol_id
        ),
        all_edges AS (
            SELECT src, tgt, edge_weight FROM dep_edges
            UNION ALL
            SELECT src, tgt, edge_weight FROM ref_edges
        )
        SELECT src, tgt, SUM(edge_weight)::REAL AS edge_weight
        FROM all_edges
        WHERE src != tgt
        GROUP BY src, tgt
        """,
        (repo_name, repo_name, repo_name, repo_name),
    )
    for source_symbol_id, target_symbol_id, edge_weight in cur.fetchall():
        src = int(source_symbol_id)
        tgt = int(target_symbol_id)
        if src not in symbol_meta or tgt not in symbol_meta:
            continue
        if graph.has_edge(src, tgt):
            graph[src][tgt]["weight"] += float(edge_weight)
        else:
            graph.add_edge(src, tgt, weight=float(edge_weight))

    return graph, symbol_meta


def _build_file_cluster_graph(cur, repo_name: str) -> tuple[nx.Graph, dict[int, dict[str, str]]]:
    """@brief Build a file-level weighted graph for fallback clustering.

    @param cur Database cursor.
    @param repo_name Repository identifier.
    @return Tuple of undirected graph and file metadata keyed by file id.
    """
    cur.execute(
        """
        SELECT
            id,
            path,
            COALESCE(NULLIF(summary, ''), NULLIF(role, '')) AS context
        FROM files
        WHERE repo = %s
        """,
        (repo_name,),
    )
    file_meta: dict[int, dict[str, str]] = {}
    for file_id, path, context in cur.fetchall():
        file_meta[int(file_id)] = {
            "label": path,
            "context": (context or "").strip(),
            "path": path,
        }

    graph = nx.Graph()
    for file_id in file_meta:
        graph.add_node(file_id)

    cur.execute(
        """
        WITH dep_edges AS (
            SELECT
                d.source_file_id AS src,
                d.target_file_id AS tgt,
                COUNT(*)::REAL AS edge_weight
            FROM dependencies d
            JOIN files sf ON sf.id = d.source_file_id
            JOIN files tf ON tf.id = d.target_file_id
            WHERE d.target_file_id IS NOT NULL
              AND sf.repo = %s
              AND tf.repo = %s
            GROUP BY d.source_file_id, d.target_file_id
        ),
        ref_edges AS (
            SELECT
                sr.source_file_id AS src,
                ts.file_id AS tgt,
                COUNT(*)::REAL AS edge_weight
            FROM symbol_references sr
            JOIN files sf ON sf.id = sr.source_file_id
            JOIN symbols ts ON ts.id = sr.target_symbol_id
            JOIN files tf ON tf.id = ts.file_id
            WHERE sr.target_symbol_id IS NOT NULL
              AND sf.repo = %s
              AND tf.repo = %s
            GROUP BY sr.source_file_id, ts.file_id
        ),
        all_edges AS (
            SELECT src, tgt, edge_weight FROM dep_edges
            UNION ALL
            SELECT src, tgt, edge_weight FROM ref_edges
        )
        SELECT src, tgt, SUM(edge_weight)::REAL AS edge_weight
        FROM all_edges
        WHERE src != tgt
        GROUP BY src, tgt
        """,
        (repo_name, repo_name, repo_name, repo_name),
    )
    for source_file_id, target_file_id, edge_weight in cur.fetchall():
        src = int(source_file_id)
        tgt = int(target_file_id)
        if src not in file_meta or tgt not in file_meta:
            continue
        if graph.has_edge(src, tgt):
            graph[src][tgt]["weight"] += float(edge_weight)
        else:
            graph.add_edge(src, tgt, weight=float(edge_weight))

    return graph, file_meta


def _cluster_modularity_contribution(graph: nx.Graph, community: set[int]) -> float:
    """@brief Compute one community's contribution to weighted modularity.

    @param graph Weighted undirected graph.
    @param community Community node set.
    @return Modularity contribution for the given community.
    """
    if graph.number_of_edges() == 0:
        return 0.0
    total_weight = float(graph.size(weight="weight"))
    if total_weight <= 0:
        return 0.0
    subgraph = graph.subgraph(community)
    internal_weight = float(subgraph.size(weight="weight"))
    weighted_degree_sum = float(sum(graph.degree(node, weight="weight") for node in community))
    return (internal_weight / total_weight) - (weighted_degree_sum / (2.0 * total_weight)) ** 2


def _cluster_membership_weight(graph: nx.Graph, node: int, community: set[int]) -> float:
    """@brief Score how strongly a node belongs to its community.

    @param graph Weighted undirected graph.
    @param node Node id to score.
    @param community Community node set containing the node.
    @return Ratio of internal to total weighted degree in `[0, 1]`.
    """
    total_degree = float(graph.degree(node, weight="weight"))
    if total_degree <= 0:
        return 1.0
    internal_degree = 0.0
    for neighbor, attrs in graph[node].items():
        if neighbor in community:
            internal_degree += float(attrs.get("weight", 1.0))
    return max(0.0, min(1.0, internal_degree / total_degree))


def _build_cluster_prompt(
    granularity: str,
    cluster_key: str,
    context_rows: list[str],
) -> str:
    """@brief Build the LLM prompt for cluster naming and summary generation.

    @param granularity Cluster granularity label (`symbol` or `file`).
    @param cluster_key Persisted cluster key.
    @param context_rows Formatted member context rows.
    @return Prompt string.
    """
    member_label = "symbols" if granularity == "symbol" else "files"
    context_text = "\n".join(context_rows[:CLUSTER_MEMBER_CONTEXT_LIMIT])
    return f"""You are naming a code cluster discovered from dependency/reference graph structure.
Use an embedding+cochange-style strategy: infer one coherent domain theme from member semantics and coupling context.

Cluster key: {cluster_key}
Granularity: {granularity}
Members ({member_label}):
{context_text}

Respond with ONLY JSON:
{{
  "name": "<short human-readable cluster name, 2-6 words>",
  "summary": "<one paragraph (2-4 sentences) explaining what this cluster does>"
}}"""


def _parse_cluster_profile(
    classifier: IntentClassifier,
    prompt: str,
    fallback_name: str,
    fallback_summary: str,
    no_classify: bool,
) -> tuple[str, str]:
    """@brief Parse cluster name/summary from classifier output with robust fallback.

    @param classifier Intent classifier client.
    @param prompt Prompt text for the cluster.
    @param fallback_name Deterministic fallback cluster name.
    @param fallback_summary Deterministic fallback cluster summary.
    @param no_classify Whether classifier calls are disabled.
    @return Tuple of `(name, summary)`.
    """
    if no_classify:
        return fallback_name, fallback_summary
    try:
        payload = classifier._parse_json(classifier._generate(prompt, max_tokens=280))
        raw_name = str(payload.get("name", "")).strip() if isinstance(payload, dict) else ""
        raw_summary = str(payload.get("summary", "")).strip() if isinstance(payload, dict) else ""
        name = re.sub(r"\s+", " ", raw_name)[:80] if raw_name else fallback_name
        summary = re.sub(r"\s+", " ", raw_summary)[:800] if raw_summary else fallback_summary
        return name, summary
    except Exception:
        return fallback_name, fallback_summary


def _build_cluster_embedding_input(
    name: str,
    summary: str,
    members: list[str],
    granularity: str,
) -> str:
    """@brief Build bounded embedding input text for a cluster profile.

    @param name Cluster name.
    @param summary Cluster summary paragraph.
    @param members Ordered member labels for cluster context.
    @param granularity Cluster granularity label.
    @return Bounded text payload for embeddings.
    """
    members_text = "; ".join(members[:CLUSTER_MEMBER_CONTEXT_LIMIT])
    payload = (
        f"cluster:{name}\n"
        f"granularity:{granularity}\n"
        f"summary:{summary}\n"
        f"members:{members_text}"
    )
    return payload[:CLUSTER_SUMMARY_MAX_CHARS]


def _detect_communities(graph: nx.Graph, resolution: float) -> tuple[list[set[int]], str]:
    """@brief Detect graph communities with backend-safe fallback behavior.

    Prefers Leiden communities when the active NetworkX backend supports it.
    Falls back to Louvain, then connected components, to avoid hard failures
    during ingestion when optional backend features are unavailable.

    @param graph Weighted undirected graph for clustering.
    @param resolution Community-resolution parameter used by Leiden/Louvain.
    @return Tuple of `(communities, algorithm_name)` where communities are
            non-empty node sets.
    """
    try:
        communities = list(
            nx.community.leiden_communities(
                graph,
                weight="weight",
                resolution=resolution,
                seed=42,
            )
        )
        normalized = [set(community) for community in communities if len(community) > 0]
        if normalized:
            return normalized, "leiden"
    except (AttributeError, NotImplementedError):
        pass

    try:
        communities = list(
            nx.community.louvain_communities(
                graph,
                weight="weight",
                resolution=resolution,
                seed=42,
            )
        )
        normalized = [set(community) for community in communities if len(community) > 0]
        if normalized:
            return normalized, "louvain"
    except (AttributeError, NotImplementedError):
        pass

    return [set(component) for component in nx.connected_components(graph)], "connected_components"


def materialize_clusters(
    conn,
    repo_name: str,
    embedder: EmbeddingClient,
    classifier: IntentClassifier,
    no_classify: bool = False,
    resolution: float = 1.0,
) -> tuple[int, str]:
    """@brief Rebuild repository cluster rows using resilient community detection.

    @param conn Open database connection.
    @param repo_name Repository identifier.
    @param embedder Embedding client for cluster profile vectors.
    @param classifier Intent classifier for name/summary generation.
    @param no_classify Whether classifier calls are disabled.
    @param resolution Leiden/Louvain resolution parameter.
    @return Tuple of `(cluster_count, granularity)`.
    """
    cur = conn.cursor()
    symbol_graph, symbol_meta = _build_symbol_cluster_graph(cur, repo_name)
    use_symbol_clusters = symbol_graph.number_of_nodes() >= MIN_SYMBOL_CLUSTER_NODES and symbol_graph.number_of_edges() > 0
    graph, node_meta, granularity = (
        (symbol_graph, symbol_meta, "symbol")
        if use_symbol_clusters
        else (*_build_file_cluster_graph(cur, repo_name), "file")
    )

    cur.execute("DELETE FROM doc_links WHERE repo = %s AND target_kind = 'cluster'", (repo_name,))
    cur.execute("DELETE FROM clusters WHERE repo = %s", (repo_name,))
    if graph.number_of_nodes() == 0:
        conn.commit()
        return 0, granularity

    communities, community_algorithm = _detect_communities(graph, resolution)
    if community_algorithm != "leiden":
        console.print(
            f"  [yellow]![/] [dim]Leiden unavailable; using {community_algorithm} communities fallback.[/]"
        )
    communities.sort(key=lambda community: sorted(node_meta[node]["label"] for node in community)[0])

    cluster_count = 0
    for index, community in enumerate(communities, 1):
        cluster_key = f"{granularity}:{index:04d}"
        member_labels = sorted(node_meta[node]["label"] for node in community)
        context_rows = [
            f"- {node_meta[node]['label']}: {node_meta[node]['context']}"
            if node_meta[node]["context"]
            else f"- {node_meta[node]['label']}"
            for node in sorted(community, key=lambda node_id: node_meta[node_id]["label"])
        ]
        fallback_name = f"{granularity.title()} Cluster {index}"
        fallback_summary = f"Related {granularity} nodes grouped by coupling in repository {repo_name}."
        prompt = _build_cluster_prompt(granularity, cluster_key, context_rows)
        cluster_name, cluster_summary = _parse_cluster_profile(
            classifier=classifier,
            prompt=prompt,
            fallback_name=fallback_name,
            fallback_summary=fallback_summary,
            no_classify=no_classify,
        )
        cluster_embedding = embedder.embed(
            _build_cluster_embedding_input(cluster_name, cluster_summary, member_labels, granularity)
        )
        modularity = _cluster_modularity_contribution(graph, community)

        cur.execute(
            """
            INSERT INTO clusters (repo, cluster_key, name, summary, modularity, embedding, granularity)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                repo_name,
                cluster_key,
                cluster_name,
                cluster_summary,
                modularity,
                cluster_embedding,
                granularity,
            ),
        )
        cluster_id = int(cur.fetchone()[0])

        for node in community:
            membership_weight = _cluster_membership_weight(graph, node, community)
            if granularity == "symbol":
                cur.execute(
                    """
                    INSERT INTO cluster_members (cluster_id, symbol_id, membership_weight)
                    VALUES (%s, %s, %s)
                    """,
                    (cluster_id, node, membership_weight),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO cluster_members (cluster_id, file_id, membership_weight)
                    VALUES (%s, %s, %s)
                    """,
                    (cluster_id, node, membership_weight),
                )

        normalized_summary = _normalize_doc_link_content(cluster_summary)
        if normalized_summary:
            cur.execute(
                """
                INSERT INTO doc_links
                    (repo, source_file_id, source, source_path, target_kind, target_id, content, embedding)
                VALUES (%s, NULL, 'cluster', %s, 'cluster', %s, %s, %s)
                """,
                (
                    repo_name,
                    cluster_key,
                    cluster_id,
                    normalized_summary,
                    cluster_embedding,
                ),
            )
        cluster_count += 1

    conn.commit()
    return cluster_count, granularity

