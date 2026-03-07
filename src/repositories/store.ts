/**
 * @file src/repositories/store.ts
 * @brief Repository-scoped read models shared by MCP tools and HTTP UI endpoints.
 */

import { query } from "../db.js";

/** @brief Aggregate counts for an indexed repository. */
export type RepositorySummary = {
  repo: string;
  total_files: number;
  total_lines: number;
  total_chunks: number;
  total_symbols: number;
};

/** @brief Per-repository language distribution record. */
export type RepoLanguageStat = {
  language: string;
  count: number;
};

/** @brief Per-repository intent distribution record. */
export type RepoIntentStat = {
  intent: string;
  count: number;
};

/** @brief Per-repository symbol kind distribution record. */
export type RepoSymbolKindStat = {
  kind: string;
  count: number;
};

/** @brief Detailed repository stats payload used by MCP and UI. */
export type RepositoryStats = {
  summary: RepositorySummary;
  languages: RepoLanguageStat[];
  intents: RepoIntentStat[];
  symbolKinds: RepoSymbolKindStat[];
};

/** @brief Aggregated semantic graph edge representation. */
export type SemanticGraphEdge = {
  source: string;
  target: string;
  kind: string;
  weight: number;
};

/** @brief Semantic graph node with degree metadata. */
export type SemanticGraphNode = {
  id: string;
  degree: number;
};

/** @brief Repo graph payload returned by API endpoints. */
export type RepositoryGraph = {
  nodes: SemanticGraphNode[];
  edges: SemanticGraphEdge[];
};

/**
 * @brief Lists all indexed repositories with file/chunk/symbol totals.
 * @returns Repository summaries sorted by repo name.
 */
export async function listRepositories(): Promise<RepositorySummary[]> {
  const result = await query(`
    WITH repo_file_stats AS (
      SELECT
        f.repo,
        COUNT(*) AS total_files,
        COALESCE(SUM(f.line_count), 0) AS total_lines
      FROM files f
      GROUP BY f.repo
    ),
    repo_chunk_stats AS (
      SELECT
        f.repo,
        COUNT(*) AS total_chunks
      FROM code_chunks cc
      JOIN files f ON cc.file_id = f.id
      GROUP BY f.repo
    ),
    repo_symbol_stats AS (
      SELECT
        f.repo,
        COUNT(*) AS total_symbols
      FROM symbols s
      JOIN files f ON s.file_id = f.id
      GROUP BY f.repo
    )
    SELECT
      rfs.repo,
      rfs.total_files,
      rfs.total_lines,
      COALESCE(rcs.total_chunks, 0) AS total_chunks,
      COALESCE(rss.total_symbols, 0) AS total_symbols
    FROM repo_file_stats rfs
    LEFT JOIN repo_chunk_stats rcs ON rcs.repo = rfs.repo
    LEFT JOIN repo_symbol_stats rss ON rss.repo = rfs.repo
    ORDER BY rfs.repo
  `);

  return result.rows.map((row: Record<string, unknown>) => ({
    repo: String(row.repo),
    total_files: Number(row.total_files),
    total_lines: Number(row.total_lines),
    total_chunks: Number(row.total_chunks),
    total_symbols: Number(row.total_symbols),
  }));
}

/**
 * @brief Checks whether a repository exists in the index.
 * @param repo Repository name.
 * @returns True if any file rows exist for the repo.
 */
export async function repositoryExists(repo: string): Promise<boolean> {
  const result = await query(`SELECT 1 FROM files WHERE repo = $1 LIMIT 1`, [repo]);
  return result.rows.length > 0;
}

/**
 * @brief Loads detailed stats for a single repository.
 * @param repo Repository name.
 * @returns Repository stats or null when repo does not exist.
 */
