# Knowledge Graph — Implementation Plan

## Goal

Extend CodeBrain from a "search + symbol table + flat dependency edges" system into a true code knowledge graph that can answer structural, behavioral, and impact questions:

1. **Structural resolution** — `is-a`, `has-a`, type resolution
2. **Execution tracing** — call graphs, instantiation, async/event handlers
3. **Dependency mapping** — imports/exports with alias resolution, third-party vs internal, cycles
4. **Blast-radius / impact analysis** — reverse traversal across all edge kinds
5. **Semantic grouping** — logical modules / domains, doc-linked nodes

## Current state

CodeBrain already has the *bones* of a graph in PostgreSQL:

- `files`, `symbols`, `symbol_references`, `dependencies` tables
- `dependencies.kind` already accepts `import | call | type_reference | inheritance`
- tree-sitter chunking and per-symbol embeddings
- `find_references` and `trace_dependencies` MCP tools

What is missing:

| Concern | Gap |
|---|---|
| Structural | No persisted inheritance/implements edges; no type resolution from signatures back to declaring `symbols.id` |
| Execution | `symbol_references` records textual `target_name`, not a resolved `target_symbol_id`; no instantiation kind; no event/callback edges |
| Dependency | Import edges resolve to files but not to specific exported symbols; no alias resolution; no cycle detection materialization; weak internal-vs-external classification |
| Blast radius | No backward query helpers; no transitive-closure indices |
| Semantic grouping | No "module / domain" cluster nodes; docstrings live on `symbols` but are not linked to the cluster they describe |

The realistic shape of this work is **resolution + new edge kinds + cluster nodes**, not "throw out and rebuild."

## Technology choice

The core decision is: **stay on PostgreSQL, or introduce a graph DB?**

