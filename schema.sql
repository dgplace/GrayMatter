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
    reference_kind  TEXT NOT NULL,                    -- call, member_call, type_reference
    line_no         INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_symbol_refs_source_file ON symbol_references(source_file_id);
CREATE INDEX idx_symbol_refs_source_chunk ON symbol_references(source_chunk_id);
CREATE INDEX idx_symbol_refs_target_name ON symbol_references(target_name);
CREATE INDEX idx_symbol_refs_kind ON symbol_references(reference_kind);

-- ============================================================
-- Dependencies: directed graph of imports and calls
-- ============================================================
CREATE TABLE dependencies (
    id              SERIAL PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target_file_id  INTEGER REFERENCES files(id) ON DELETE CASCADE,  -- NULL for external deps
    source_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,                    -- import, call, type_reference, inheritance
    external_module TEXT,                             -- for unresolved / third-party imports
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deps_source_file ON dependencies(source_file_id);
CREATE INDEX idx_deps_target_file ON dependencies(target_file_id);
CREATE INDEX idx_deps_source_symbol ON dependencies(source_symbol_id);
CREATE INDEX idx_deps_target_symbol ON dependencies(target_symbol_id);
CREATE INDEX idx_deps_kind ON dependencies(kind);

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
    similarity_threshold FLOAT DEFAULT 0.3
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
