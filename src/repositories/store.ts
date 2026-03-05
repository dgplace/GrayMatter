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
