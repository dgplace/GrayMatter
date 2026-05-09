-- CodeBrain Schema
-- PostgreSQL + pgvector for local codebase intelligence

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Files: one row per source file
-- ============================================================
CREATE TABLE files (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,                   -- repository name / root path
    path            TEXT NOT NULL,                   -- relative file path
    language        TEXT,                            -- detected language
    size_bytes      INTEGER,
    line_count      INTEGER,
    hash            TEXT NOT NULL,                   -- SHA256 of file content (for change detection)
    summary         TEXT,                            -- LLM-generated plain-English summary
    role            TEXT,                            -- architectural role classification
    embedding       vector(768),                     -- file-level semantic embedding
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, path)
);

CREATE INDEX idx_files_repo ON files(repo);
CREATE INDEX idx_files_repo_path ON files(repo, path);
CREATE INDEX idx_files_language ON files(language);
CREATE INDEX idx_files_role ON files(role);
CREATE INDEX idx_files_embedding ON files USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ============================================================
-- Code Chunks: AST-aware pieces of files
-- ============================================================
CREATE TABLE code_chunks (
    id              SERIAL PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,                -- order within file
    content         TEXT NOT NULL,                    -- raw source code
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    symbol_name     TEXT,                             -- function/class name if this chunk IS a symbol
    symbol_type     TEXT,                             -- function, class, interface, type, method, etc.
    parent_symbol   TEXT,                             -- enclosing class/module if this is a method
    intent          TEXT,                             -- classified intent category
    intent_detail   TEXT,                             -- plain-English description of what this code does
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(file_id, chunk_index)
);

