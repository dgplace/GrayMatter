/**
 * @file src/db.ts
 * @brief PostgreSQL pool access and schema patch bootstrapping for MCP runtime.
 */

import pg from "pg";

import { DATABASE_URL } from "./config.js";

const pool = new pg.Pool({ connectionString: DATABASE_URL });

const SCHEMA_PATCHES = [
  `ALTER TABLE symbols ADD COLUMN IF NOT EXISTS container_symbol TEXT`,
  `ALTER TABLE symbols ADD COLUMN IF NOT EXISTS declared_in_extension BOOLEAN NOT NULL DEFAULT FALSE`,
  `ALTER TABLE symbols ADD COLUMN IF NOT EXISTS is_primary_declaration BOOLEAN NOT NULL DEFAULT TRUE`,
  `CREATE TABLE IF NOT EXISTS symbol_references (
      id SERIAL PRIMARY KEY,
      source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
      source_chunk_id INTEGER REFERENCES code_chunks(id) ON DELETE CASCADE,
      source_symbol_name TEXT,
      target_name TEXT NOT NULL,
      reference_kind TEXT NOT NULL,
      line_no INTEGER NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )`,
  `CREATE INDEX IF NOT EXISTS idx_symbols_container ON symbols(container_symbol)`,
  `CREATE INDEX IF NOT EXISTS idx_symbols_primary ON symbols(is_primary_declaration)`,
  `CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_file ON symbol_references(source_file_id)`,
  `CREATE INDEX IF NOT EXISTS idx_symbol_refs_source_chunk ON symbol_references(source_chunk_id)`,
  `CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name ON symbol_references(target_name)`,
  `CREATE INDEX IF NOT EXISTS idx_symbol_refs_kind ON symbol_references(reference_kind)`,
  `CREATE TABLE IF NOT EXISTS module_intents (
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
    )`,
  `CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo)`,
  `CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind)`,
  `
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
  `,
] as const;

/**
 * @brief Executes a SQL statement with optional positional parameters.
 * @param text SQL statement text.
 * @param params Positional bind values.
 * @returns Query result from node-postgres.
 */
export async function query(text: string, params?: unknown[]): Promise<pg.QueryResult> {
  const client = await pool.connect();
  try {
    return await client.query(text, params);
  } finally {
    client.release();
  }
}

/**
 * @brief Applies MCP-required schema patches for backwards-compatible startup.
 *
 * Uses a PostgreSQL advisory lock to prevent concurrent DDL from multiple
 * server instances colliding on catalog tuples.
 * @returns Promise resolved when all patches are applied.
 */
export async function ensureSchema(): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("SELECT pg_advisory_lock(42)");
    for (const statement of SCHEMA_PATCHES) {
      await client.query(statement);
    }
    await client.query("SELECT pg_advisory_unlock(42)");
  } catch (err) {
    await client.query("SELECT pg_advisory_unlock(42)").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

/**
 * @brief Closes the shared PostgreSQL pool during graceful shutdown.
 * @returns Promise resolved when all connections are closed.
 */
export async function closePool(): Promise<void> {
  await pool.end();
}
