#!./.venv/bin/python3
"""
@file synthesize_modules.py
@brief Module Intent Synthesis CLI.

Analyzes files and existing repository clusters to synthesize directory-based and
logical modules with domain-specific narrative intents.

Logical modules are derived from the repository's existing `clusters` /
`cluster_members` rows (produced by the ingestion pipeline using Leiden with
Louvain/connected-components fallback). Synthesis filters those clusters down to
eligible communities and overlays a narrative `dominant_intent`
via the LLM. There is no separate community-detection pass at synthesis time; the
ingestion-built Leiden clustering is the single source of truth for coupling-based
communities.
"""

import sys

import click
from rich.console import Console
from rich.progress import track

# Allow direct script execution (`codebrain/synthesize_modules.py ...`) by
# ensuring the repo root is on sys.path before importing sibling modules.
if __package__ in (None, ""):
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from codebrain.ingest import load_config, get_db
from codebrain.classifier import IntentClassifier

console = Console()

_LOGICAL_MEMBER_CONTEXT_LIMIT = 30


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


# ── Logical modules (overlay on Leiden clusters) ─────────────────────────────

def _fetch_cluster_candidates(cur, repo: str) -> list[dict]:
    """@brief Load symbol-granularity clusters with their member symbols.

    Symbol clusters are preferred because they encode coupling at the type/class
    level. When a repository has no symbol clusters, file-level clusters are used
    instead.

    @param cur Database cursor.
    @param repo Repository name.
    @return List of cluster dicts with id, name, summary, granularity, and a
            members list of dicts (label, kind, path, context).
    """
    cur.execute(
        """
        SELECT id FROM clusters
        WHERE repo = %s AND granularity = 'symbol'
        LIMIT 1
        """,
        (repo,),
    )
    granularity = 'symbol' if cur.fetchone() else 'file'

    cur.execute(
        """
        SELECT id, cluster_key, name, summary
        FROM clusters
        WHERE repo = %s AND granularity = %s
        ORDER BY id
        """,
        (repo, granularity),
    )
    clusters = [
        {
            'id': int(row[0]),
            'cluster_key': row[1],
            'name': row[2],
            'summary': row[3],
            'granularity': granularity,
            'members': [],
        }
        for row in cur.fetchall()
    ]
    if not clusters:
        return []

    cluster_ids = [c['id'] for c in clusters]
    by_id = {c['id']: c for c in clusters}

    if granularity == 'symbol':
        cur.execute(
            """
            SELECT
                cm.cluster_id,
                s.name,
                s.kind,
                f.path,
                COALESCE(
                    NULLIF(s.docstring, ''),
                    NULLIF(cc.intent_detail, ''),
                    NULLIF(f.summary, '')
                ) AS context
            FROM cluster_members cm
            JOIN symbols s ON s.id = cm.symbol_id
            JOIN files f ON f.id = s.file_id
            LEFT JOIN code_chunks cc ON cc.id = s.chunk_id
            WHERE cm.cluster_id = ANY(%s)
            ORDER BY cm.membership_weight DESC NULLS LAST, f.path, s.name
            """,
            (cluster_ids,),
        )
        for cluster_id, name, kind, path, context in cur.fetchall():
            by_id[int(cluster_id)]['members'].append({
                'label': name,
                'kind': kind,
                'path': path,
                'context': (context or '').strip(),
            })
    else:
        cur.execute(
            """
            SELECT
                cm.cluster_id,
                f.path,
                f.role,
                COALESCE(NULLIF(f.summary, ''), NULLIF(f.role, '')) AS context,
                (SELECT COUNT(*) FROM code_chunks cc WHERE cc.file_id = f.id) AS chunk_count
            FROM cluster_members cm
            JOIN files f ON f.id = cm.file_id
            WHERE cm.cluster_id = ANY(%s)
            ORDER BY cm.membership_weight DESC NULLS LAST, f.path
            """,
            (cluster_ids,),
        )
        for cluster_id, path, role, context, chunk_count in cur.fetchall():
            by_id[int(cluster_id)]['members'].append({
                'label': path.rsplit('/', 1)[-1],
                'kind': role or 'file',
                'path': path,
                'context': (context or '').strip(),
                'chunk_count': int(chunk_count or 0),
            })

    return clusters


def _cluster_file_count(members: list[dict], granularity: str) -> int:
    """@brief Number of distinct files covered by a cluster's members.

    @param members Cluster members.
    @param granularity 'symbol' or 'file'.
    @return Distinct file count.
    """
    if granularity == 'symbol':
        return len({m['path'] for m in members})
    return len(members)


