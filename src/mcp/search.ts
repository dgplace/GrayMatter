/**
 * @file src/mcp/search.ts
 * @brief Query preprocessing and keyword search fallback for semantic discovery.
 */

import { query } from "../db.js";
import type { SearchRow } from "./types.js";

/**
 * @brief Extracts compact keyword terms from natural language search input.
 * @param text Search phrase from the MCP client.
 * @returns Deduplicated terms suitable for SQL ILIKE fallback queries.
 */
export function extractKeywordTerms(text: string): string[] {
  const stopwords = new Set([
    "a",
    "an",
    "and",
    "bar",
    "by",
    "code",
    "config",
    "configuration",
    "file",
    "for",
    "how",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
    "with",
  ]);
  const seen = new Set<string>();
  const terms: string[] = [];

  for (const raw of text.toLowerCase().split(/[^a-z0-9_]+/)) {
    const term = raw.trim();
    if (term.length < 3 || stopwords.has(term) || seen.has(term)) {
      continue;
    }
    seen.add(term);
    terms.push(term);
  }

  return terms.slice(0, 6);
}

/**
 * @brief Runs repo-scoped keyword fallback search over indexed chunks and file paths.
 * @param searchQuery User search text.
 * @param repo Repository name to scope the search to.
 * @param limit Max rows to return.
 * @param intent Optional intent filter.
 * @param language Optional language filter.
 * @param pathPrefix Optional file path prefix filter.
 * @returns Search rows ranked by keyword score.
 */
export async function keywordSearch(
  searchQuery: string,
  repo: string,
  limit: number,
  intent?: string,
  language?: string,
  pathPrefix?: string,
): Promise<SearchRow[]> {
  const terms = extractKeywordTerms(searchQuery);
  if (terms.length === 0) {
    return [];
  }

  const params: unknown[] = [repo];
  const termSql: string[] = [];
  const scoreParts: string[] = [];

  for (const term of terms) {
    const pattern = `%${term.replace(/[%_\\]/g, "\\$&")}%`;

    params.push(pattern);
    const contentIndex = params.length;
    params.push(pattern);
    const symbolIndex = params.length;
    params.push(pattern);
    const pathIndex = params.length;

    termSql.push(
      `(cc.content ILIKE $${contentIndex} ESCAPE '\\' OR COALESCE(cc.symbol_name, '') ILIKE $${symbolIndex} ESCAPE '\\' OR f.path ILIKE $${pathIndex} ESCAPE '\\')`,
    );
    scoreParts.push(
      `(CASE WHEN cc.content ILIKE $${contentIndex} ESCAPE '\\' THEN 1 ELSE 0 END + CASE WHEN COALESCE(cc.symbol_name, '') ILIKE $${symbolIndex} ESCAPE '\\' THEN 3 ELSE 0 END + CASE WHEN f.path ILIKE $${pathIndex} ESCAPE '\\' THEN 2 ELSE 0 END)`,
    );
  }

  let filterSql = "";
  if (intent) {
    params.push(intent);
    filterSql += ` AND cc.intent = $${params.length}`;
  }
  if (language) {
    params.push(language);
    filterSql += ` AND f.language = $${params.length}`;
  }
  if (pathPrefix) {
    params.push(`${pathPrefix}%`);
    filterSql += ` AND f.path LIKE $${params.length}`;
  }

  params.push(Math.max(limit * 3, 20));

  const sql = `
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
      NULL::float AS similarity,
      ${scoreParts.join(" + ")} AS keyword_score
    FROM code_chunks cc
    JOIN files f ON cc.file_id = f.id
    WHERE f.repo = $1
      AND (${termSql.join(" OR ")})
      ${filterSql}
    ORDER BY keyword_score DESC, f.path, cc.start_line
    LIMIT $${params.length}
  `;

  const result = await query(sql, params);
  return result.rows as SearchRow[];
}
