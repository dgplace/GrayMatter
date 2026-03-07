#!./.venv/bin/python3
"""
@file synthesize_modules.py
@brief Module Intent Synthesis CLI.

Analyzes files, classes, and dependencies to synthesize directory-based and
logical modules with domain-specific narrative intents.

Logical modules are detected via weighted community detection on a class-level
coupling graph (Louvain algorithm with configurable resolution).  Hub classes
are dampened to prevent utility types from merging unrelated clusters, and
oversized communities are recursively split.
"""

import click
import networkx as nx
from rich.console import Console
from rich.progress import track

from ingest import load_config, get_db
from classifier import IntentClassifier

console = Console()

_CLASS_KINDS = ('class', 'struct', 'interface', 'protocol', 'enum')
_MIN_CLASS_SYMBOLS_FOR_CLASS_GRAPH = 5


# ── Graph helpers ────────────────────────────────────────────────────────────

def _dampen_hub_edges(G: nx.Graph, hub_percentile: float = 90.0) -> None:
    """@brief Reduce edge weights for high-degree hub nodes.

    Nodes above the hub_percentile degree threshold have all their edge weights
    scaled by median_degree / node_degree, preserving their strongest connections
    while weakening tenuous ones so they don't merge unrelated clusters.

    @param G Weighted undirected graph (modified in place).
    @param hub_percentile Degree percentile above which nodes are treated as hubs.
    """
    if len(G.nodes) < 3:
        return
    degrees = sorted(d for _, d in G.degree())
    threshold_idx = int(len(degrees) * hub_percentile / 100)
    hub_threshold = degrees[min(threshold_idx, len(degrees) - 1)]
    median_degree = degrees[len(degrees) // 2]

    if hub_threshold <= median_degree:
        return

    for node in list(G.nodes):
        deg = G.degree(node)
        if deg > hub_threshold:
            scale = median_degree / deg
            for neighbor in list(G.neighbors(node)):
                G[node][neighbor]['weight'] *= scale


def _split_oversized(G: nx.Graph, communities: list[set],
                     max_size: int, resolution: float) -> list[set]:
    """@brief Recursively sub-partition communities that exceed max_size.

    @param G The full weighted graph.
    @param communities Initial community partition.
    @param max_size Maximum allowed community size.
    @param resolution Current Louvain resolution (doubled on each recursion).
    @return Flat list of communities, all at or below max_size.
    """
    result = []
    for comm in communities:
        if len(comm) <= max_size:
            result.append(comm)
            continue
        subgraph = G.subgraph(comm).copy()
        sub_resolution = resolution * 2.0
        sub_communities = nx.community.louvain_communities(
            subgraph, weight='weight', resolution=sub_resolution, seed=42
        )
        if len(sub_communities) <= 1:
            result.append(comm)
            continue
        result.extend(
            _split_oversized(subgraph, list(sub_communities), max_size, sub_resolution)
        )
    return result


# ── Directory modules ────────────────────────────────────────────────────────

def synthesize_directory_modules(conn, repo: str, min_files: int,
                                 classifier: IntentClassifier,
                                 machine: bool = False):
    """@brief Synthesize one module_intents row per directory with narrative intent.

    @param conn Database connection.
    @param repo Repository name.
    @param min_files Minimum files for a directory to qualify as a module.
    @param classifier IntentClassifier for LLM-based summarization.
    @param machine Emit machine-readable progress lines instead of rich progress.
    """
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM module_intents WHERE repo = %s AND kind = 'directory'",
        (repo,),
    )

    cur.execute("""
        SELECT
            f.path,
            f.summary AS file_summary,
            f.role AS file_role,
            COUNT(c.id) AS chunk_count,
            STRING_AGG(DISTINCT c.intent_detail, ' | ') AS intent_details
        FROM files f
        LEFT JOIN code_chunks c ON c.file_id = f.id
        WHERE f.repo = %s
        GROUP BY f.id, f.path, f.summary, f.role
    """, (repo,))

    directories: dict[str, dict] = {}

    for path, summary, role, chunk_count, intent_details in cur.fetchall():
        dir_path = path.rsplit('/', 1)[0] if '/' in path else '.'
        if dir_path not in directories:
            directories[dir_path] = {
                'files': [],
                'chunk_count': 0,
                'intent_details': [],
            }
        directories[dir_path]['files'].append({
            'path': path,
            'summary': summary,
            'role': role,
        })
        directories[dir_path]['chunk_count'] += chunk_count
        if intent_details:
            directories[dir_path]['intent_details'].append(intent_details)

    eligible = {k: v for k, v in directories.items() if len(v['files']) >= min_files}
    total_dirs = len(eligible)

    if machine:
        print(f"SYNTH:dir:0:{total_dirs}", flush=True)

    items = eligible.items()
    if not machine:
        items = track(items, total=total_dirs,
                      description="Synthesizing directory modules...")

    for idx, (dir_path, data) in enumerate(items, 1):
        if machine:
            print(f"SYNTH:dir:{idx}:{total_dirs}", flush=True)

        files_context = "\n".join(
            f"- {f['path']}: {f['role']} — {f['summary']}"
            for f in data['files'][:20]
        )
        details_context = "\n".join(data['intent_details'][:10])

        prompt = f"""You are reading the source code of an application like chapters of a book.
This directory groups related files. Describe the STORY — what is this directory
trying to accomplish? What problem is it solving?

Directory: {dir_path}

Files:
{files_context}

What the code does (from chunk analysis):
{details_context}

Think of dominant_intent as the chapter summary — it should tell the reader what happens
in this part of the application and why it matters.

BAD intents (too generic):
- "Handles business logic for the application"
- "Provides utility functions"
- "Manages data models"

GOOD intents (tells the story):
- "Orchestrates customer order fulfillment by validating inventory, calculating shipping, and dispatching to warehouse systems"
- "Parses source code into an AST, extracts semantic chunks, and resolves cross-file symbol references for code intelligence indexing"

Respond with ONLY this JSON object:
{{
  "summary": "<1-2 sentences summarizing what this directory module does>",
  "role": "<architectural role>",
  "dominant_intent": "<the story: what is this module trying to accomplish and why?>"
}}"""

        try:
            res = classifier._parse_json(classifier._generate(prompt, max_tokens=250))
            summary = res.get("summary", "")
            role = res.get("role", "unknown")
            dominant_intent = res.get("dominant_intent", "")
        except Exception:
            summary = "Directory module"
            role = "module"
            dominant_intent = ""

        cur.execute("""
            INSERT INTO module_intents
                (repo, module_path, kind, module_name, summary, role,
                 dominant_intent, file_count, chunk_count, updated_at)
            VALUES (%s, %s, 'directory', %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (repo, module_path) DO UPDATE SET
                kind = EXCLUDED.kind,
                module_name = EXCLUDED.module_name,
                summary = EXCLUDED.summary,
                role = EXCLUDED.role,
                dominant_intent = EXCLUDED.dominant_intent,
                file_count = EXCLUDED.file_count,
                chunk_count = EXCLUDED.chunk_count,
                updated_at = NOW()
        """, (
            repo, dir_path, dir_path.split('/')[-1],
            summary, role, dominant_intent,
            len(data['files']), data['chunk_count'],
        ))

    conn.commit()