export async function getRepositoryStats(repo: string): Promise<RepositoryStats | null> {
  const summaryResult = await query(
    `
    WITH summary AS (
      SELECT
        $1::text AS repo,
        COUNT(*) AS total_files,
        COALESCE(SUM(f.line_count), 0) AS total_lines
      FROM files f
      WHERE f.repo = $1
    ),
    chunks AS (
      SELECT COUNT(*) AS total_chunks
      FROM code_chunks cc
      JOIN files f ON cc.file_id = f.id
      WHERE f.repo = $1
    ),
    symbols AS (
      SELECT COUNT(*) AS total_symbols
      FROM symbols s
      JOIN files f ON s.file_id = f.id
      WHERE f.repo = $1
    )
    SELECT
      summary.repo,
      summary.total_files,
      summary.total_lines,
      chunks.total_chunks,
      symbols.total_symbols
    FROM summary
    CROSS JOIN chunks
    CROSS JOIN symbols
  `,
    [repo],
  );

  if (summaryResult.rows.length === 0 || Number(summaryResult.rows[0].total_files) === 0) {
    return null;
  }

  const languageResult = await query(
    `
    SELECT COALESCE(language, 'unknown') AS language, COUNT(*) AS count
    FROM files
    WHERE repo = $1
    GROUP BY COALESCE(language, 'unknown')
    ORDER BY count DESC, language
  `,
    [repo],
  );

  const intentResult = await query(
    `
    SELECT COALESCE(cc.intent, 'unclassified') AS intent, COUNT(*) AS count
    FROM code_chunks cc
    JOIN files f ON f.id = cc.file_id
    WHERE f.repo = $1
    GROUP BY COALESCE(cc.intent, 'unclassified')
    ORDER BY count DESC, intent
  `,
    [repo],
  );

  const symbolKindResult = await query(
    `
    SELECT s.kind, COUNT(*) AS count
    FROM symbols s
    JOIN files f ON f.id = s.file_id
    WHERE f.repo = $1
    GROUP BY s.kind
    ORDER BY count DESC, s.kind
  `,
    [repo],
  );

  const summaryRow = summaryResult.rows[0] as Record<string, unknown>;

  return {
    summary: {
      repo: String(summaryRow.repo),
      total_files: Number(summaryRow.total_files),
      total_lines: Number(summaryRow.total_lines),
      total_chunks: Number(summaryRow.total_chunks),
      total_symbols: Number(summaryRow.total_symbols),
    },
    languages: languageResult.rows.map((row: Record<string, unknown>) => ({
      language: String(row.language),
      count: Number(row.count),
    })),
    intents: intentResult.rows.map((row: Record<string, unknown>) => ({
      intent: String(row.intent),
      count: Number(row.count),
    })),
    symbolKinds: symbolKindResult.rows.map((row: Record<string, unknown>) => ({
      kind: String(row.kind),
      count: Number(row.count),
    })),
  };
}

/** @brief Repository-scoped module intent representation. */
export type ModuleIntent = {
  repo: string;
  module_path: string;
  kind: string;
  module_name: string | null;
  summary: string | null;
  role: string | null;
  dominant_intent: string | null;
  file_count: number;
  chunk_count: number;
  updated_at: Date;
};

/**
 * @brief Loads module intents for a repository.
 * @param repo Repository name.
 * @param kind Optional module kind (directory, logical).
 * @param pathPrefix Optional module path prefix to filter by.
 * @returns Array of module intents.
 */
export async function getModuleIntents(repo: string, kind?: string, pathPrefix?: string): Promise<ModuleIntent[]> {
  const conditions = ["repo = $1"];
  const params: any[] = [repo];

  if (kind && kind !== 'all') {
    params.push(kind);
    conditions.push(`kind = $${params.length}`);
  }

  if (pathPrefix) {
    params.push(`${pathPrefix}%`);
    conditions.push(`module_path LIKE $${params.length}`);
  }

  const result = await query(
    `SELECT * FROM module_intents WHERE ${conditions.join(" AND ")} ORDER BY kind, module_path`,
    params
  );

  return result.rows.map((row: any) => ({
    repo: String(row.repo),
    module_path: String(row.module_path),
    kind: String(row.kind),
    module_name: row.module_name ? String(row.module_name) : null,
    summary: row.summary ? String(row.summary) : null,
    role: row.role ? String(row.role) : null,
    dominant_intent: row.dominant_intent ? String(row.dominant_intent) : null,
    file_count: Number(row.file_count),
    chunk_count: Number(row.chunk_count),
    updated_at: new Date(row.updated_at)
  }));
}

/** @brief Storage size breakdown for a repository's index. */
export type RepositoryIndexSize = {
  repo: string;
  file_count: number;
  chunk_count: number;
  symbol_count: number;
  ref_count: number;
  content_bytes: number;
  estimated_embedding_bytes: number;
  estimated_total_bytes: number;
};

/**
 * @brief Returns row counts and estimated storage size for a repository's index.
 * @param repo Repository name.
 * @returns Size breakdown, or null when repo does not exist.
 */