Recommendation: **stay on PostgreSQL, add the [Apache AGE](https://age.apache.org/) extension as an optional view, and adopt [SCIP](https://github.com/sourcegraph/scip) as the cross-language resolution data model.**

Rationale:

- The existing schema is already a directed graph in relational form. Recursive CTEs handle most queries we care about (call graphs to depth N, reverse impact, cycle detection). Benchmarks show Apache AGE is slower than Neo4j on deep variable-length traversals but acceptable for fixed-depth iterative queries — and it lives in the same Postgres process, so backups, ops, and the existing `pg_basebackup`/Docker topology are unchanged ([Apache AGE vs Neo4j — DEV](https://dev.to/pawnsapprentice/apache-age-vs-neo4j-battle-of-the-graph-databases-2m4)).
- Introducing Neo4j would mean a second persistence layer, dual-write consistency problems, and a second backup story for a local-first product. Not worth it for our scale.
- For *resolution* (mapping a textual call site to the symbol it actually binds to), the heavyweight, well-tested options are:
  - **[SCIP](https://sourcegraph.com/blog/announcing-scip)** — Sourcegraph's successor to LSIF. Protobuf format, stable indexers for TypeScript, Python, Java, Kotlin, Scala, C/C++, Ruby, Go, Dart, PHP, .NET. Designed to be incremental and producer-friendly.
  - **[stack-graphs](https://github.blog/open-source/introducing-stack-graphs/)** — GitHub's file-incremental name resolution built on tree-sitter. Strong fit because we already use tree-sitter, but per-language rule sets are nontrivial.
  - **Custom resolution per language** (today's approach, partial).

  Plan: use **SCIP indexers when available** (they exist for almost every language CodeBrain currently parses) and fall back to tree-sitter+heuristics for the rest. SCIP gives us resolved `definition` ↔ `reference` ranges out of the box, which is exactly what `symbol_references.target_symbol_id` needs.
- Community detection for semantic grouping: **Leiden over Louvain**. Leiden guarantees connected communities and is faster ([Traag et al., 2019](https://www.nature.com/articles/s41598-019-41695-z)). Easiest path is `python-igraph` or `networkx`'s Leiden binding, run as a post-ingestion pass.

## Schema additions

All additive — no destructive migrations.

```sql
-- 1. Structural: inheritance / implements / mixin
CREATE TABLE symbol_relationships (
    id              SERIAL PRIMARY KEY,
    source_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    target_name     TEXT NOT NULL,         -- denormalized for unresolved external types
    kind            TEXT NOT NULL,         -- extends | implements | mixin | type_alias | returns | param_type | field_type
    external_module TEXT,                  -- non-NULL when target is in a third-party package
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_symrel_src ON symbol_relationships(source_symbol_id);
CREATE INDEX idx_symrel_tgt ON symbol_relationships(target_symbol_id);
CREATE INDEX idx_symrel_kind ON symbol_relationships(kind);

-- 2. Execution: resolve symbol_references and add instantiation/callback kinds
ALTER TABLE symbol_references
    ADD COLUMN target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    ADD COLUMN resolution_confidence REAL,    -- 1.0 = SCIP-resolved, <1.0 = heuristic
    ADD COLUMN reference_kind_v2 TEXT;        -- call | member_call | instantiation | callback_register | event_emit | type_reference
CREATE INDEX idx_symrefs_target_symbol ON symbol_references(target_symbol_id);

-- 3. Dependency: resolve imports to exported symbols and track aliases
ALTER TABLE dependencies
    ADD COLUMN imported_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    ADD COLUMN imported_name TEXT,            -- original name in the source module
    ADD COLUMN local_alias TEXT,              -- bound name in importing file
    ADD COLUMN is_external BOOLEAN NOT NULL DEFAULT FALSE;

-- 4. Cycles: materialized cycle membership (refreshed per-ingest)
CREATE TABLE dependency_cycles (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    cycle_hash      TEXT NOT NULL,            -- stable id derived from sorted file_ids
    file_ids        INTEGER[] NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, cycle_hash)
);

-- 5. Semantic grouping: clusters and membership
CREATE TABLE clusters (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    name            TEXT NOT NULL,            -- LLM-named, e.g. "Authentication Domain"
    summary         TEXT,                     -- LLM summary
    algorithm       TEXT NOT NULL,            -- 'leiden' | 'manual' | 'directory'
    modularity      REAL,
    embedding       vector(768),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, name)
);
CREATE TABLE cluster_members (
    cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    weight          REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (cluster_id, file_id)
);

-- 6. Doc linking: associate prose to nodes (file/symbol/cluster)
CREATE TABLE doc_links (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    target_kind     TEXT NOT NULL,            -- 'file' | 'symbol' | 'cluster'
    target_id       INTEGER NOT NULL,
    source          TEXT NOT NULL,            -- 'docstring' | 'readme' | 'inline_comment' | 'llm_summary'
    source_path     TEXT,
    content         TEXT NOT NULL,
    embedding       vector(768),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_doc_links_target ON doc_links(target_kind, target_id);
```

**Optional AGE projection.** Once edges are resolved, add `CREATE EXTENSION age;` and a one-time `SELECT * FROM cypher(...)` materialization that mirrors `symbols`/`symbol_relationships`/`symbol_references`/`dependencies` into a property graph for users who want Cypher. The relational tables remain authoritative.

## Pipeline changes

### Stage A — Resolution layer (`resolver.py`, new)

A post-parse, pre-persist stage that takes the per-file tree-sitter results and produces *resolved* edges.

- For languages with a SCIP indexer, run the indexer over the repo (or per-file when changed) and join SCIP `Occurrence` ranges back to our `symbols.start_line/end_line`. Replace heuristic `target_name` with `target_symbol_id` where SCIP gives a definitive resolution.
- For unsupported languages, keep the current heuristic path but mark `resolution_confidence < 1.0` so consumers can downweight it.

This is the single most important change — every other concern depends on having edges that point at *symbols* rather than at *strings*.

### Stage B — Structural extraction (extend `chunker.py`/`classifier.py`)

Per language, surface from the AST:

- `extends` / `implements` / `mixin` clauses on class declarations → `symbol_relationships`
- type annotations on returns, parameters, fields → `symbol_relationships(kind in returns/param_type/field_type)`
- type aliases / generics → `symbol_relationships(kind=type_alias)`

Tree-sitter queries are sufficient for this; no SCIP required for the source side. The *target* is resolved via Stage A.

### Stage C — Execution tracing

- `instantiation`: tree-sitter pattern for `new X()` (JS/TS/Java), `X(...)` calls where `X` resolves to a class symbol (Python).
- `callback_register`: detect common patterns — `emitter.on('evt', fn)`, `addEventListener`, decorator-based handlers, framework conventions (Express `app.get`, FastAPI `@app.route`). Each pattern is a separate, opt-in extractor; **callbacks are explicitly a research item** (see below).

### Stage D — Dependency resolution

- Parse import statements with tree-sitter (already done) and additionally record `imported_name` (original) and `local_alias` (bound name).
- Decide internal vs external by checking whether the import target resolves to a `files.id` in the same repo. Mark `is_external = TRUE` and store `external_module` otherwise.
- For npm/pip/maven dependencies, optionally read `package.json` / `requirements.txt` / `pyproject.toml` to pin a version on `external_module`.

### Stage E — Cycle detection (post-ingestion)

After all `dependencies` rows are written for a repo, run Tarjan's SCC algorithm on the file-level graph (recursive CTE or in-process via `networkx`). For each SCC of size > 1, write a row to `dependency_cycles`. Cheap; runs in seconds even on large repos.

### Stage F — Semantic grouping (post-ingestion)

1. Build a weighted graph in memory (`networkx`/`igraph`):
   - nodes = files (or symbols, configurable)
   - edge weight = count of dependencies + co-changes (if git history available) + shared-cluster heuristics
2. Run **Leiden** to produce a partition. Persist communities as `clusters` rows with `algorithm='leiden'`.
3. For each cluster, run a single LLM call summarizing the member files (using existing `classifier.py` infra) → `clusters.name`, `clusters.summary`, embedding.
4. Existing docstrings/READMEs are written to `doc_links` and embedded; the cluster's `embedding` is computed from the concatenated/averaged member docs.

### Stage G — Blast-radius indices

No new tables; just SQL helpers and one MCP tool. Recursive CTEs walk:

- `symbol_relationships` reversed (who implements / extends / uses this type)
- `symbol_references` reversed (who calls this symbol)
- `dependencies` reversed (who imports this file/symbol)

Add a `LATERAL`-friendly SQL function `impact_of(symbol_id, max_depth)` that returns the union with depth annotations, so the MCP tool is a thin wrapper.

## MCP tool additions

| Tool | Purpose |
|---|---|
| `find_supertypes(symbol)` / `find_subtypes(symbol)` | Walk `symbol_relationships(kind in extends|implements)` |
| `find_implementations(interface)` | Specialized subtype walk filtered to `kind=implements` |
| `call_graph(symbol, direction, depth)` | Forward = callees; reverse = callers; uses resolved `target_symbol_id` |
| `find_instantiations(class)` | Filter `symbol_references` where `reference_kind_v2='instantiation'` |
| `cycles(repo)` | Read `dependency_cycles` |
| `impact_of(symbol, depth)` | Reverse-walk all edge kinds; returns affected files, symbols, clusters |
| `clusters(repo)` / `cluster_members(cluster)` | Browse semantic groupings |
| `describe_node(kind, id)` | Returns symbol/file/cluster + all linked `doc_links` |

These slot into `src/mcp/` next to the existing `find_references`, `trace_dependencies`. The web UI gets a new `/ui/graph` view that overlays clusters on the existing semantic graph.

## Phased delivery

Each phase is independently shippable.

1. **Phase 1 — Resolution backbone.** Add `target_symbol_id`/`resolution_confidence` to `symbol_references`; integrate one SCIP indexer (TypeScript first, since the MCP server itself is TS and gives us a real test bed); fall back to existing heuristic. *Outcome:* `find_references` becomes precise. No new tools.
2. **Phase 2 — Structural edges.** Add `symbol_relationships`; tree-sitter queries for `extends`/`implements`/type annotations; ship `find_supertypes`, `find_subtypes`, `find_implementations`. *Outcome:* answers "is-a" questions.
3. **Phase 3 — Execution tracing.** `reference_kind_v2`, instantiation extractor, `call_graph` and `find_instantiations` tools. *Outcome:* answers "calls" and "creates" questions.
4. **Phase 4 — Dependency precision + cycles.** Resolved imports with aliases; `dependency_cycles` table; `cycles` tool. *Outcome:* answers "needs" questions and surfaces architectural smells.
5. **Phase 5 — Impact / blast radius.** `impact_of` SQL function and tool, plus reverse-traversal indexes. *Outcome:* "what breaks if I rename X?"
6. **Phase 6 — Semantic grouping.** Leiden clustering, `clusters` + `doc_links` tables, `describe_node`/`clusters` tools, `/ui/graph` view. *Outcome:* domain-level navigation.
7. **Phase 7 (optional) — AGE projection.** Materialize the graph for Cypher consumers. Only do this if there's pull from users.

Phases 1–5 are mostly mechanical. Phase 6 is where the open questions cluster.

## Items needing further research

These are the unknowns I would not commit to without a spike:

1. **Callback / event handler resolution.** The user explicitly called this out as "more advanced," and it is. Patterns are framework-specific (Express, FastAPI, React `useEffect` deps, EventEmitter, decorators, DOM `addEventListener`, Qt signals/slots in the desktop code). Question: do we encode each framework as a pluggable extractor, or use an LLM-assisted pass that reads the chunk and emits structured edges? Both are viable. I would prototype the LLM-assisted path on a single repo first to measure precision before committing.

2. **SCIP indexer integration cost.** SCIP is the right format but each indexer is a separate binary with its own build/run requirements (Java needs a JVM, scip-typescript needs `tsc`, scip-clang needs build commands). Question: do we ship them as Docker sidecars, expect users to install them, or fall back to tree-sitter when missing? Affects the desktop app onboarding story.

3. **Symbol granularity for clustering.** Running Leiden on the *file* graph is cheap and intuitive but may be too coarse for monorepos with mega-files. Running on the *symbol* graph is more accurate but explodes node count (100k+ symbols on a real repo). Question: pick a level, or expose both? Needs a benchmark on at least one large repo.

4. **Cluster naming quality.** LLM-named clusters are only as good as the prompt and the inputs. Question: is "first 20 file paths + their summaries" enough context, or do we need to feed file embeddings + co-change history? Needs evaluation against human-labeled clusters on a known codebase.

5. **Type resolution for dynamic languages.** Python's static analysis without type hints is famously incomplete. SCIP-python uses a real type checker (Pyright-equivalent) — good. But duck-typed code, dynamic attribute access, and `getattr` patterns will produce low-confidence edges. Question: do we surface confidence in MCP responses, or filter aggressively? Affects how trustworthy "blast radius" is.

6. **Incremental updates.** Today's ingestion is full-file re-walk. Stack-graphs are explicitly file-incremental and SCIP supports per-file updates. Question: when a file changes in watch mode, can we update only its outgoing edges and the (small) set of incoming edges that resolve to its symbols, without rebuilding the world? Critical for desktop-app responsiveness; likely a multi-week piece of work on its own.

7. **AGE vs raw SQL performance crossover.** At what graph size do recursive CTEs slow enough that AGE's Cypher compiler wins? I don't have a number for our schema. Worth a benchmark before promising it as a feature.

## Sources

- [Apache AGE vs Neo4j — DEV Community](https://dev.to/pawnsapprentice/apache-age-vs-neo4j-battle-of-the-graph-databases-2m4)
- [Apache AGE project](https://age.apache.org/)
- [SCIP — a better code indexing format than LSIF (Sourcegraph blog)](https://sourcegraph.com/blog/announcing-scip)
- [sourcegraph/scip on GitHub](https://github.com/sourcegraph/scip)
- [Introducing stack graphs (GitHub Blog)](https://github.blog/open-source/introducing-stack-graphs/)
- [Stack graphs: name resolution at scale (Creager, 2023)](https://drops.dagstuhl.de/storage/01oasics/oasics-vol109-evcs2023/OASIcs.EVCS.2023.8/OASIcs.EVCS.2023.8.pdf)
- [tree-sitter/tree-sitter-graph](https://github.com/tree-sitter/tree-sitter-graph)
- [Building Call Graphs for Code Exploration Using Tree-Sitter (DZone)](https://dzone.com/articles/call-graphs-code-exploration-tree-sitter)
- [From Louvain to Leiden: guaranteeing well-connected communities (Traag et al., 2019)](https://www.nature.com/articles/s41598-019-41695-z)
- [Louvain method (Wikipedia)](https://en.wikipedia.org/wiki/Louvain_method)
- [GraphRAG for Devs: Graph-Code Demo (Memgraph)](https://memgraph.com/blog/graphrag-for-devs-coding-assistant)