# ── Logical modules ──────────────────────────────────────────────────────────

def _build_class_graph(cur, repo: str) -> tuple[nx.Graph, dict]:
    """@brief Build a weighted coupling graph at the class/type level.

    Nodes are symbol IDs for classes, structs, interfaces, protocols, and enums.
    Edges come from symbol-to-symbol dependencies and symbol references, weighted
    by the number of distinct coupling points.

    @param cur Database cursor.
    @param repo Repository name.
    @return Tuple of (graph, symbol_meta dict keyed by symbol ID).
    """
    kind_placeholders = ','.join(f"'{k}'" for k in _CLASS_KINDS)

    # Fetch class-level symbol metadata
    cur.execute(f"""
        SELECT s.id, s.name, s.qualified_name, s.kind, s.docstring, f.path,
               cc.intent_detail
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        LEFT JOIN code_chunks cc ON cc.id = s.chunk_id
        WHERE f.repo = %s
          AND s.kind IN ({kind_placeholders})
          AND s.parent_id IS NULL
    """, (repo,))

    symbol_meta = {}
    for row in cur.fetchall():
        sid, name, qname, kind, docstring, path, intent_detail = row
        symbol_meta[sid] = {
            'name': name,
            'qualified_name': qname,
            'kind': kind,
            'docstring': docstring,
            'path': path,
            'intent_detail': intent_detail,
        }

    if len(symbol_meta) < _MIN_CLASS_SYMBOLS_FOR_CLASS_GRAPH:
        return nx.Graph(), symbol_meta

    class_ids = set(symbol_meta.keys())

    # Build weighted edges between class symbols
    cur.execute("""
        WITH dep_edges AS (
            SELECT d.source_symbol_id AS src, d.target_symbol_id AS tgt
            FROM dependencies d
            WHERE d.source_symbol_id IS NOT NULL
              AND d.target_symbol_id IS NOT NULL
        ),
        ref_edges AS (
            SELECT DISTINCT
                COALESCE(src_parent.id, src_sym.id) AS src,
                tgt_sym.id AS tgt
            FROM symbol_references sr
            JOIN symbols src_sym ON src_sym.name = sr.source_symbol_name
                AND src_sym.file_id = sr.source_file_id
            JOIN symbols tgt_sym ON lower(tgt_sym.name) = lower(sr.target_name)
            LEFT JOIN symbols src_parent ON src_parent.id = src_sym.parent_id
        ),
        all_edges AS (
            SELECT src, tgt FROM dep_edges
            UNION ALL
            SELECT src, tgt FROM ref_edges
        )
        SELECT src, tgt, COUNT(*) AS weight
        FROM all_edges
        WHERE src != tgt
        GROUP BY src, tgt
    """)

    G = nx.Graph()
    for src, tgt, weight in cur.fetchall():
        if src not in class_ids or tgt not in class_ids:
            continue
        if G.has_edge(src, tgt):
            G[src][tgt]['weight'] += weight
        else:
            G.add_edge(src, tgt, weight=weight)

    return G, symbol_meta


