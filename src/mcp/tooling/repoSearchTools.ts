/**
 * @file src/mcp/tooling/repoSearchTools.ts
 * @brief MCP tools for repository listing and symbol/search discovery.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../../db.js";
import { embed, vecLiteral } from "../../embed.js";
import { listRepositories } from "../../repositories/store.js";
import { formatReferenceResults, formatSearchResults, formatSymbolResults } from "../formatters.js";
import { logToolInvocation } from "../logging.js";
import { keywordSearch } from "../search.js";
import { SYMBOL_KIND_VALUES } from "../types.js";
import type { ReferenceRow, SearchRow, SymbolRow } from "../types.js";
import { DOCUMENTATION_INTENT, INTENT_VALUES, nextStepFooter, requireRepository } from "./shared.js";

/**
 * @brief Registers repository/symbol search tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerRepoSearchTools(server: McpServer): void {
  server.tool(
    "list_repositories",
    "Call first to discover the exact `repo` string -- never guess it from the working directory. All other tools require `repo`.",
    {},
    async () => {
      logToolInvocation("list_repositories");
      const repos = await listRepositories();
      if (repos.length === 0) {
        return { content: [{ type: "text", text: "No repositories are indexed yet." }] };
      }

      const lines = ["# Indexed Repositories", ""];
      for (const repo of repos) {
        lines.push(`- **${repo.repo}** - files: ${repo.total_files}, lines: ${repo.total_lines.toLocaleString()}, chunks: ${repo.total_chunks}, symbols: ${repo.total_symbols}`);
      }
      lines.push(
        "",
        "Tool naming note: dependency/impact traversal tools use `find_*` names (`find_call_graph`, `find_cycles`, `find_impact`, `find_external_dependencies`).",
      );

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "semantic_search",
    "Use ONLY when no identifier is known and the question is conceptual. If you know any part of a name, prefer `find_symbol` or `exact_symbol_search`. If two queries miss, switch tools -- do not rephrase.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      query: z
        .string()
        .describe("Short technical search phrase. Prefer 2-8 words with framework names, APIs, or domain terms."),
      limit: z.number().optional().describe("Max results (default 10)."),
      intent: z
        .enum(INTENT_VALUES)
        .optional()
        .describe("Optional intent filter when you already know the kind of code you want."),
      language: z.string().optional().describe("Optional language filter (python, typescript, swift, etc.)."),
      path_prefix: z
        .string()
        .optional()
        .describe("Optional path prefix to focus search on a subsystem (for example src/api/)."),
      threshold: z
        .number()
        .optional()
        .describe("Semantic similarity threshold 0-1. Lower this when codebase terminology is sparse."),
      include_documentation: z
        .boolean()
        .optional()
        .describe("When false (default), documentation-intent chunks are filtered out to prioritize code matches."),
    },
    async ({ repo, query: searchQuery, limit = 10, intent, language, path_prefix, threshold = 0.3, include_documentation = false }) => {
      logToolInvocation("semantic_search", {
        repo,
        query: searchQuery,
        limit,
        intent,
        language,
        path_prefix,
        threshold,
        include_documentation,
      });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const embedding = await embed(searchQuery);

      const semanticResult = await query(
        `SELECT * FROM search_code($1::vector, $2, $3, $4, $5, NULL, $6, $7)`,
        [vecLiteral(embedding), limit, intent || null, language || null, path_prefix || null, threshold, repo],
      );
      const keywordResult = await keywordSearch(searchQuery, repo, limit, intent, language, path_prefix);

      const merged = new Map<number, SearchRow>();
      for (const row of semanticResult.rows as SearchRow[]) {
        merged.set(row.chunk_id, { ...row, keyword_score: 0 });
      }

      for (const row of keywordResult) {
        const existing = merged.get(row.chunk_id);
        if (existing) {
          existing.keyword_score = Math.max(existing.keyword_score || 0, row.keyword_score || 0);
        } else {
          merged.set(row.chunk_id, row);
        }
      }

      const rows = Array.from(merged.values())
        .filter((row) => include_documentation || row.intent !== DOCUMENTATION_INTENT)
        .sort((a, b) => {
          const aSemantic = a.similarity ?? -1;
          const bSemantic = b.similarity ?? -1;
          if (bSemantic !== aSemantic) {
            return bSemantic - aSemantic;
          }

          const aKeyword = a.keyword_score ?? 0;
          const bKeyword = b.keyword_score ?? 0;
          if (bKeyword !== aKeyword) {
            return bKeyword - aKeyword;
          }

          return a.file_path.localeCompare(b.file_path) || a.start_line - b.start_line;
        })
        .slice(0, limit);

      if (rows.length === 0) {
        return {
          content: [
            {
              type: "text",
              text: "No results found. Try broadening your query, lowering the threshold, or using more specific symbol names.",
            },
          ],
        };
      }

      return { content: [{ type: "text", text: formatSearchResults(rows) + nextStepFooter("semantic_search") }] };
    },
  );

  server.tool(
    "find_symbol",
    "FIRST RESORT after `list_repositories`. Try this with the user's own nouns -- lowercase, English, partial all hit (case-insensitive, ranked). Do not pre-judge whether a word \"looks like\" an identifier: `polyline`, `canvas`, `auth`, `payment` all work and usually hit. Only fall through to `get_file_map`/`semantic_search` if this returns nothing.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      name: z
        .string()
        .describe("Partial or exact symbol name. Start here before broad text search when you know the identifier."),
      kind: z.enum(SYMBOL_KIND_VALUES).optional().describe("Optional symbol kind filter to narrow ambiguous names."),
      file: z.string().optional().describe("Optional filename filter when the symbol is likely in a known file or module."),
    },
    async ({ repo, name, kind, file }) => {
      logToolInvocation("find_symbol", { repo, name, kind, file });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
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
          s.is_exported,
          s.container_symbol,
          s.declared_in_extension,
          s.is_primary_declaration
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE f.repo = $4
          AND (
            s.name ILIKE '%' || $1 || '%'
            OR COALESCE(s.qualified_name, '') ILIKE '%' || $1 || '%'
            OR COALESCE(s.signature, '') ILIKE '%' || $1 || '%'
          )
          AND ($2::text IS NULL OR s.kind = $2)
          AND ($3::text IS NULL OR f.path LIKE '%' || $3 || '%')
        ORDER BY
          CASE
            WHEN s.name = $1 THEN 0
            WHEN lower(s.name) = lower($1) THEN 1
            WHEN COALESCE(s.qualified_name, '') = $1 THEN 2
            WHEN COALESCE(s.qualified_name, '') ILIKE '%:' || $1 THEN 3
            WHEN COALESCE(s.signature, '') ILIKE $1 || '%' THEN 4
            WHEN s.name ILIKE $1 || '%' THEN 5
            WHEN COALESCE(s.qualified_name, '') ILIKE '%' || $1 || '%' THEN 6
            WHEN COALESCE(s.signature, '') ILIKE '%' || $1 || '%' THEN 7
            ELSE 8
          END,
          CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
          CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
          CASE WHEN s.kind IN ('class', 'struct', 'protocol', 'interface', 'enum', 'extension') THEN 0 ELSE 1 END,
          s.is_exported DESC,
          f.path,
          s.start_line
        LIMIT 25
      `,
        [name, kind || null, file || null, repo],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No symbols found matching "${name}" in repo \`${repo}\`. Try a different noun from the user's question, or fall back to \`semantic_search\` / \`get_file_map\` (no path_prefix).` }] };
      }

      return {
        content: [
          { type: "text", text: formatSymbolResults(result.rows as SymbolRow[]) + nextStepFooter("find_symbol") },
        ],
      };
    },
  );

  server.tool(
    "exact_symbol_search",
    "Use when you know the exact identifier. Grep-like precision, unambiguous. Always prefer this over `semantic_search` when the name is known.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      name: z.string().describe("Exact symbol or method name to match."),
      kind: z.enum(SYMBOL_KIND_VALUES).optional().describe("Optional symbol kind filter to narrow exact matches."),
      file: z.string().optional().describe("Optional file filter when declaration is expected in a known module or file."),
    },
    async ({ repo, name, kind, file }) => {
      logToolInvocation("exact_symbol_search", { repo, name, kind, file });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
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
          s.is_exported,
          s.container_symbol,
          s.declared_in_extension,
          s.is_primary_declaration
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE f.repo = $4
          AND (
            lower(s.name) = lower($1)
            OR COALESCE(s.qualified_name, '') ILIKE '%' || $1
            OR COALESCE(s.signature, '') ILIKE $1 || '%'
          )
          AND ($2::text IS NULL OR s.kind = $2)
          AND ($3::text IS NULL OR f.path LIKE '%' || $3 || '%')
        ORDER BY
          CASE WHEN lower(s.name) = lower($1) THEN 0 ELSE 1 END,
          CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
          CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
          s.is_exported DESC,
          f.path,
          s.start_line
        LIMIT 25
      `,
        [name, kind || null, file || null, repo],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No exact symbol matches found for "${name}" in repo \`${repo}\`. Try \`find_symbol\` for partial matches.` }] };
      }

      return {
        content: [
          { type: "text", text: formatSymbolResults(result.rows as SymbolRow[]) + nextStepFooter("exact_symbol_search") },
        ],
      };
    },
  );

  server.tool(
    "find_references",
    "Use when you need callers/usages of a known symbol. Returns resolved high-confidence edges by default; pass `include_unresolved: true` for heuristic matches, or `reference_kind` to filter call/member_call/type_reference/instantiation.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      name: z.string().describe("Exact symbol name to find references for."),
      file: z.string().optional().describe("Optional target declaration file filter to disambiguate common names."),
      reference_kind: z
        .enum(["call", "member_call", "type_reference", "instantiation"])
        .optional()
        .describe("Optional richer reference kind filter. When omitted, all kinds are returned."),
      limit: z.number().optional().describe("Max references to return (default 25)."),
      min_confidence: z
        .number()
        .min(0)
        .max(1)
        .optional()
        .describe("Minimum resolution_confidence to include (default 0.55). Set to 0 to include unresolved heuristic name matches."),
      include_unresolved: z
        .boolean()
        .optional()
        .describe("When true, returns the full set including rows below the confidence threshold."),
    },
    async ({ repo, name, file, reference_kind, limit = 25, min_confidence = 0.55, include_unresolved = false }) => {
      logToolInvocation("find_references", { repo, name, file, reference_kind, limit, min_confidence, include_unresolved });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        SELECT
          sf.path AS source_path,
          sr.line_no,
          COALESCE(sr.reference_kind_v2, sr.reference_kind) AS reference_kind,
          sr.source_symbol_name,
          sr.resolution_confidence,
          sr.resolution_method,
          array_remove(array_agg(DISTINCT COALESCE(rs_file.path, tf.path)), NULL) AS target_paths
        FROM symbol_references sr
        JOIN files sf ON sf.id = sr.source_file_id
        LEFT JOIN symbols rs ON rs.id = sr.target_symbol_id
        LEFT JOIN files rs_file ON rs_file.id = rs.file_id AND rs_file.repo = $2
        LEFT JOIN symbols s ON sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name)
        LEFT JOIN files tf ON tf.id = s.file_id AND tf.repo = $2
        WHERE lower(sr.target_name) = lower($1)
          AND sf.repo = $2
          AND ($7::text IS NULL OR COALESCE(sr.reference_kind_v2, sr.reference_kind) = $7)
          AND ($5::boolean OR COALESCE(sr.resolution_confidence, 0) >= $4)
          AND (
            $3::text IS NULL
            OR EXISTS (
              SELECT 1
              FROM symbols s2
              JOIN files tf2 ON tf2.id = s2.file_id
              WHERE lower(s2.name) = lower(sr.target_name)
                AND tf2.repo = $2
                AND tf2.path LIKE '%' || $3 || '%'
            )
          )
        GROUP BY sf.path, sr.line_no, COALESCE(sr.reference_kind_v2, sr.reference_kind), sr.source_symbol_name, sr.resolution_confidence, sr.resolution_method
        ORDER BY sr.resolution_confidence DESC NULLS LAST, sf.path, sr.line_no
        LIMIT $6
      `,
        [name, repo, file || null, min_confidence, include_unresolved, limit, reference_kind || null],
      );

      if (result.rows.length === 0) {
        const hint = !include_unresolved && min_confidence > 0
          ? ` (confidence threshold ${min_confidence}; pass include_unresolved=true to see heuristic-only matches)`
          : "";
        return { content: [{ type: "text", text: `No references found for "${name}" in repo \`${repo}\`${hint}.` }] };
      }

      return {
        content: [
          { type: "text", text: formatReferenceResults(result.rows as ReferenceRow[], name) + nextStepFooter("find_references") },
        ],
      };
    },
  );

}