def _cluster_chunk_count(cur, repo: str, members: list[dict],
                         granularity: str) -> int:
    """@brief Total code-chunk count across a cluster's distinct files.

    @param cur Database cursor.
    @param repo Repository name.
    @param members Cluster members.
    @param granularity 'symbol' or 'file'.
    @return Aggregate chunk count.
    """
    if granularity == 'file':
        return sum(m.get('chunk_count', 0) for m in members)
    paths = list({m['path'] for m in members})
    if not paths:
        return 0
    cur.execute(
        """
        SELECT COUNT(*)
        FROM code_chunks cc
        JOIN files f ON f.id = cc.file_id
        WHERE f.repo = %s AND f.path = ANY(%s)
        """,
        (repo, paths),
    )
    return int(cur.fetchone()[0] or 0)


def _build_logical_module_prompt(cluster: dict) -> str:
    """@brief Build the LLM prompt used to name and describe one logical module.

    @param cluster Cluster record with members and existing name/summary.
    @return Prompt string for classifier generation.
    """
    granularity = cluster['granularity']
    entity_label = "classes/types" if granularity == 'symbol' else "files"
    members = cluster['members'][:_LOGICAL_MEMBER_CONTEXT_LIMIT]
    if granularity == 'symbol':
        member_lines = [
            f"- {m['label']} ({m['kind']}, {m['path']}): {m['context']}"
            if m['context']
            else f"- {m['label']} ({m['kind']}, {m['path']})"
            for m in members
        ]
    else:
        member_lines = [
            f"- {m['path']}: {m['kind']} — {m['context']}"
            if m['context']
            else f"- {m['path']}: {m['kind']}"
            for m in members
        ]
    context_str = "\n".join(member_lines)

    existing = ""
    if cluster.get('name') or cluster.get('summary'):
        existing = (
            "\nCluster profile from coupling analysis:\n"
            f"- name: {cluster.get('name') or '(none)'}\n"
            f"- summary: {cluster.get('summary') or '(none)'}\n"
        )

    return f"""You are reading the source code of an application like reading chapters of a book.
These {entity_label} were detected as one community by Leiden coupling analysis. Your job is to
describe the STORY — what is this code trying to accomplish? What
problem is it solving? What is the narrative arc?
{existing}
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


def _parse_logical_module_metadata(
    classifier: IntentClassifier,
    prompt: str,
    fallback_slug: str,
    fallback_summary: str,
) -> tuple[str, str, str, str]:
    """@brief Parse classifier JSON output for logical module metadata.

    @param classifier Intent classifier client.
    @param prompt Prompt text for the classifier model.
    @param fallback_slug Deterministic fallback module slug.
    @param fallback_summary Deterministic fallback summary text.
    @return Tuple of module_name, summary, role, and dominant_intent.
    """
    try:
        res = classifier._parse_json(classifier._generate(prompt, max_tokens=300))
        return (
            res.get("module_name", fallback_slug),
            res.get("summary", fallback_summary),
            res.get("role", "unknown"),
            res.get("dominant_intent", ""),
        )
    except Exception:
        return (fallback_slug, fallback_summary, "module", "")


def _upsert_logical_module_intent(
    cur,
    repo: str,
    module_path: str,
    module_name: str,
    summary: str,
    role: str,
    dominant_intent: str,
    file_count: int,
    chunk_count: int,
    member_names: list[str],
    cluster_id: int,
) -> None:
    """@brief Upsert one logical module_intents record bound to a cluster.

    @param cur Database cursor.
    @param repo Repository name.
    @param module_path Persisted module path key (e.g. `_logical/<slug>`).
    @param module_name Display module name.
    @param summary Module summary text.
    @param role Architectural role label.
    @param dominant_intent Narrative intent sentence.
    @param file_count Number of covered files.
    @param chunk_count Aggregate chunk count represented by this cluster.
    @param member_names Ordered member labels for quick get_module_map rendering.
    @param cluster_id Source cluster id from the `clusters` table.
    """
    cur.execute("""
        INSERT INTO module_intents
            (repo, module_path, kind, module_name, summary, role,
             dominant_intent, file_count, chunk_count, member_symbols,
             cluster_id, updated_at)
        VALUES (%s, %s, 'logical', %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (repo, module_path) DO UPDATE SET
            kind = EXCLUDED.kind,
            module_name = EXCLUDED.module_name,
            summary = EXCLUDED.summary,
            role = EXCLUDED.role,
            dominant_intent = EXCLUDED.dominant_intent,
            file_count = EXCLUDED.file_count,
            chunk_count = EXCLUDED.chunk_count,
            member_symbols = EXCLUDED.member_symbols,
            cluster_id = EXCLUDED.cluster_id,
            updated_at = NOW()
    """, (
        repo,
        module_path,
        module_name,
        summary,
        role,
        dominant_intent,
        file_count,
        chunk_count,
        member_names or None,
        cluster_id,
    ))


def synthesize_logical_modules(conn, repo: str, min_files: int,
                               classifier: IntentClassifier,
                               machine: bool = False):
    """@brief Overlay narrative intents on ingestion-produced clusters.

    Reads existing `clusters` / `cluster_members` (produced by ingestion using
    Leiden) and promotes the eligible ones to logical
    modules. There is no second clustering pass at synthesis time.

    @param conn Database connection.
    @param repo Repository name.
    @param min_files Minimum distinct files for a cluster to qualify.
    @param classifier IntentClassifier for LLM-based naming and summarization.
    @param machine Emit machine-readable progress lines instead of rich progress.
    """
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM module_intents WHERE repo = %s AND kind = 'logical'",
        (repo,),
    )

    clusters = _fetch_cluster_candidates(cur, repo)
    if not clusters:
        conn.commit()
        return

    candidates = []
    for cluster in clusters:
        members = cluster['members']
        if not members:
            continue
        file_count = _cluster_file_count(members, cluster['granularity'])
        if file_count < min_files:
            continue
        candidates.append((cluster, file_count))

    total = len(candidates)
    if machine:
        print(f"SYNTH:logical:0:{total}", flush=True)

    if not machine:
        iterator = track(
            enumerate(candidates),
            total=total,
            description="Synthesizing logical modules...",
        )
    else:
        iterator = enumerate(candidates)

    used_slugs: set[str] = set()
    for idx, (cluster, file_count) in iterator:
        if machine:
            print(f"SYNTH:logical:{idx + 1}:{total}", flush=True)

        fallback_slug = f"logical-{cluster['id']}"
        fallback_summary = cluster.get('summary') or "Logical module"
        prompt = _build_logical_module_prompt(cluster)
        module_name, summary, role, dominant_intent = _parse_logical_module_metadata(
            classifier, prompt, fallback_slug, fallback_summary,
        )
        slug = module_name or fallback_slug
        if slug in used_slugs:
            slug = f"{slug}-{cluster['id']}"
        used_slugs.add(slug)

        member_names = [m['label'] for m in cluster['members']]
        chunk_count = _cluster_chunk_count(cur, repo, cluster['members'], cluster['granularity'])

        _upsert_logical_module_intent(
            cur=cur,
            repo=repo,
            module_path=f"_logical/{slug}",
            module_name=slug,
            summary=summary,
            role=role,
            dominant_intent=dominant_intent,
            file_count=file_count,
            chunk_count=chunk_count,
            member_names=member_names,
            cluster_id=cluster['id'],
        )

    conn.commit()


# ── CLI entry point ──────────────────────────────────────────────────────────

@click.command()
@click.option("--repo", required=True, help="Repository name")
@click.option("--mode", type=click.Choice(['directory', 'logical', 'all']),
              default='all', help="Synthesis mode")
@click.option("--min-files", default=1, show_default=True, help="Minimum files per module")
@click.option("--config", default="codebrain.toml", help="Config file path")
@click.option("--machine", is_flag=True, default=False,
              help="Emit machine-readable progress lines (for desktop app)")
def main(repo: str, mode: str, min_files: int, config: str, machine: bool):
    """@brief Synthesize module intents for a repository."""
    cfg = load_config(config)
    conn = get_db(cfg)
    classifier = IntentClassifier(cfg)

    if not machine:
        console.print(f"Synthesizing modules for [bold]{repo}[/] (mode: {mode})")

    if mode in ('directory', 'all'):
        synthesize_directory_modules(conn, repo, min_files, classifier,
                                     machine=machine)

    if mode in ('logical', 'all'):
        synthesize_logical_modules(
            conn, repo, min_files, classifier, machine=machine,
        )

    if machine:
        print("SYNTH:complete", flush=True)
    else:
        console.print("[bold green]Synthesis complete![/]")


if __name__ == '__main__':
    main()
