#!./.venv/bin/python3
"""
Module Intent Synthesis CLI
Analyzes files and dependencies to synthesize directory-based and logical modules.
"""

import click
import networkx as nx
from rich.console import Console
from rich.progress import track

from ingest import load_config, get_db
from classifier import IntentClassifier

console = Console()

def synthesize_directory_modules(conn, repo: str, min_files: int, classifier: IntentClassifier):
    cur = conn.cursor()
    
    cur.execute("""
        SELECT 
            f.path,
            f.summary as file_summary,
            f.role as file_role,
            COUNT(c.id) as chunk_count,
            STRING_AGG(c.intent, ', ') as intents
        FROM files f
        LEFT JOIN code_chunks c ON c.file_id = f.id
        WHERE f.repo = %s
        GROUP BY f.id, f.path, f.summary, f.role
    """, (repo,))
    
    directories = {}
    
    for row in cur.fetchall():
        path, summary, role, chunk_count, intents = row
        if '/' in path:
            dir_path = path.rsplit('/', 1)[0]
        else:
            dir_path = '.'
            
        if dir_path not in directories:
            directories[dir_path] = {
                'files': [],
                'chunk_count': 0,
                'intents': []
            }
            
        directories[dir_path]['files'].append({
            'path': path,
            'summary': summary,
            'role': role
        })
        directories[dir_path]['chunk_count'] += chunk_count
        if intents:
            directories[dir_path]['intents'].extend([i.strip() for i in intents.split(',') if i.strip()])
            
    for dir_path, data in track(directories.items(), description="Synthesizing directory modules..."):
        if len(data['files']) < min_files:
            continue
            
        intent_counts = {}
        for intent in data['intents']:
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
        dominant_intent = max(intent_counts.items(), key=lambda x: x[1])[0] if intent_counts else 'unknown'
        
        files_context = "\n".join([f"- {f['path']}: {f['role']} - {f['summary']}" for f in data['files'][:20]])
        prompt = f"""Analyze this directory module.

Files:
{files_context}

Respond with ONLY this JSON object:
{{
  "summary": "<1-2 sentences on what this directory module does>",
  "role": "<architectural role>",
  "dominant_intent": "<a full sentence describing the primary intent or purpose of this module>"
}}"""

        try:
            res = classifier._parse_json(classifier._generate(prompt, max_tokens=200))
            summary = res.get("summary", "")
            role = res.get("role", "unknown")
            dominant_intent = res.get("dominant_intent", dominant_intent)
        except Exception:
            summary = "Directory module"
            role = "module"

        cur.execute("""
            INSERT INTO module_intents (repo, module_path, kind, module_name, summary, role, dominant_intent, file_count, chunk_count, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
            repo, dir_path, 'directory', dir_path.split('/')[-1], summary, role, dominant_intent, len(data['files']), data['chunk_count']
        ))
        
    conn.commit()

def synthesize_logical_modules(conn, repo: str, min_files: int, classifier: IntentClassifier):
    cur = conn.cursor()
    cur.execute("""
        WITH reference_edges AS (
            SELECT sf.path as source, tf.path as target
            FROM symbol_references sr
            JOIN files sf ON sf.id = sr.source_file_id
            JOIN symbols s ON lower(s.name) = lower(sr.target_name)
            JOIN files tf ON tf.id = s.file_id
            WHERE sf.repo = %s AND tf.repo = %s AND sf.id != tf.id
        ),
        dependency_edges AS (
            SELECT sf.path as source, tf.path as target
            FROM dependencies d
            JOIN files sf ON sf.id = d.source_file_id
            JOIN files tf ON tf.id = d.target_file_id
            WHERE sf.repo = %s AND tf.repo = %s AND sf.id != tf.id
        )
        SELECT source, target FROM reference_edges
        UNION
        SELECT source, target FROM dependency_edges
    """, (repo, repo, repo, repo))
    
    G = nx.Graph()
    for row in cur.fetchall():
        G.add_edge(row[0], row[1])
        
    if len(G.nodes) == 0:
        return
        
    communities = nx.community.greedy_modularity_communities(G)
    
    cur.execute("""
        SELECT 
            f.path,
            f.summary as file_summary,
            f.role as file_role,
            COUNT(c.id) as chunk_count,
            STRING_AGG(c.intent, ', ') as intents
        FROM files f
        LEFT JOIN code_chunks c ON c.file_id = f.id
        WHERE f.repo = %s
        GROUP BY f.id, f.path, f.summary, f.role
    """, (repo,))
    file_meta = {}
    for row in cur.fetchall():
        path, summary, role, chunk_count, intents = row
        file_meta[path] = {
            'summary': summary,
            'role': role,
            'chunk_count': chunk_count,
            'intents': [i.strip() for i in (intents or '').split(',') if i.strip()]
        }
    
    for i, comm in enumerate(track(communities, description="Synthesizing logical modules...")):
        files = list(comm)
        if len(files) < min_files:
            continue
            
        dirs = {f.rsplit('/', 1)[0] if '/' in f else '.' for f in files}
        if len(dirs) <= 1:
            continue
            
        total_chunks = 0
        all_intents = []
        files_context_lines = []
        for f in files:
            meta = file_meta.get(f)
            if meta:
                total_chunks += meta['chunk_count']
                all_intents.extend(meta['intents'])
                files_context_lines.append(f"- {f}: {meta['role']} - {meta['summary']}")
                
        intent_counts = {}
        for intent in all_intents:
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
        dominant_intent = max(intent_counts.items(), key=lambda x: x[1])[0] if intent_counts else 'unknown'
        
        files_context = "\n".join(files_context_lines[:20])
        prompt = f"""Analyze these files which form a cross-directory logical module.
The module name should reflect the specific architectural purpose or intent of this group of files, NOT the name of the repository itself. Use a kebab-case slug.
For example, instead of 'my-project-name', use 'auth-service', 'data-pipeline', 'ui-components', etc.

Files:
{files_context}

Respond with ONLY this JSON object:
{{
  "module_name": "<short descriptive slug>",
  "summary": "<1-2 sentences on what this logical module does>",
  "role": "<architectural role>",
  "dominant_intent": "<a full sentence describing the primary intent or purpose of this module>"
}}"""

        try:
            res = classifier._parse_json(classifier._generate(prompt, max_tokens=200))
            module_name = res.get("module_name", f"logical-{i}")
            summary = res.get("summary", "")
            role = res.get("role", "unknown")
            dominant_intent = res.get("dominant_intent", dominant_intent)
        except Exception:
            module_name = f"logical-{i}"
            summary = "Logical module"
            role = "module"
            
        module_path = f"_logical/{module_name}"
        
        cur.execute("""
            INSERT INTO module_intents (repo, module_path, kind, module_name, summary, role, dominant_intent, file_count, chunk_count, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
            repo, module_path, 'logical', module_name, summary, role, dominant_intent, len(files), total_chunks
        ))
    conn.commit()

@click.command()
@click.option("--repo", required=True, help="Repository name")
@click.option("--mode", type=click.Choice(['directory', 'logical', 'all']), default='all', help="Synthesis mode")
@click.option("--min-files", default=3, help="Minimum files per module")
@click.option("--config", default="codebrain.toml", help="Config file path")
def main(repo: str, mode: str, min_files: int, config: str):
    cfg = load_config(config)
    conn = get_db(cfg)
    classifier = IntentClassifier(cfg)
    
    console.print(f"Synthesizing modules for [bold]{repo}[/] (mode: {mode})")
    
    if mode in ('directory', 'all'):
        synthesize_directory_modules(conn, repo, min_files, classifier)
        
    if mode in ('logical', 'all'):
        synthesize_logical_modules(conn, repo, min_files, classifier)
        
    console.print("[bold green]Synthesis complete![/]")

if __name__ == '__main__':
    main()