def _build_file_graph(cur, repo: str) -> tuple[nx.Graph, dict]:
    """@brief Fallback: build a weighted coupling graph at the file level.

    Used when the repo has too few class-level symbols for meaningful
    class-level clustering.

    @param cur Database cursor.
    @param repo Repository name.
    @return Tuple of (graph, file_meta dict keyed by file path).
    """
    cur.execute("""
        WITH reference_edges AS (
            SELECT sf.path AS source, tf.path AS target
            FROM symbol_references sr
            JOIN files sf ON sf.id = sr.source_file_id
            JOIN symbols s ON lower(s.name) = lower(sr.target_name)
            JOIN files tf ON tf.id = s.file_id
            WHERE sf.repo = %s AND tf.repo = %s AND sf.id != tf.id
        ),
        dependency_edges AS (
            SELECT sf.path AS source, tf.path AS target
            FROM dependencies d
            JOIN files sf ON sf.id = d.source_file_id
            JOIN files tf ON tf.id = d.target_file_id
            WHERE sf.repo = %s AND tf.repo = %s AND sf.id != tf.id
        ),
        all_edges AS (
            SELECT source, target FROM reference_edges
            UNION ALL
            SELECT source, target FROM dependency_edges
        )
        SELECT source, target, COUNT(*) AS weight
        FROM all_edges
        GROUP BY source, target
    """, (repo, repo, repo, repo))

    G = nx.Graph()
    for source, target, weight in cur.fetchall():
        if G.has_edge(source, target):
            G[source][target]['weight'] += weight
        else:
            G.add_edge(source, target, weight=weight)

    # Fetch file metadata for prompt context
    cur.execute("""
        SELECT f.path, f.summary, f.role,
               COUNT(c.id) AS chunk_count,
               STRING_AGG(DISTINCT c.intent_detail, ' | ') AS intent_details
        FROM files f
        LEFT JOIN code_chunks c ON c.file_id = f.id
        WHERE f.repo = %s
        GROUP BY f.id, f.path, f.summary, f.role
    """, (repo,))

    file_meta = {}
    for path, summary, role, chunk_count, intent_details in cur.fetchall():
        file_meta[path] = {
            'name': path.rsplit('/', 1)[-1],
            'path': path,
            'summary': summary,
            'role': role,
            'chunk_count': chunk_count,
            'intent_detail': intent_details,
        }

    return G, file_meta


