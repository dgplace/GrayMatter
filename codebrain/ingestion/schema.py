"""
@file schema.py
@brief Ingestion schema migrations and symbol persistence helpers.
"""

from typing import Optional

SCHEMA_PATCHES = [
    """
    UPDATE files
    SET role = COALESCE(
        NULLIF(regexp_replace(regexp_replace(lower(btrim(role)), '[-_]+', ' ', 'g'), '[[:space:]]+', ' ', 'g'), ''),
        'unknown'
    )
    WHERE role IS NOT NULL
      AND role IS DISTINCT FROM COALESCE(
          NULLIF(regexp_replace(regexp_replace(lower(btrim(role)), '[-_]+', ' ', 'g'), '[[:space:]]+', ' ', 'g'), ''),
          'unknown'
      )
    """,
    """
    UPDATE files
    SET role = CASE
        WHEN lower(path) LIKE '%/test%' OR lower(path) LIKE 'test_%' OR lower(path) LIKE '%_test.py' OR lower(path) LIKE '%.test.ts' OR lower(path) LIKE '%.spec.ts' THEN 'test suite'
        WHEN lower(path) LIKE '%.toml' OR lower(path) LIKE '%.yaml' OR lower(path) LIKE '%.yml' OR lower(path) LIKE '%.json' OR lower(path) LIKE '%.ini' OR lower(path) LIKE '%.cfg' THEN 'configuration'
        WHEN lower(path) LIKE '%migration%' THEN 'migration'
        WHEN lower(path) LIKE '%.md' OR lower(path) LIKE '%.rst' OR lower(path) LIKE '%.txt' THEN 'documentation'
        WHEN lower(path) LIKE '%controller%' OR lower(path) LIKE '%route%' OR lower(path) LIKE '%api%' THEN 'api controller'
        WHEN lower(path) LIKE '%parser%' OR lower(path) LIKE '%grammar%' THEN 'parser'
        WHEN lower(path) LIKE '%model%' OR lower(path) LIKE '%types%' OR lower(path) LIKE '%mtypes%' THEN 'data model'
        WHEN lower(path) LIKE '%service%' THEN 'service layer'
        WHEN lower(path) LIKE '%middleware%' THEN 'middleware'
        WHEN lower(path) LIKE '%cli%' OR lower(path) IN ('main.py', 'main.ts', 'main.swift', 'main.cpp') THEN 'cli entry point'
        WHEN lower(path) LIKE '%view.swift' OR lower(path) LIKE '%.tsx' OR lower(path) LIKE '%.jsx' THEN 'ui component'
        WHEN language IS NOT NULL AND btrim(language) <> '' THEN lower(btrim(language)) || ' module'
        ELSE 'source file'
    END
    WHERE role IS NULL OR btrim(role) = '' OR lower(btrim(role)) = 'unknown'
    """,
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS container_symbol TEXT
    """,
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS declared_in_extension BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    ALTER TABLE symbols
    ADD COLUMN IF NOT EXISTS is_primary_declaration BOOLEAN NOT NULL DEFAULT TRUE
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_references (
        id SERIAL PRIMARY KEY,
        source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_chunk_id INTEGER REFERENCES code_chunks(id) ON DELETE CASCADE,
        source_symbol_name TEXT,
        target_name TEXT NOT NULL,
        target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
        resolution_confidence REAL,
        resolution_method TEXT,
        reference_kind TEXT NOT NULL,
        reference_kind_v2 TEXT,
        line_no INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS resolution_confidence REAL
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS resolution_method TEXT
    """,
    """
    ALTER TABLE symbol_references
    ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT
    """,
    """
    UPDATE symbol_references
    SET reference_kind_v2 = reference_kind
    WHERE reference_kind_v2 IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_container
    ON symbols(container_symbol)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_primary
    ON symbols(is_primary_declaration)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_file
    ON symbol_references(source_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_chunk
    ON symbol_references(source_chunk_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name
    ON symbol_references(target_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_kind
    ON symbol_references(reference_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol
    ON symbol_references(target_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_reverse_lookup
    ON symbol_references(target_symbol_id, source_file_id, source_symbol_name)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind
    ON symbol_references(target_name, reference_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name
    ON symbols(file_id, is_primary_declaration, name)
    """,
    """
    CREATE TABLE IF NOT EXISTS symbol_relationships (
        id SERIAL PRIMARY KEY,
        source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
        relationship_kind TEXT NOT NULL,
        target_name TEXT NOT NULL,
        external_module TEXT,
        line_no INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS relationship_kind TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS target_name TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS external_module TEXT
    """,
    """
    ALTER TABLE symbol_relationships
    ADD COLUMN IF NOT EXISTS line_no INTEGER
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS imported_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS imported_name TEXT
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS local_alias TEXT
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS is_external BOOLEAN
    """,
    """
    ALTER TABLE dependencies
    ADD COLUMN IF NOT EXISTS external_version TEXT
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deps_target_symbol
    ON dependencies(target_symbol_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deps_reverse_lookup
    ON dependencies(target_symbol_id, source_file_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS dependency_cycles (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        cycle_hash TEXT NOT NULL,
        member_file_ids INTEGER[] NOT NULL,
        member_paths TEXT[] NOT NULL,
        cycle_size INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(repo, cycle_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dependency_cycles_repo
    ON dependency_cycles(repo)
    """,
    """
    CREATE TABLE IF NOT EXISTS clusters (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        cluster_key TEXT NOT NULL,
        name TEXT NOT NULL,
        summary TEXT,
        modularity REAL NOT NULL DEFAULT 0,
        embedding vector(768),
        granularity TEXT NOT NULL CHECK (granularity IN ('symbol', 'file')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(repo, cluster_key)
    )
    """,
    """
    ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS modularity REAL NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS embedding vector(768)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_clusters_repo
    ON clusters(repo)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_clusters_granularity
    ON clusters(repo, granularity)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_clusters_embedding
    ON clusters USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_members (
        id SERIAL PRIMARY KEY,
        cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
        symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
        file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
        membership_weight REAL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (
            (symbol_id IS NOT NULL AND file_id IS NULL)
            OR (symbol_id IS NULL AND file_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_symbol_unique
    ON cluster_members(cluster_id, symbol_id)
    WHERE symbol_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_file_unique
    ON cluster_members(cluster_id, file_id)
    WHERE file_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cluster_members_symbol
    ON cluster_members(symbol_id)
    WHERE symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cluster_members_file
    ON cluster_members(file_id)
    WHERE file_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS flows (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        flow_key TEXT NOT NULL,
        name TEXT NOT NULL,
        summary TEXT,
        dominant_intent TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(repo, flow_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_flows_repo
    ON flows(repo)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_flows_dominant_intent
    ON flows(repo, dominant_intent)
    """,
    """
    CREATE TABLE IF NOT EXISTS flow_members (
        id SERIAL PRIMARY KEY,
        flow_id INTEGER NOT NULL REFERENCES flows(id) ON DELETE CASCADE,
        symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        role TEXT,
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_members_symbol_unique
    ON flow_members(flow_id, symbol_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_flow_members_symbol
    ON flow_members(symbol_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_links (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        source_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        source_path TEXT,
        target_kind TEXT NOT NULL CHECK (target_kind IN ('file', 'symbol', 'cluster')),
        target_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        embedding vector(768) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doc_links_repo_source
    ON doc_links(repo, source)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doc_links_source_file
    ON doc_links(source_file_id)
    WHERE source_file_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doc_links_target
    ON doc_links(target_kind, target_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doc_links_embedding
    ON doc_links USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    """,
    """
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
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_file
    ON symbol_relationships(source_file_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_symbol
    ON symbol_relationships(source_symbol_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_symbol
    ON symbol_relationships(target_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_reverse_lookup
    ON symbol_relationships(target_symbol_id, source_symbol_id)
    WHERE target_symbol_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_kind
    ON symbol_relationships(relationship_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_name
    ON symbol_relationships(target_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS module_intents (
      repo            TEXT NOT NULL,
      module_path     TEXT NOT NULL,
      kind            TEXT NOT NULL DEFAULT 'directory',
      module_name     TEXT,
      summary         TEXT,
      role            TEXT,
      dominant_intent TEXT,
      file_count      INTEGER NOT NULL DEFAULT 0,
      chunk_count     INTEGER NOT NULL DEFAULT 0,
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (repo, module_path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind)
    """,
    """
    ALTER TABLE module_intents
    ADD COLUMN IF NOT EXISTS member_symbols TEXT[]
    """,
    """
    ALTER TABLE module_intents
    ADD COLUMN IF NOT EXISTS cluster_id INTEGER
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_module_intents_cluster
    ON module_intents(cluster_id)
    WHERE cluster_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_diagnostics (
        id SERIAL PRIMARY KEY,
        repo TEXT NOT NULL,
        diagnostic_kind TEXT NOT NULL,
        framework TEXT NOT NULL,
        extractor_module TEXT,
        affected_file_count INTEGER NOT NULL DEFAULT 0,
        affected_file_ids INTEGER[] NOT NULL DEFAULT '{}',
        details JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(repo, diagnostic_kind, framework)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ingestion_diagnostics_repo_kind
    ON ingestion_diagnostics(repo, diagnostic_kind)
    """,
]


def ensure_schema(conn) -> None:
    cur = conn.cursor()
    try:
        for statement in SCHEMA_PATCHES:
            cur.execute(statement)
        conn.commit()
    finally:
        cur.close()


def insert_symbol(cur, file_id: int, chunk_id: Optional[int], symbol: dict, embedding, parent_id: Optional[int] = None) -> int:
    cur.execute(
        """INSERT INTO symbols
           (file_id, chunk_id, name, qualified_name, kind, signature, docstring,
            start_line, end_line, parent_id, container_symbol, visibility, is_exported,
            declared_in_extension, is_primary_declaration, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            file_id,
            chunk_id,
            symbol["name"],
            symbol.get("qualified_name"),
            symbol.get("kind", "unknown"),
            symbol.get("signature"),
            symbol.get("docstring"),
            symbol["start_line"],
            symbol["end_line"],
            parent_id,
            symbol.get("container_symbol"),
            symbol.get("visibility", "public"),
            symbol.get("is_exported", False),
            symbol.get("declared_in_extension", False),
            symbol.get("is_primary_declaration", True),
            embedding,
        ),
    )
    return cur.fetchone()[0]
