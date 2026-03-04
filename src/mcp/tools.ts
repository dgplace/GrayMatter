/**
 * @file src/mcp/tools.ts
 * @brief MCP tool registration for repo-scoped search, symbols, references, and stats.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../db.js";
import { embed, vecLiteral } from "../embed.js";
import {
  getRepositoryStats,
  listRepositories,
  repositoryExists,
} from "../repositories/store.js";
import {
  formatReferenceResults,
  formatSearchResults,
  formatSymbolResults,
} from "./formatters.js";
import { logToolInvocation } from "./logging.js";
import { keywordSearch } from "./search.js";
import type { ReferenceRow, SearchRow, SymbolRow } from "./types.js";
import { SYMBOL_KIND_VALUES } from "./types.js";

const INTENT_VALUES = [
  "data-model",
  "business-logic",
  "api-endpoint",
  "utility",
  "configuration",
  "test",
  "infrastructure",
  "ui-component",
  "integration",
  "orchestration",
  "type-definition",
  "middleware",
  "migration",
] as const;

/**
 * @brief Creates a consistent not-found payload when a repo is missing.
 * @param repo Repository name supplied by the caller.
 * @returns Text payload ready for MCP content response.
 */
function repoNotFoundText(repo: string): string {
  return `Repository \`${repo}\` is not indexed. Use \`list_repositories\` to discover available repositories.`;
}

/**
 * @brief Checks that a repo exists before executing a repo-scoped query.
 * @param repo Repository name.
 * @returns Null when present, or an MCP response object when absent.
 */
async function requireRepository(repo: string): Promise<{ content: Array<{ type: "text"; text: string }> } | null> {
  if (await repositoryExists(repo)) {
    return null;
  }
  return { content: [{ type: "text", text: repoNotFoundText(repo) }] };
}