def _build_community_context(community_nodes, meta: dict,
                             is_class_level: bool) -> tuple[str, list[str], int]:
    """@brief Build the LLM prompt context and metadata for one community.

    @param community_nodes Set of node IDs (symbol IDs or file paths).
    @param meta Metadata dict keyed by node ID.
    @param is_class_level True if nodes are class symbols, False if files.
    @return Tuple of (context_string, member_names, total_chunks).
    """
    context_lines = []
    member_names = []
    total_chunks = 0

    for node in community_nodes:
        m = meta.get(node)
        if not m:
            continue
        name = m.get('name', str(node))
        member_names.append(name)
        detail = m.get('intent_detail') or m.get('docstring') or m.get('summary') or ''
        if is_class_level:
            context_lines.append(
                f"- {name} ({m['kind']}, {m['path']}): {detail}"
            )
        else:
            context_lines.append(
                f"- {m['path']}: {m.get('role', '')} — {detail}"
            )
            total_chunks += m.get('chunk_count', 0)

    return "\n".join(context_lines[:30]), member_names, total_chunks


def synthesize_logical_modules(conn, repo: str, min_files: int,
                               classifier: IntentClassifier,
                               resolution: float = 1.5,
                               max_community_size: int = 20,
                               hub_percentile: float = 90.0,
                               machine: bool = False):
    """@brief Detect cross-directory logical modules via weighted community detection.

    Builds a weighted coupling graph at class level (falling back to file level),
    dampens hub nodes, runs Louvain community detection, recursively splits
    oversized communities, and synthesizes narrative-driven intents via LLM.

    @param conn Database connection.
    @param repo Repository name.
    @param min_files Minimum members for a community to become a module.
    @param classifier IntentClassifier for LLM-based naming and summarization.
    @param resolution Louvain resolution parameter (higher = smaller communities).
    @param max_community_size Max members per module before recursive splitting.
    @param hub_percentile Degree percentile above which nodes get dampened edges.
    @param machine Emit machine-readable progress lines instead of rich progress.
    """
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM module_intents WHERE repo = %s AND kind = 'logical'",
        (repo,),
    )

    # Try class-level graph first, fall back to file-level
    G, meta = _build_class_graph(cur, repo)
    is_class_level = len(G.nodes) > 0

    if not is_class_level:
        if not machine:
            console.print("[dim]Few class-level symbols; falling back to file-level graph[/]")
        G, meta = _build_file_graph(cur, repo)

    if len(G.nodes) == 0:
        conn.commit()
        return

    if not machine:
        console.print(
            f"[dim]Graph: {len(G.nodes)} nodes, {len(G.edges)} edges "
            f"({'class-level' if is_class_level else 'file-level'})[/]"
        )

    _dampen_hub_edges(G, hub_percentile)

    raw_communities = nx.community.louvain_communities(
        G, weight='weight', resolution=resolution, seed=42
    )
    communities = _split_oversized(
        G, list(raw_communities), max_community_size, resolution
    )

    total_communities = len(communities)
    if machine:
        print(f"SYNTH:logical:0:{total_communities}", flush=True)

    items = enumerate(communities)
    if not machine:
        items = enumerate(track(communities,
                                description="Synthesizing logical modules..."))

    for i, comm in items:
        if machine:
            print(f"SYNTH:logical:{i + 1}:{total_communities}", flush=True)
        if len(comm) < min_files:
            continue

        # For class-level: require classes from multiple directories
        if is_class_level:
            dirs = {meta[n]['path'].rsplit('/', 1)[0] if '/' in meta[n]['path'] else '.'
                    for n in comm if n in meta}
        else:
            dirs = {n.rsplit('/', 1)[0] if '/' in n else '.' for n in comm}
        if len(dirs) <= 1:
            continue

        context_str, member_names, total_chunks = _build_community_context(
            comm, meta, is_class_level
        )

        if not context_str:
            continue

        entity_label = "classes/types" if is_class_level else "files"
        prompt = f"""You are reading the source code of an application like reading chapters of a book.
These {entity_label} work together as one logical module. Your job is to describe the STORY —
what is this code trying to accomplish? What problem is it solving? What is the narrative arc?

{entity_label.capitalize()} in this module:
{context_str}

Think of dominant_intent as the chapter summary of a book — it should tell the reader
what happens in this part of the application and why it matters.

BAD intents (too generic, tells the reader nothing):
- "Handles business logic for the application"
- "Provides utility functions"
- "Manages data models"

GOOD intents (tells the story):
- "Orchestrates customer order fulfillment by validating inventory, calculating shipping, and dispatching to warehouse systems"
- "Manages the OAuth2 token lifecycle — acquiring tokens, refreshing expired sessions, revoking access, and enforcing scope boundaries"
- "Parses source code into an AST, extracts semantic chunks, and resolves cross-file symbol references for code intelligence indexing"

Respond with ONLY this JSON:
{{
  "module_name": "<domain-specific kebab-case slug>",
  "summary": "<1-2 sentences on what these {entity_label} do together>",
  "role": "<architectural role>",
  "dominant_intent": "<the story: what is this module trying to accomplish and why?>"
}}"""

        try:
            res = classifier._parse_json(classifier._generate(prompt, max_tokens=300))
            module_name = res.get("module_name", f"logical-{i}")
            summary = res.get("summary", "")
            role = res.get("role", "unknown")
            dominant_intent = res.get("dominant_intent", "")
        except Exception:
            module_name = f"logical-{i}"
            summary = "Logical module"
            role = "module"
            dominant_intent = ""

        module_path = f"_logical/{module_name}"

        # Count files covered by this community
        if is_class_level:
            file_paths = {meta[n]['path'] for n in comm if n in meta}
            file_count = len(file_paths)
        else:
            file_count = len(comm)

        cur.execute("""
            INSERT INTO module_intents
                (repo, module_path, kind, module_name, summary, role,
                 dominant_intent, file_count, chunk_count, member_symbols,
                 updated_at)
            VALUES (%s, %s, 'logical', %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (repo, module_path) DO UPDATE SET
                kind = EXCLUDED.kind,
                module_name = EXCLUDED.module_name,
                summary = EXCLUDED.summary,
                role = EXCLUDED.role,
                dominant_intent = EXCLUDED.dominant_intent,
                file_count = EXCLUDED.file_count,
                chunk_count = EXCLUDED.chunk_count,
                member_symbols = EXCLUDED.member_symbols,
                updated_at = NOW()
        """, (
            repo, module_path, module_name, summary, role, dominant_intent,
            file_count, total_chunks, member_names or None,
        ))

    conn.commit()