export async function getRepositoryIndexSize(repo: string): Promise<RepositoryIndexSize | null> {
  const result = await query(
    `
    SELECT
      (SELECT COUNT(*) FROM files WHERE repo = $1)::int AS file_count,
      (SELECT COUNT(*) FROM code_chunks cc JOIN files f ON f.id = cc.file_id WHERE f.repo = $1)::int AS chunk_count,
      (SELECT COALESCE(SUM(length(cc.content)), 0) FROM code_chunks cc JOIN files f ON f.id = cc.file_id WHERE f.repo = $1)::bigint AS content_bytes,
      (SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id WHERE f.repo = $1)::int AS symbol_count,
      (SELECT COUNT(*) FROM symbol_references sr JOIN files f ON f.id = sr.source_file_id WHERE f.repo = $1)::int AS ref_count
    `,
    [repo],
  );

  const row = result.rows[0] as Record<string, unknown>;
  if (!row || Number(row.file_count) === 0) {
    return null;
  }

  const chunkCount = Number(row.chunk_count);
  const contentBytes = Number(row.content_bytes);
  // vector(768): 768 floats × 4 bytes + 8 bytes overhead per row
  const embeddingBytes = chunkCount * (768 * 4 + 8);

  return {
    repo,
    file_count: Number(row.file_count),
    chunk_count: chunkCount,
    symbol_count: Number(row.symbol_count),
    ref_count: Number(row.ref_count),
    content_bytes: contentBytes,
    estimated_embedding_bytes: embeddingBytes,
    estimated_total_bytes: contentBytes + embeddingBytes,
  };
}

/**
 * @brief Deletes all indexed data for a repository (files, chunks, symbols, references, dependencies).
 * @param repo Repository name.
 * @returns Number of files deleted.
 */
export async function deleteRepository(repo: string): Promise<number> {
  // Deleting from files cascades to code_chunks, symbols, dependencies, symbol_references
  const result = await query(`DELETE FROM files WHERE repo = $1`, [repo]);
  return result.rowCount ?? 0;
}

/**
 * @brief Loads a repository semantic graph from dependency and reference edges.
 * @param repo Repository name.
 * @param limit Maximum number of aggregated edges to return.
 * @returns Repo graph payload with nodes and weighted edges.
 */
export async function getRepositoryGraph(repo: string, limit = 300): Promise<RepositoryGraph> {
  const safeLimit = Math.max(50, Math.min(limit, 2000));
  const result = await query(
    `
    WITH dependency_edges AS (
      SELECT
        sf.path AS source,
        COALESCE(tf.path, d.external_module) AS target,
        d.kind AS kind
      FROM dependencies d
      JOIN files sf ON sf.id = d.source_file_id
      LEFT JOIN files tf ON tf.id = d.target_file_id
      WHERE sf.repo = $1
        AND (tf.id IS NULL OR tf.repo = $1)
        AND COALESCE(tf.path, d.external_module) IS NOT NULL
    ),
    reference_edges AS (
      SELECT
        sf.path AS source,
        tf.path AS target,
        sr.reference_kind AS kind
      FROM symbol_references sr
      JOIN files sf ON sf.id = sr.source_file_id
      JOIN symbols s ON lower(s.name) = lower(sr.target_name)
      JOIN files tf ON tf.id = s.file_id
      WHERE sf.repo = $1
        AND tf.repo = $1
    ),
    all_edges AS (
      SELECT source, target, kind FROM dependency_edges
      UNION ALL
      SELECT source, target, kind FROM reference_edges
    ),
    grouped_edges AS (
      SELECT source, target, kind, COUNT(*)::int AS weight
      FROM all_edges
      GROUP BY source, target, kind
      ORDER BY weight DESC, source, target, kind
      LIMIT $2
    )
    SELECT source, target, kind, weight
    FROM grouped_edges
    ORDER BY weight DESC, source, target, kind
  `,
    [repo, safeLimit],
  );

  const edges = result.rows.map((row: Record<string, unknown>) => ({
    source: String(row.source),
    target: String(row.target),
    kind: String(row.kind),
    weight: Number(row.weight),
  }));

  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + edge.weight);
    degree.set(edge.target, (degree.get(edge.target) || 0) + edge.weight);
  }

  const nodes = Array.from(degree.entries())
    .map(([id, count]) => ({ id, degree: count }))
    .sort((a, b) => b.degree - a.degree || a.id.localeCompare(b.id));

  return { nodes, edges };
}
