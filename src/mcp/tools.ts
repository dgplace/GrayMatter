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
  formatCouplingAnalysis,
  formatCycles,
  formatModularizationSeams,
  formatModuleInterface,
  formatReferenceResults,
  formatSearchResults,
  formatSymbolResults,
} from "./formatters.js";
import { findCycles, rankNodesByCycleParticipation } from "./graph.js";
import { logToolInvocation } from "./logging.js";
import { keywordSearch } from "./search.js";
import type {
  CouplingEdgeRow,
  GraphEdge,
  ModuleInterfaceRow,
  ReferenceRow,
  SearchRow,
  SeamRow,
  SymbolRow,
} from "./types.js";
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
      summary: z.boolean().optional().default(false).describe("When true, returns aggregated counts by file and kind instead of individual edges."),
    },
    async ({ repo, path, direction, max_depth, summary }) => {
      logToolInvocation("trace_dependencies", { repo, path, direction, max_depth, summary });

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

      if (summary) {
        // Aggregate by connected file + kind with counts
        const groups = new Map<string, { direction: string; file: string; kind: string; count: number; minDepth: number }>();
        for (const row of result.rows as Array<Record<string, unknown>>) {
          const sourcePath = String(row.source_path);
          const targetPath = row.target_path_out ? String(row.target_path_out) : String(row.external_module || "unknown");
          const kind = String(row.dep_kind);
          const depth = Number(row.depth);
          const isOutbound = sourcePath.includes(path);
          const dir = isOutbound ? "outbound" : "inbound";
          const connectedFile = isOutbound ? targetPath : sourcePath;
          const key = `${dir}:${connectedFile}:${kind}`;
          const existing = groups.get(key);
          if (existing) {
            existing.count++;
            existing.minDepth = Math.min(existing.minDepth, depth);
          } else {
            groups.set(key, { direction: dir, file: connectedFile, kind, count: 1, minDepth: depth });
          }
        }

        const sorted = Array.from(groups.values()).sort((a, b) => b.count - a.count);
        const inbound = sorted.filter((g) => g.direction === "inbound");
        const outbound = sorted.filter((g) => g.direction === "outbound");

        const lines = [`## Dependency Summary for ${path}`, ""];
        if (inbound.length > 0) {
          lines.push("### Inbound (what depends on this)", "");
          lines.push("| File | Kind | Edges | Min Depth |");
          lines.push("|------|------|-------|-----------|");
          for (const g of inbound) {
            lines.push(`| ${g.file} | ${g.kind} | ${g.count} | ${g.minDepth} |`);
          }
          lines.push("");
        }
        if (outbound.length > 0) {
          lines.push("### Outbound (what this depends on)", "");
          lines.push("| File | Kind | Edges | Min Depth |");
          lines.push("|------|------|-------|-----------|");
          for (const g of outbound) {
            lines.push(`| ${g.file} | ${g.kind} | ${g.count} | ${g.minDepth} |`);
          }
        }

        return { content: [{ type: "text", text: lines.join("\n") }] };
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

  /* ------------------------------------------------------------------ */
  /*  Refactoring analysis tools                                        */
  /* ------------------------------------------------------------------ */

  server.tool(
    "analyze_coupling",
    "Quantifies how tightly coupled a subsystem is to the rest of the codebase. Reports afferent/efferent coupling, instability metric, and top cross-boundary file pairs. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().describe("Path prefix identifying the subsystem to analyze (e.g. 'src/payments/')."),
      top_n: z.number().optional().default(20).describe("Number of top coupling pairs to return (default 20)."),
    },
    async ({ repo, path_prefix, top_n }) => {
      logToolInvocation("analyze_coupling", { repo, path_prefix, top_n });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      // Count internal files
      const fileCountResult = await query(
        `SELECT COUNT(*) AS cnt FROM files WHERE repo = $1 AND path LIKE $2 || '%'`,
        [repo, path_prefix],
      );
      const internalFileCount = Number((fileCountResult.rows[0] as Record<string, unknown>).cnt);

      if (internalFileCount === 0) {
        return { content: [{ type: "text", text: `No files found under \`${path_prefix}\` in repo \`${repo}\`.` }] };
      }

      // Cross-boundary edges from dependencies + symbol_references
      const result = await query(
        `
        WITH target_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        outbound AS (
          SELECT
            tf.path AS internal_path,
            COALESCE(ef.path, d.external_module, '(external)') AS external_path,
            d.kind,
            COUNT(*) AS edge_count
          FROM dependencies d
          JOIN target_files tf ON tf.id = d.source_file_id
          LEFT JOIN files ef ON ef.id = d.target_file_id AND ef.repo = $1
          WHERE (ef.id IS NULL OR ef.path NOT LIKE $2 || '%')
          GROUP BY tf.path, COALESCE(ef.path, d.external_module, '(external)'), d.kind
        ),
        inbound AS (
          SELECT
            tf.path AS internal_path,
            sf.path AS external_path,
            d.kind,
            COUNT(*) AS edge_count
          FROM dependencies d
          JOIN target_files tf ON tf.id = d.target_file_id
          JOIN files sf ON sf.id = d.source_file_id AND sf.repo = $1
          WHERE sf.path NOT LIKE $2 || '%'
          GROUP BY tf.path, sf.path, d.kind
        ),
        ref_outbound AS (
          SELECT
            tf.path AS internal_path,
            ef.path AS external_path,
            sr.reference_kind AS kind,
            COUNT(*) AS edge_count
          FROM symbol_references sr
          JOIN target_files tf ON tf.id = sr.source_file_id
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
          WHERE ef.path NOT LIKE $2 || '%'
          GROUP BY tf.path, ef.path, sr.reference_kind
        ),
        ref_inbound AS (
          SELECT
            tf.path AS internal_path,
            sf.path AS external_path,
            sr.reference_kind AS kind,
            COUNT(*) AS edge_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN target_files tf ON tf.id = s.file_id
          WHERE sf.path NOT LIKE $2 || '%'
          GROUP BY tf.path, sf.path, sr.reference_kind
        )
        SELECT 'outbound' AS direction, internal_path, external_path, kind, edge_count FROM outbound
        UNION ALL
        SELECT 'inbound', internal_path, external_path, kind, edge_count FROM inbound
        UNION ALL
        SELECT 'ref_outbound', internal_path, external_path, kind, edge_count FROM ref_outbound
        UNION ALL
        SELECT 'ref_inbound', internal_path, external_path, kind, edge_count FROM ref_inbound
        ORDER BY edge_count DESC
        `,
        [repo, path_prefix],
      );

      const rows = result.rows as CouplingEdgeRow[];
      if (rows.length === 0) {
        return { content: [{ type: "text", text: `No cross-boundary dependencies found for \`${path_prefix}\` in repo \`${repo}\`. Module appears isolated.` }] };
      }

      const text = formatCouplingAnalysis(path_prefix, repo, rows, internalFileCount, top_n);
      return { content: [{ type: "text", text }] };
    },
  );

  server.tool(
    "extract_module_interface",
    "Extracts the public surface of a subsystem -- the exported symbols that external code actually references. Shows what interface a replacement module must implement. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().describe("Path prefix of the module (e.g. 'src/auth/')."),
      include_unused: z.boolean().optional().default(false)
        .describe("Also show exported symbols with no external consumers."),
    },
    async ({ repo, path_prefix, include_unused }) => {
      logToolInvocation("extract_module_interface", { repo, path_prefix, include_unused });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      const result = await query(
        `
        WITH module_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        module_symbols AS (
          SELECT s.id, s.name, s.qualified_name, s.kind, s.signature, s.docstring,
                 s.visibility, s.is_exported, f.path AS file_path,
                 s.start_line, s.end_line, s.container_symbol
          FROM symbols s
          JOIN module_files f ON f.id = s.file_id
          WHERE s.is_exported = true OR s.visibility = 'public'
        ),
        external_refs AS (
          SELECT
            sr.target_name,
            sf.path AS consumer_path,
            sr.reference_kind,
            COUNT(*) AS ref_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          WHERE sf.path NOT LIKE $2 || '%'
            AND lower(sr.target_name) IN (SELECT lower(name) FROM module_symbols)
          GROUP BY sr.target_name, sf.path, sr.reference_kind
        ),
        external_deps AS (
          SELECT
            ts.name AS target_name,
            sf.path AS consumer_path,
            d.kind AS reference_kind,
            COUNT(*) AS ref_count
          FROM dependencies d
          JOIN files sf ON sf.id = d.source_file_id AND sf.repo = $1
          JOIN symbols ts ON ts.id = d.target_symbol_id
          JOIN module_files mf ON mf.id = ts.file_id
          WHERE sf.path NOT LIKE $2 || '%'
          GROUP BY ts.name, sf.path, d.kind
        ),
        all_external_refs AS (
          SELECT target_name, consumer_path, reference_kind, ref_count FROM external_refs
          UNION ALL
          SELECT target_name, consumer_path, reference_kind, ref_count FROM external_deps
        ),
        symbol_consumers AS (
          SELECT
            target_name,
            array_agg(DISTINCT consumer_path) AS consumer_files,
            SUM(ref_count)::integer AS total_refs,
            array_agg(DISTINCT reference_kind) AS ref_kinds
          FROM all_external_refs
          GROUP BY target_name
        )
        SELECT
          ms.name, ms.qualified_name, ms.kind, ms.signature, ms.docstring,
          ms.file_path, ms.start_line, ms.end_line, ms.visibility,
          ms.container_symbol,
          sc.consumer_files, sc.total_refs, sc.ref_kinds
        FROM module_symbols ms
        LEFT JOIN symbol_consumers sc ON lower(sc.target_name) = lower(ms.name)
        WHERE sc.target_name IS NOT NULL
           OR $3 = true
        ORDER BY
          CASE WHEN sc.target_name IS NOT NULL THEN 0 ELSE 1 END,
          sc.total_refs DESC NULLS LAST,
          ms.kind, ms.name
        `,
        [repo, path_prefix, include_unused],
      );

      const rows = result.rows as ModuleInterfaceRow[];
      if (rows.length === 0) {
        return { content: [{ type: "text", text: `No exported symbols found in \`${path_prefix}\` in repo \`${repo}\`.` }] };
      }

      const text = formatModuleInterface(path_prefix, rows, include_unused);
      return { content: [{ type: "text", text }] };
    },
  );

  server.tool(
    "find_dependency_cycles",
    "Detects circular dependency chains in the file dependency graph. Cycles are the primary obstacle to modularization. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().optional().default("")
        .describe("Optional path prefix to scope cycle detection to a subsystem."),
      max_cycle_length: z.number().optional().default(6)
        .describe("Maximum cycle length to search for (default 6, max 10)."),
    },
    async ({ repo, path_prefix, max_cycle_length }) => {
      const clampedMax = Math.min(max_cycle_length, 10);
      logToolInvocation("find_dependency_cycles", { repo, path_prefix, max_cycle_length: clampedMax });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      // Extract directed file-to-file edge list
      const result = await query(
        `
        WITH scoped_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        dep_edges AS (
          SELECT DISTINCT
            sf.path AS source,
            tf.path AS target
          FROM dependencies d
          JOIN scoped_files sf ON sf.id = d.source_file_id
          JOIN scoped_files tf ON tf.id = d.target_file_id
          WHERE sf.path <> tf.path
        ),
        ref_edges AS (
          SELECT DISTINCT
            sf.path AS source,
            tf.path AS target
          FROM symbol_references sr
          JOIN scoped_files sf ON sf.id = sr.source_file_id
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN scoped_files tf ON tf.id = s.file_id
          WHERE sf.path <> tf.path
        )
        SELECT DISTINCT source, target FROM dep_edges
        UNION
        SELECT DISTINCT source, target FROM ref_edges
        ORDER BY source, target
        `,
        [repo, path_prefix],
      );

      const edges = result.rows as GraphEdge[];
      if (edges.length === 0) {
        return { content: [{ type: "text", text: `No dependency edges found under \`${path_prefix || "(entire repo)"}\` in repo \`${repo}\`.` }] };
      }

      const cycles = findCycles(edges, clampedMax);
      if (cycles.length === 0) {
        return { content: [{ type: "text", text: `No dependency cycles found under \`${path_prefix || "(entire repo)"}\` in repo \`${repo}\`. The dependency graph is acyclic.` }] };
      }

      const rankings = rankNodesByCycleParticipation(cycles);
      const text = formatCycles(repo, path_prefix, cycles, rankings);
      return { content: [{ type: "text", text }] };
    },
  );

  server.tool(
    "find_modularization_seams",
    "Produces a comprehensive extraction plan for a subsystem: required interfaces, dependencies to inject, and cross-boundary seams to cut. Use this to plan modularizing a component so it can be replaced. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().describe("Path prefix of the module to extract (e.g. 'src/notifications/')."),
    },
    async ({ repo, path_prefix }) => {
      logToolInvocation("find_modularization_seams", { repo, path_prefix });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      // Count internal files
      const fileCountResult = await query(
        `SELECT COUNT(*) AS cnt FROM files WHERE repo = $1 AND path LIKE $2 || '%'`,
        [repo, path_prefix],
      );
      const internalFileCount = Number((fileCountResult.rows[0] as Record<string, unknown>).cnt);

      if (internalFileCount === 0) {
        return { content: [{ type: "text", text: `No files found under \`${path_prefix}\` in repo \`${repo}\`.` }] };
      }

      // Query A: Required interfaces (external code calling into this module)
      const interfaceResult = await query(
        `
        WITH module_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        module_symbols AS (
          SELECT s.id, s.name, s.qualified_name, s.kind, s.signature, s.docstring,
                 s.visibility, s.is_exported, f.path AS file_path,
                 s.start_line, s.end_line, s.container_symbol
          FROM symbols s
          JOIN module_files f ON f.id = s.file_id
        ),
        external_refs AS (
          SELECT
            sr.target_name,
            sf.path AS consumer_path,
            sr.reference_kind,
            COUNT(*) AS ref_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          WHERE sf.path NOT LIKE $2 || '%'
            AND lower(sr.target_name) IN (SELECT lower(name) FROM module_symbols)
          GROUP BY sr.target_name, sf.path, sr.reference_kind
        ),
        symbol_consumers AS (
          SELECT
            target_name,
            array_agg(DISTINCT consumer_path) AS consumer_files,
            SUM(ref_count)::integer AS total_refs,
            array_agg(DISTINCT reference_kind) AS ref_kinds
          FROM external_refs
          GROUP BY target_name
        )
        SELECT
          ms.name, ms.qualified_name, ms.kind, ms.signature, ms.docstring,
          ms.file_path, ms.start_line, ms.end_line, ms.visibility,
          ms.container_symbol,
          sc.consumer_files, sc.total_refs, sc.ref_kinds
        FROM module_symbols ms
        JOIN symbol_consumers sc ON lower(sc.target_name) = lower(ms.name)
        ORDER BY sc.total_refs DESC, ms.kind, ms.name
        `,
        [repo, path_prefix],
      );

      // Query B: Dependencies to inject (external symbols referenced inside the module)
      const depsResult = await query(
        `
        WITH module_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        )
        SELECT
          'outbound' AS direction,
          mf.path AS internal_file,
          ef.path AS external_file,
          sr.target_name AS symbol_name,
          s.kind AS symbol_kind,
          s.signature,
          sr.reference_kind,
          COUNT(*) AS usage_count
        FROM symbol_references sr
        JOIN module_files mf ON mf.id = sr.source_file_id
        JOIN symbols s ON lower(s.name) = lower(sr.target_name)
        JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
        WHERE ef.path NOT LIKE $2 || '%'
        GROUP BY mf.path, ef.path, sr.target_name, s.kind, s.signature, sr.reference_kind
        ORDER BY usage_count DESC
        `,
        [repo, path_prefix],
      );

      // Query C: All cross-boundary seam edges (both directions)
      const seamsResult = await query(
        `
        WITH module_files AS (
          SELECT id, path FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        inbound_seams AS (
          SELECT
            'inbound' AS direction,
            mf.path AS internal_file,
            sf.path AS external_file,
            sr.target_name AS symbol_name,
            s.kind AS symbol_kind,
            s.signature,
            sr.reference_kind,
            COUNT(*) AS usage_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN module_files mf ON mf.id = s.file_id
          WHERE sf.path NOT LIKE $2 || '%'
          GROUP BY mf.path, sf.path, sr.target_name, s.kind, s.signature, sr.reference_kind
        ),
        outbound_seams AS (
          SELECT
            'outbound' AS direction,
            mf.path AS internal_file,
            ef.path AS external_file,
            sr.target_name AS symbol_name,
            s.kind AS symbol_kind,
            s.signature,
            sr.reference_kind,
            COUNT(*) AS usage_count
          FROM symbol_references sr
          JOIN module_files mf ON mf.id = sr.source_file_id
          JOIN symbols s ON lower(s.name) = lower(sr.target_name)
          JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
          WHERE ef.path NOT LIKE $2 || '%'
          GROUP BY mf.path, ef.path, sr.target_name, s.kind, s.signature, sr.reference_kind
        )
        SELECT * FROM inbound_seams
        UNION ALL
        SELECT * FROM outbound_seams
        ORDER BY usage_count DESC
        `,
        [repo, path_prefix],
      );

      const requiredInterface = interfaceResult.rows as ModuleInterfaceRow[];
      const dependencies = depsResult.rows as SeamRow[];
      const seams = seamsResult.rows as SeamRow[];

      const text = formatModularizationSeams(path_prefix, requiredInterface, dependencies, seams, internalFileCount);
      return { content: [{ type: "text", text }] };
    },
  );
}