# ── CLI entry point ──────────────────────────────────────────────────────────

@click.command()
@click.option("--repo", required=True, help="Repository name")
@click.option("--mode", type=click.Choice(['directory', 'logical', 'all']),
              default='all', help="Synthesis mode")
@click.option("--min-files", default=3, help="Minimum files per module")
@click.option("--resolution", default=None, type=float,
              help="Louvain resolution (higher = smaller communities, default 1.5)")
@click.option("--max-community-size", default=None, type=int,
              help="Max members per module before recursive splitting (default 20)")
@click.option("--hub-percentile", default=None, type=float,
              help="Degree percentile for hub dampening (default 90.0)")
@click.option("--config", default="codebrain.toml", help="Config file path")
@click.option("--machine", is_flag=True, default=False,
              help="Emit machine-readable progress lines (for desktop app)")
def main(repo: str, mode: str, min_files: int,
         resolution: float | None, max_community_size: int | None,
         hub_percentile: float | None, config: str, machine: bool):
    """@brief Synthesize module intents for a repository."""
    cfg = load_config(config)
    conn = get_db(cfg)
    classifier = IntentClassifier(cfg)

    synthesis_cfg = cfg.get("synthesis", {})
    effective_resolution = resolution or synthesis_cfg.get("resolution", 1.5)
    effective_max_size = max_community_size or synthesis_cfg.get("max_community_size", 20)
    effective_hub_pct = hub_percentile or synthesis_cfg.get("hub_percentile", 90.0)

    if not machine:
        console.print(f"Synthesizing modules for [bold]{repo}[/] (mode: {mode})")

    if mode in ('directory', 'all'):
        synthesize_directory_modules(conn, repo, min_files, classifier,
                                     machine=machine)

    if mode in ('logical', 'all'):
        synthesize_logical_modules(
            conn, repo, min_files, classifier,
            resolution=effective_resolution,
            max_community_size=effective_max_size,
            hub_percentile=effective_hub_pct,
            machine=machine,
        )

    if machine:
        print("SYNTH:complete", flush=True)
    else:
        console.print("[bold green]Synthesis complete![/]")


if __name__ == '__main__':
    main()