/**
 * @brief Registers all CodeBrain MCP tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerTools(server: McpServer): void {
  server.tool(
    "list_repositories",
    "Lists indexed repositories and top-level counts so callers can select a repo for mandatory repo-scoped query tools.",
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

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "semantic_search",
    "Use for concept-based discovery when the exact symbol name is unknown. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      query: z
        .string()
        .describe("Short technical search phrase. Prefer 2-8 words with framework names, APIs, or domain terms."),
      limit: z.number().optional().default(10).describe("Max results (default 10)."),
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
        .default(0.3)
        .describe("Semantic similarity threshold 0-1. Lower this when codebase terminology is sparse."),
    },
    async ({ repo, query: searchQuery, limit, intent, language, path_prefix, threshold }) => {
      logToolInvocation("semantic_search", {
        repo,
        query: searchQuery,
        limit,
        intent,
        language,
        path_prefix,
        threshold,
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

      return { content: [{ type: "text", text: formatSearchResults(rows) }] };
    },
  );

  server.tool(
    "find_symbol",
    "Use first when you know part of a symbol name. Repository scope is required.",
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
        return { content: [{ type: "text", text: `No symbols found matching "${name}" in repo \`${repo}\`.` }] };
      }

      return { content: [{ type: "text", text: formatSymbolResults(result.rows as SymbolRow[]) }] };
    },
  );

  server.tool(
    "exact_symbol_search",
    "Use for exact identifier lookups when you need grep-like precision. Repository scope is required.",
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
        return { content: [{ type: "text", text: `No exact symbol matches found for "${name}" in repo \`${repo}\`.` }] };
      }

      return { content: [{ type: "text", text: formatSymbolResults(result.rows as SymbolRow[]) }] };
    },
  );

  server.tool(
    "find_references",
    "Finds indexed lexical and call references to an exact symbol name. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      name: z.string().describe("Exact symbol name to find references for."),
      file: z.string().optional().describe("Optional target declaration file filter to disambiguate common names."),
      limit: z.number().optional().default(25).describe("Max references to return (default 25)."),
    },
    async ({ repo, name, file, limit }) => {
      logToolInvocation("find_references", { repo, name, file, limit });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        SELECT
          sf.path AS source_path,
          sr.line_no,
          sr.reference_kind,
          sr.source_symbol_name,
          array_remove(array_agg(DISTINCT tf.path), NULL) AS target_paths
        FROM symbol_references sr
        JOIN files sf ON sf.id = sr.source_file_id
        LEFT JOIN symbols s ON lower(s.name) = lower(sr.target_name)
        LEFT JOIN files tf ON tf.id = s.file_id AND tf.repo = $2
        WHERE lower(sr.target_name) = lower($1)
          AND sf.repo = $2
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
        GROUP BY sf.path, sr.line_no, sr.reference_kind, sr.source_symbol_name
        ORDER BY sf.path, sr.line_no
        LIMIT $4
      `,
        [name, repo, file || null, limit],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No references found for "${name}" in repo \`${repo}\`.` }] };
      }

      return { content: [{ type: "text", text: formatReferenceResults(result.rows as ReferenceRow[], name) }] };
    },
  );

  server.tool(
    "trace_dependencies",
    "Follows dependency edges to answer what depends on X and what X depends on. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path: z.string().describe("File path or distinctive partial path to trace."),
      direction: z
        .enum(["inbound", "outbound", "both"])
        .optional()
        .default("both")
        .describe("Use inbound for reverse dependencies, outbound for direct dependencies, both for a quick graph walk."),
      max_depth: z.number().optional().default(3).describe("Depth limit for the graph walk (default 3)."),
    },
    async ({ repo, path, direction, max_depth }) => {
      logToolInvocation("trace_dependencies", { repo, path, direction, max_depth });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        WITH RECURSIVE target_files AS (
          SELECT id, path
          FROM files
          WHERE repo = $2
            AND path LIKE '%' || $1 || '%'
        ),
        target_symbol_names AS (
          SELECT DISTINCT s.name
          FROM symbols s
          JOIN target_files tf ON tf.id = s.file_id
        ),
        edges AS (
          SELECT
            d.source_file_id,
            COALESCE(direct_target.id, ts_file.id, resolved_target.file_id) AS target_file_id,
            d.kind AS dep_kind,
            ss.name AS source_symbol,
            COALESCE(ts.name, resolved_target.name) AS target_symbol,
            d.external_module
          FROM dependencies d
          JOIN files source_file ON source_file.id = d.source_file_id AND source_file.repo = $2
          LEFT JOIN files direct_target ON direct_target.id = d.target_file_id AND direct_target.repo = $2
          LEFT JOIN symbols ss ON ss.id = d.source_symbol_id
          LEFT JOIN symbols ts ON ts.id = d.target_symbol_id
          LEFT JOIN files ts_file ON ts_file.id = ts.file_id AND ts_file.repo = $2
          LEFT JOIN LATERAL (
            SELECT s.id, s.file_id, s.name
            FROM symbols s
            JOIN files rf ON rf.id = s.file_id
            WHERE d.target_symbol_id IS NULL
              AND d.external_module IS NOT NULL
              AND lower(s.name) = lower(d.external_module)
              AND rf.repo = $2
            ORDER BY
              CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
              CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
              s.is_exported DESC,
              s.start_line
            LIMIT 1
          ) resolved_target ON TRUE

          UNION ALL

          SELECT
            sr.source_file_id,
            target_file.id AS target_file_id,
            sr.reference_kind AS dep_kind,
            sr.source_symbol_name AS source_symbol,
            s.name AS target_symbol,
            NULL::text AS external_module
          FROM symbol_references sr
          JOIN files source_file ON source_file.id = sr.source_file_id AND source_file.repo = $2
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN files target_file ON target_file.id = s.file_id AND target_file.repo = $2
        ),
        dep_tree AS (
          SELECT
            sf.path AS source_path,
            COALESCE(tf.path, e.external_module) AS target_path_out,
            e.dep_kind,
            e.source_symbol,
            e.target_symbol,
            e.external_module,
            1 AS depth,
            e.target_file_id
          FROM edges e
          JOIN files sf ON sf.id = e.source_file_id
          LEFT JOIN files tf ON tf.id = e.target_file_id
          WHERE (
              $3 IN ('outbound', 'both')
              AND e.source_file_id IN (SELECT id FROM target_files)
            )
            OR (
              $3 IN ('inbound', 'both')
              AND (
                e.target_file_id IN (SELECT id FROM target_files)
                OR (
                  e.target_file_id IS NOT NULL
                  AND e.target_symbol IN (SELECT name FROM target_symbol_names)
                )
              )
            )

          UNION ALL

          SELECT
            sf.path AS source_path,
            COALESCE(tf.path, e.external_module) AS target_path_out,
            e.dep_kind,
            e.source_symbol,
            e.target_symbol,
            e.external_module,
            dt.depth + 1 AS depth,
            e.target_file_id
          FROM dep_tree dt
          JOIN edges e ON e.source_file_id = dt.target_file_id
          JOIN files sf ON sf.id = e.source_file_id
          LEFT JOIN files tf ON tf.id = e.target_file_id
          WHERE dt.depth < $4
        )
        SELECT DISTINCT
          source_path,
          target_path_out,
          dep_kind,
          source_symbol,
          target_symbol,
          external_module,
          depth
        FROM dep_tree
        ORDER BY
          depth,
          CASE dep_kind
            WHEN 'service_usage' THEN 0
            WHEN 'injection' THEN 1
            WHEN 'member_call' THEN 2
            WHEN 'call' THEN 3
            WHEN 'type_reference' THEN 4
            WHEN 'import' THEN 9
            ELSE 5
          END,
          source_path,
          target_path_out
        LIMIT 200
      `,
        [path, repo, direction, max_depth],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No dependencies found for "${path}" in repo \`${repo}\`.` }] };
      }

      const formatted = result.rows
        .map((row: Record<string, unknown>) => {
          const arrow = "->";
          const sourcePath = String(row.source_path);
          const sourceSymbol = row.source_symbol ? ` (${String(row.source_symbol)})` : "";
          const targetBase = row.target_path_out ? String(row.target_path_out) : String(row.external_module || "unknown");
          const target = row.target_symbol ? `${targetBase} (${String(row.target_symbol)})` : targetBase;
          const depth = Number(row.depth);
          return `${"  ".repeat(Math.max(depth - 1, 0))}${sourcePath}${sourceSymbol} ${arrow} ${target} [${String(row.dep_kind)}]`;
        })
        .join("\n");

      return { content: [{ type: "text", text: `Dependency trace for \`${path}\` in repo \`${repo}\`:\n\n${formatted}` }] };
    },
  );

  server.tool(
    "get_file_map",
    "Gives an architectural map of a directory or subsystem. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path_prefix: z.string().optional().default("").describe("Directory or path prefix to inspect."),
    },
    async ({ repo, path_prefix }) => {
      logToolInvocation("get_file_map", { repo, path_prefix });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        SELECT
          f.path,
          f.language,
          f.line_count,
          f.role,
          f.summary,
          array_agg(DISTINCT s.name || ' (' || s.kind || ')') FILTER (WHERE s.name IS NOT NULL) AS symbols
        FROM files f
        LEFT JOIN symbols s ON s.file_id = f.id AND s.is_exported = true
        WHERE f.repo = $1
          AND f.path LIKE $2 || '%'
        GROUP BY f.id
        ORDER BY f.path
      `,
        [repo, path_prefix],
      );

      if (result.rows.length === 0) {
        return {
          content: [
            {
              type: "text",
              text: `No files found in repo \`${repo}\` under \`${path_prefix || "(root)"}\`.`,
            },
          ],
        };
      }

      const formatted = result.rows
        .map((row: Record<string, unknown>) => {
          const symbols = Array.isArray(row.symbols) ? row.symbols.join(", ") : "none";
          return [
            `FILE **${String(row.path)}** (${String(row.language || "unknown")}, ${Number(row.line_count)} lines)`,
            `   Role: ${String(row.role || "unclassified")}`,
            row.summary ? `   Summary: ${String(row.summary)}` : "",
            `   Exports: ${symbols}`,
          ]
            .filter(Boolean)
            .join("\n");
        })
        .join("\n\n");

      return { content: [{ type: "text", text: formatted }] };
    },
  );

  server.tool(
    "get_intent",
    "Summarizes what a file is for and what its indexed code sections are doing. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path: z.string().describe("File path or distinctive partial path to inspect before editing."),
    },
    async ({ repo, path }) => {
      logToolInvocation("get_intent", { repo, path });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const fileResult = await query(
        `
        SELECT f.path, f.summary, f.role
        FROM files f
        WHERE f.repo = $1
          AND f.path LIKE '%' || $2 || '%'
        ORDER BY f.path
        LIMIT 1
      `,
        [repo, path],
      );

      if (fileResult.rows.length === 0) {
        return { content: [{ type: "text", text: `File \`${path}\` not found in repo \`${repo}\`.` }] };
      }

      const file = fileResult.rows[0] as Record<string, unknown>;
      const resolvedPath = String(file.path);

      const chunkResult = await query(
        `
        SELECT
          cc.symbol_name,
          cc.symbol_type,
          cc.intent,
          cc.intent_detail,
          cc.start_line,
          cc.end_line
        FROM code_chunks cc
        JOIN files f ON cc.file_id = f.id
        WHERE f.repo = $1
          AND f.path = $2
        ORDER BY cc.chunk_index
      `,
        [repo, resolvedPath],
      );

      let output = `# ${resolvedPath}\n\n`;
      output += `**Repository:** ${repo}\n`;
      output += `**Role:** ${String(file.role || "unknown")}\n`;
      output += `**Summary:** ${String(file.summary || "no summary")}\n\n`;
      output += "## Code Sections\n\n";

      for (const chunk of chunkResult.rows as Array<Record<string, unknown>>) {
        output += `- **${String(chunk.symbol_type || "block")}** `;
        if (chunk.symbol_name) {
          output += `\`${String(chunk.symbol_name)}\` `;
        }
        output += `(L${Number(chunk.start_line)}-${Number(chunk.end_line)})`;
        if (chunk.intent) {
          output += ` - *${String(chunk.intent)}*`;
        }
        if (chunk.intent_detail) {
          output += `: ${String(chunk.intent_detail)}`;
        }
        output += "\n";
      }

      return { content: [{ type: "text", text: output }] };
    },
  );

  server.tool(
    "codebase_stats",
    "Returns high-level metrics for a selected repository. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to summarize. Required."),
    },
    async ({ repo }) => {
      logToolInvocation("codebase_stats", { repo });

      const stats = await getRepositoryStats(repo);
      if (!stats) {
        return { content: [{ type: "text", text: repoNotFoundText(repo) }] };
      }

      let output = `# Repository Statistics\n\n`;
      output += `## ${stats.summary.repo}\n`;
      output += `- **Files:** ${stats.summary.total_files}\n`;
      output += `- **Lines:** ${stats.summary.total_lines.toLocaleString()}\n`;
      output += `- **Chunks:** ${stats.summary.total_chunks}\n`;
      output += `- **Symbols:** ${stats.summary.total_symbols}\n\n`;

      output += "### Languages\n";
      for (const language of stats.languages) {
        output += `- ${language.language}: ${language.count} files\n`;
      }

      output += "\n### Intent Distribution\n";
      for (const intent of stats.intents) {
        output += `- ${intent.intent}: ${intent.count} chunks\n`;
      }

      output += "\n### Symbol Kinds\n";
      for (const kind of stats.symbolKinds) {
        output += `- ${kind.kind}: ${kind.count}\n`;
      }

      return { content: [{ type: "text", text: output }] };
    },
  );
}