CREATE INDEX idx_chunks_file ON code_chunks(file_id);
CREATE INDEX idx_chunks_symbol ON code_chunks(symbol_name) WHERE symbol_name IS NOT NULL;
CREATE INDEX idx_chunks_symbol_type ON code_chunks(symbol_type) WHERE symbol_type IS NOT NULL;
CREATE INDEX idx_chunks_intent ON code_chunks(intent);
CREATE INDEX idx_chunks_embedding ON code_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Symbols: extracted functions, classes, types, etc.
-- ============================================================
CREATE TABLE symbols (
    id              SERIAL PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_id        INTEGER REFERENCES code_chunks(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT,                             -- module.ClassName.method_name
    kind            TEXT NOT NULL,                    -- function, class, interface, type, variable, constant
    signature       TEXT,                             -- full signature with types
    docstring       TEXT,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    parent_id       INTEGER REFERENCES symbols(id),  -- for methods inside classes
    container_symbol TEXT,                            -- enclosing type for methods / extension members
    visibility      TEXT,                             -- public, private, protected, internal
    is_exported     BOOLEAN DEFAULT FALSE,
    declared_in_extension BOOLEAN NOT NULL DEFAULT FALSE,
    is_primary_declaration BOOLEAN NOT NULL DEFAULT TRUE,
    embedding       vector(768),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_kind ON symbols(kind);
CREATE INDEX idx_symbols_qualified ON symbols(qualified_name) WHERE qualified_name IS NOT NULL;
CREATE INDEX idx_symbols_container ON symbols(container_symbol) WHERE container_symbol IS NOT NULL;
CREATE INDEX idx_symbols_primary ON symbols(is_primary_declaration);
CREATE INDEX idx_symbols_embedding ON symbols USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ============================================================
-- Symbol references: lexical/call references extracted from chunks
-- ============================================================
CREATE TABLE symbol_references (
    id              SERIAL PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source_chunk_id INTEGER REFERENCES code_chunks(id) ON DELETE CASCADE,
    source_symbol_name TEXT,
    target_name     TEXT NOT NULL,
    target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    resolution_confidence REAL,
    resolution_method TEXT,
    reference_kind  TEXT NOT NULL,                    -- call, member_call, type_reference
    reference_kind_v2 TEXT,                           -- richer resolver-aware reference kind
    line_no         INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_symbol_refs_source_file ON symbol_references(source_file_id);
CREATE INDEX idx_symbol_refs_source_chunk ON symbol_references(source_chunk_id);
CREATE INDEX idx_symbol_refs_target_name ON symbol_references(target_name);
CREATE INDEX idx_symbol_refs_kind ON symbol_references(reference_kind);
CREATE INDEX idx_symbol_refs_target_symbol ON symbol_references(target_symbol_id) WHERE target_symbol_id IS NOT NULL;
CREATE INDEX idx_symbol_refs_reverse_lookup ON symbol_references(target_symbol_id, source_file_id, source_symbol_name)
    WHERE target_symbol_id IS NOT NULL;
CREATE INDEX idx_symbol_refs_target_name_kind ON symbol_references(target_name, reference_kind);
CREATE INDEX idx_symbols_file_primary_name ON symbols(file_id, is_primary_declaration, name);

-- ============================================================
-- Symbol relationships: structural edges between declarations
-- ============================================================
CREATE TABLE symbol_relationships (
    id              SERIAL PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    relationship_kind TEXT NOT NULL,                 -- extends, implements, mixin, type_alias, returns, param_type, field_type
    target_name     TEXT NOT NULL,                   -- fallback name when target symbol is unresolved/external
    external_module TEXT,                            -- optional module/namespace for unresolved external targets
    line_no         INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_symbol_rels_source_file ON symbol_relationships(source_file_id);
CREATE INDEX idx_symbol_rels_source_symbol ON symbol_relationships(source_symbol_id);
CREATE INDEX idx_symbol_rels_target_symbol ON symbol_relationships(target_symbol_id) WHERE target_symbol_id IS NOT NULL;
CREATE INDEX idx_symbol_rels_reverse_lookup ON symbol_relationships(target_symbol_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL;
CREATE INDEX idx_symbol_rels_kind ON symbol_relationships(relationship_kind);
CREATE INDEX idx_symbol_rels_target_name ON symbol_relationships(target_name);

-- ============================================================
-- Dependencies: directed graph of imports and calls
-- ============================================================
CREATE TABLE dependencies (
    id              SERIAL PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target_file_id  INTEGER REFERENCES files(id) ON DELETE CASCADE,  -- NULL for external deps
    source_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    imported_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    imported_name   TEXT,
    local_alias     TEXT,
    is_external     BOOLEAN,
    kind            TEXT NOT NULL,                    -- import, call, type_reference, inheritance
    external_module TEXT,                             -- for unresolved / third-party imports
    external_version TEXT,                            -- optional version from language manifests
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deps_source_file ON dependencies(source_file_id);
CREATE INDEX idx_deps_target_file ON dependencies(target_file_id);
CREATE INDEX idx_deps_source_symbol ON dependencies(source_symbol_id);
CREATE INDEX idx_deps_target_symbol ON dependencies(target_symbol_id);
CREATE INDEX idx_deps_reverse_lookup ON dependencies(target_symbol_id, source_file_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL;
CREATE INDEX idx_deps_imported_symbol ON dependencies(imported_symbol_id);
CREATE INDEX idx_deps_kind ON dependencies(kind);
CREATE INDEX idx_deps_source_target ON dependencies(source_file_id, target_file_id);

-- ============================================================
-- Dependency cycles: SCC materialization per repository
-- ============================================================
CREATE TABLE dependency_cycles (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    cycle_hash      TEXT NOT NULL,
    member_file_ids INTEGER[] NOT NULL,
    member_paths    TEXT[] NOT NULL,
    cycle_size      INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, cycle_hash)
);

CREATE INDEX idx_dependency_cycles_repo ON dependency_cycles(repo);

-- ============================================================
-- Clusters: semantic groupings of symbols/files
-- ============================================================
CREATE TABLE clusters (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    cluster_key     TEXT NOT NULL,
    name            TEXT NOT NULL,
    summary         TEXT,
    modularity      REAL NOT NULL DEFAULT 0,
    embedding       vector(768),
    granularity     TEXT NOT NULL CHECK (granularity IN ('symbol', 'file')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, cluster_key)
);

CREATE INDEX idx_clusters_repo ON clusters(repo);
CREATE INDEX idx_clusters_granularity ON clusters(repo, granularity);
CREATE INDEX idx_clusters_embedding ON clusters USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Cluster members: symbol/file membership by cluster granularity
-- ============================================================
CREATE TABLE cluster_members (
    id              SERIAL PRIMARY KEY,
    cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    symbol_id       INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    file_id         INTEGER REFERENCES files(id) ON DELETE CASCADE,
    membership_weight REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (symbol_id IS NOT NULL AND file_id IS NULL)
        OR (symbol_id IS NULL AND file_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_cluster_members_symbol_unique
    ON cluster_members(cluster_id, symbol_id)
    WHERE symbol_id IS NOT NULL;
CREATE UNIQUE INDEX idx_cluster_members_file_unique
    ON cluster_members(cluster_id, file_id)
    WHERE file_id IS NOT NULL;
CREATE INDEX idx_cluster_members_symbol ON cluster_members(symbol_id) WHERE symbol_id IS NOT NULL;
CREATE INDEX idx_cluster_members_file ON cluster_members(file_id) WHERE file_id IS NOT NULL;

-- ============================================================
-- Execution flows: call-graph/intent-derived symbol memberships
-- ============================================================
CREATE TABLE flows (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    flow_key        TEXT NOT NULL,
    name            TEXT NOT NULL,
    summary         TEXT,
    dominant_intent TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(repo, flow_key)
);

CREATE INDEX idx_flows_repo ON flows(repo);
CREATE INDEX idx_flows_dominant_intent ON flows(repo, dominant_intent);

CREATE TABLE flow_members (
    id              SERIAL PRIMARY KEY,
    flow_id         INTEGER NOT NULL REFERENCES flows(id) ON DELETE CASCADE,
    symbol_id       INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    role            TEXT,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_flow_members_symbol_unique ON flow_members(flow_id, symbol_id);
CREATE INDEX idx_flow_members_symbol ON flow_members(symbol_id);

-- ============================================================
-- Documentation links: prose mapped to files/symbols/clusters
-- ============================================================
CREATE TABLE doc_links (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    source_file_id  INTEGER REFERENCES files(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    source_path     TEXT,
    target_kind     TEXT NOT NULL CHECK (target_kind IN ('file', 'symbol', 'cluster')),
    target_id       INTEGER NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_doc_links_repo_source ON doc_links(repo, source);
CREATE INDEX idx_doc_links_source_file ON doc_links(source_file_id) WHERE source_file_id IS NOT NULL;
CREATE INDEX idx_doc_links_target ON doc_links(target_kind, target_id);
CREATE INDEX idx_doc_links_embedding ON doc_links USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Ingestion runs: track what was indexed when
-- ============================================================
CREATE TABLE ingestion_runs (
    id              SERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    files_processed INTEGER DEFAULT 0,
    chunks_created  INTEGER DEFAULT 0,
    symbols_found   INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'            -- running, completed, failed
);

-- ============================================================
-- Search function: semantic similarity with optional filters
-- ============================================================
CREATE OR REPLACE FUNCTION search_code(
    query_embedding vector(768),
    match_count     INTEGER DEFAULT 20,
    filter_intent   TEXT DEFAULT NULL,
    filter_language TEXT DEFAULT NULL,
    filter_path     TEXT DEFAULT NULL,
    filter_symbol   TEXT DEFAULT NULL,
    similarity_threshold FLOAT DEFAULT 0.3,
    filter_repo     TEXT DEFAULT NULL
)
RETURNS TABLE (
    chunk_id        INTEGER,
    file_path       TEXT,
    language        TEXT,
    content         TEXT,
    symbol_name     TEXT,
    symbol_type     TEXT,
    intent          TEXT,
    intent_detail   TEXT,
    start_line      INTEGER,
    end_line        INTEGER,
    similarity      FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        cc.id AS chunk_id,
        f.path AS file_path,
        f.language,
        cc.content,
        cc.symbol_name,
        cc.symbol_type,
        cc.intent,
        cc.intent_detail,
        cc.start_line,
        cc.end_line,
        1 - (cc.embedding <=> query_embedding) AS similarity
    FROM code_chunks cc
    JOIN files f ON cc.file_id = f.id
    WHERE 1 - (cc.embedding <=> query_embedding) > similarity_threshold
      AND (filter_intent IS NULL OR cc.intent = filter_intent)
      AND (filter_language IS NULL OR f.language = filter_language)
      AND (filter_path IS NULL OR f.path LIKE filter_path || '%')
      AND (filter_symbol IS NULL OR cc.symbol_type = filter_symbol)
      AND (filter_repo IS NULL OR f.repo = filter_repo)
    ORDER BY cc.embedding <=> query_embedding
    LIMIT match_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Symbol search function
-- ============================================================
CREATE OR REPLACE FUNCTION find_symbol(
    search_name     TEXT,
    search_kind     TEXT DEFAULT NULL,
    search_file     TEXT DEFAULT NULL
)
RETURNS TABLE (
    symbol_id       INTEGER,
    name            TEXT,
    qualified_name  TEXT,
    kind            TEXT,
    signature       TEXT,
    docstring       TEXT,
    file_path       TEXT,
    start_line      INTEGER,
    end_line        INTEGER,
    is_exported     BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.id AS symbol_id,
        s.name,
        s.qualified_name,
        s.kind,
        s.signature,
        s.docstring,
        f.path AS file_path,
        s.start_line,
        s.end_line,
        s.is_exported
    FROM symbols s
    JOIN files f ON s.file_id = f.id
    WHERE s.name ILIKE '%' || search_name || '%'
      AND (search_kind IS NULL OR s.kind = search_kind)
      AND (search_file IS NULL OR f.path LIKE '%' || search_file || '%')
    ORDER BY
        CASE WHEN s.name = search_name THEN 0
             WHEN s.name ILIKE search_name THEN 1
             ELSE 2 END,
        s.is_exported DESC,
        f.path;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Dependency trace function
-- ============================================================
CREATE OR REPLACE FUNCTION trace_dependencies(
    target_path     TEXT,
    direction       TEXT DEFAULT 'both',   -- 'inbound', 'outbound', 'both'
    max_depth       INTEGER DEFAULT 3
)
RETURNS TABLE (
    source_path     TEXT,
    target_path_out TEXT,
    dep_kind        TEXT,
    source_symbol   TEXT,
    target_symbol   TEXT,
    external_module TEXT,
    depth           INTEGER
) AS $$
WITH RECURSIVE dep_tree AS (
    -- Base case: direct dependencies
    SELECT
        sf.path AS source_path,
        COALESCE(tf.path, d.external_module) AS target_path_out,
        d.kind AS dep_kind,
        ss.name AS source_symbol,
        ts.name AS target_symbol,
        d.external_module,
        1 AS depth
    FROM dependencies d
    JOIN files sf ON d.source_file_id = sf.id
    LEFT JOIN files tf ON d.target_file_id = tf.id
    LEFT JOIN symbols ss ON d.source_symbol_id = ss.id
    LEFT JOIN symbols ts ON d.target_symbol_id = ts.id
    WHERE (direction IN ('outbound', 'both') AND sf.path LIKE '%' || target_path || '%')
       OR (direction IN ('inbound', 'both') AND tf.path LIKE '%' || target_path || '%')

    UNION ALL

    -- Recursive: follow the chain
    SELECT
        sf.path,
        COALESCE(tf.path, d.external_module),
        d.kind,
        ss.name,
        ts.name,
        d.external_module,
        dt.depth + 1
    FROM dep_tree dt
    JOIN files sf2 ON (
        CASE WHEN direction IN ('outbound', 'both')
             THEN sf2.path = dt.target_path_out
             ELSE sf2.path = dt.source_path END
    )
    JOIN dependencies d ON d.source_file_id = sf2.id
    JOIN files sf ON d.source_file_id = sf.id
    LEFT JOIN files tf ON d.target_file_id = tf.id
    LEFT JOIN symbols ss ON d.source_symbol_id = ss.id
    LEFT JOIN symbols ts ON d.target_symbol_id = ts.id
    WHERE dt.depth < max_depth
)
SELECT DISTINCT * FROM dep_tree ORDER BY depth, source_path;
$$ LANGUAGE sql;

-- ============================================================
-- Reverse impact traversal function
-- ============================================================
CREATE OR REPLACE FUNCTION impact_of(
    input_symbol_id  INTEGER,
    max_depth        INTEGER,
    min_confidence   REAL DEFAULT 0.55
)
RETURNS TABLE (
    affected_symbol_id      INTEGER,
    affected_file_id        INTEGER,
    affected_file_path      TEXT,
    affected_symbol_name    TEXT,
    depth                   INTEGER,
    edge_kind               TEXT,
    path_min_confidence     REAL
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE reverse_edges AS (
        SELECT
            sr.target_symbol_id,
            sr.source_symbol_id,
            sr.relationship_kind AS edge_kind,
            1.0::REAL AS edge_confidence
        FROM symbol_relationships sr
        WHERE sr.target_symbol_id IS NOT NULL

        UNION ALL

        SELECT
            refs.target_symbol_id,
            source_symbols.source_symbol_id,
            COALESCE(refs.reference_kind_v2, refs.reference_kind) AS edge_kind,
            COALESCE(refs.resolution_confidence, 0.55)::REAL AS edge_confidence
        FROM symbol_references refs
        JOIN LATERAL (
            SELECT s.id AS source_symbol_id
            FROM symbols s
            WHERE s.file_id = refs.source_file_id
              AND refs.source_symbol_name IS NOT NULL
              AND lower(s.name) = lower(refs.source_symbol_name)
            ORDER BY
                CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
                s.start_line
            LIMIT 1
        ) source_symbols ON TRUE
        WHERE refs.target_symbol_id IS NOT NULL

        UNION ALL

        SELECT
            d.target_symbol_id,
            d.source_symbol_id,
            d.kind AS edge_kind,
            1.0::REAL AS edge_confidence
        FROM dependencies d
        WHERE d.target_symbol_id IS NOT NULL
          AND d.source_symbol_id IS NOT NULL
    ),
    walk AS (
        SELECT
            input_symbol_id AS symbol_id,
            0 AS depth,
            NULL::TEXT AS edge_kind,
            1.0::REAL AS path_min_confidence,
            ARRAY[input_symbol_id]::INTEGER[] AS visited

        UNION ALL

        SELECT
            re.source_symbol_id AS symbol_id,
            walk.depth + 1 AS depth,
            re.edge_kind,
            LEAST(walk.path_min_confidence, re.edge_confidence)::REAL AS path_min_confidence,
            walk.visited || re.source_symbol_id AS visited
        FROM walk
        JOIN reverse_edges re
          ON re.target_symbol_id = walk.symbol_id
        WHERE walk.depth < max_depth
          AND re.source_symbol_id IS NOT NULL
          AND re.edge_confidence >= min_confidence
          AND NOT re.source_symbol_id = ANY(walk.visited)
    )
    SELECT
        s.id AS affected_symbol_id,
        f.id AS affected_file_id,
        f.path AS affected_file_path,
        s.name AS affected_symbol_name,
        walk.depth,
        walk.edge_kind,
        walk.path_min_confidence
    FROM walk
    JOIN symbols s ON s.id = walk.symbol_id
    JOIN files f ON f.id = s.file_id
    WHERE walk.depth > 0
    ORDER BY walk.depth, walk.path_min_confidence DESC, f.path, s.name;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Module Intents
-- ============================================================
CREATE TABLE IF NOT EXISTS module_intents (
  repo            TEXT NOT NULL,
  module_path     TEXT NOT NULL, -- directory path OR "_logical/<slug>"
  kind            TEXT NOT NULL DEFAULT 'directory', -- 'directory' | 'logical'
  module_name     TEXT,
  summary         TEXT,
  role            TEXT,
  dominant_intent TEXT,
  file_count      INTEGER NOT NULL DEFAULT 0,
  chunk_count     INTEGER NOT NULL DEFAULT 0,
  member_symbols  TEXT[],                            -- class/type names in this module
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (repo, module_path)
);
CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo);
CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind);

-- ============================================================
-- Codebase stats view
-- ============================================================
CREATE OR REPLACE VIEW codebase_stats AS
SELECT
    f.repo,
    COUNT(DISTINCT f.id) AS total_files,
    SUM(f.line_count) AS total_lines,
    COUNT(DISTINCT cc.id) AS total_chunks,
    COUNT(DISTINCT s.id) AS total_symbols,
    COUNT(DISTINCT d.id) AS total_dependencies,
    jsonb_object_agg(DISTINCT f.language, lang_counts.cnt) FILTER (WHERE f.language IS NOT NULL) AS languages,
    jsonb_object_agg(DISTINCT cc.intent, intent_counts.cnt) FILTER (WHERE cc.intent IS NOT NULL) AS intents,
    jsonb_object_agg(DISTINCT s.kind, kind_counts.cnt) FILTER (WHERE s.kind IS NOT NULL) AS symbol_kinds
FROM files f
LEFT JOIN code_chunks cc ON cc.file_id = f.id
LEFT JOIN symbols s ON s.file_id = f.id
LEFT JOIN dependencies d ON d.source_file_id = f.id
LEFT JOIN LATERAL (
    SELECT f2.language, COUNT(*) cnt FROM files f2 WHERE f2.repo = f.repo GROUP BY f2.language
) lang_counts ON lang_counts.language = f.language
LEFT JOIN LATERAL (
    SELECT cc2.intent, COUNT(*) cnt FROM code_chunks cc2 JOIN files f3 ON cc2.file_id = f3.id WHERE f3.repo = f.repo GROUP BY cc2.intent
) intent_counts ON intent_counts.intent = cc.intent
LEFT JOIN LATERAL (
    SELECT s2.kind, COUNT(*) cnt FROM symbols s2 JOIN files f4 ON s2.file_id = f4.id WHERE f4.repo = f.repo GROUP BY s2.kind
) kind_counts ON kind_counts.kind = s.kind
GROUP BY f.repo;
