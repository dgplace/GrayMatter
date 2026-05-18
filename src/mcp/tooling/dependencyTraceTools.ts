/**
 * @file src/mcp/tooling/dependencyTraceTools.ts
 * @brief MCP tools for dependency tracing and repository/file intent overviews.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../../db.js";
import { getRepositoryStats } from "../../repositories/store.js";
import { logToolInvocation } from "../logging.js";
import { buildPathPrefixHint, nextStepFooter, repoNotFoundText, requireRepository } from "./shared.js";

/**
 * @brief Registers dependency tracing and file-map/intent tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerDependencyTraceTools(server: McpServer): void {
  server.tool(
    "trace_dependencies",
    "Use for cross-file dependency walks (inbound/outbound/both) up to `max_depth`. For symbol-level call relationships, use `find_call_graph` instead.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path: z.string().describe("File path or distinctive partial path to trace."),
      direction: z
        .enum(["inbound", "outbound", "both"])
        .optional()
        .describe("Use inbound for reverse dependencies, outbound for direct dependencies, both for a quick graph walk."),
      max_depth: z.number().optional().describe("Depth limit for the graph walk (default 3)."),
      summary: z.boolean().optional().describe("When true, returns aggregated counts by file and kind instead of individual edges."),
    },
    async ({ repo, path, direction = "both", max_depth = 3, summary = false }) => {
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
            COALESCE(
              CASE
                WHEN resolved_file.id IS NOT NULL
                  AND (
                    (COALESCE(source_file.language, '') = COALESCE(resolved_file.language, ''))
                    OR (
                      source_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                      AND resolved_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                    )
                  )
                THEN resolved_file.id
                ELSE NULL
              END,
              fallback_file.id
            ) AS target_file_id,
            COALESCE(sr.reference_kind_v2, sr.reference_kind) AS dep_kind,
            sr.source_symbol_name AS source_symbol,
            COALESCE(
              CASE
                WHEN resolved_file.id IS NOT NULL
                  AND (
                    (COALESCE(source_file.language, '') = COALESCE(resolved_file.language, ''))
                    OR (
                      source_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                      AND resolved_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                    )
                  )
                THEN resolved_symbol.name
                ELSE NULL
              END,
              fallback_symbol.name
            ) AS target_symbol,
            NULL::text AS external_module
          FROM symbol_references sr
          JOIN files source_file ON source_file.id = sr.source_file_id AND source_file.repo = $2
          LEFT JOIN symbols resolved_symbol ON resolved_symbol.id = sr.target_symbol_id
          LEFT JOIN files resolved_file ON resolved_file.id = resolved_symbol.file_id AND resolved_file.repo = $2
          LEFT JOIN LATERAL (
            SELECT s.id, s.file_id, s.name
            FROM symbols s
            JOIN files tf ON tf.id = s.file_id AND tf.repo = $2
            WHERE lower(s.name) = lower(sr.target_name)
              AND (
                (COALESCE(source_file.language, '') = COALESCE(tf.language, ''))
                OR (
                  source_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                  AND tf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                )
              )
              AND (
                sr.target_symbol_id IS NULL
                OR resolved_file.id IS NULL
                OR NOT (
                  (COALESCE(source_file.language, '') = COALESCE(resolved_file.language, ''))
                  OR (
                    source_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                    AND resolved_file.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                  )
                )
              )
            ORDER BY
              CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
              CASE WHEN s.declared_in_extension THEN 1 ELSE 0 END,
              s.is_exported DESC,
              s.start_line
            LIMIT 1
          ) fallback_symbol ON TRUE
          LEFT JOIN files fallback_file ON fallback_file.id = fallback_symbol.file_id AND fallback_file.repo = $2
          WHERE COALESCE(resolved_file.id, fallback_file.id) IS NOT NULL
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
              AND e.target_file_id IN (SELECT id FROM target_files)
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
        ),
        dedup_rows AS (
          SELECT DISTINCT
            source_path,
            target_path_out,
            dep_kind,
            source_symbol,
            target_symbol,
            external_module,
            depth
          FROM dep_tree
        )
        SELECT
          source_path,
          target_path_out,
          dep_kind,
          source_symbol,
          target_symbol,
          external_module,
          depth
        FROM dedup_rows
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
    "FALLBACK orientation tool -- use ONLY after `find_symbol` and `semantic_search` returned nothing useful. File map is for mapping the territory, not for discovering code. Call with NO `path_prefix` first to see real top-level dirs; do not guess `src/`. NEXT STEP after this returns a relevant class/method name: call `exact_symbol_search` or `describe_node` on it -- do NOT jump to Grep/Read.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path_prefix: z.string().optional().describe("Directory or path prefix to inspect."),
    },
    async ({ repo, path_prefix = "" }) => {
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
        const hint = await buildPathPrefixHint(repo, path_prefix);
        return { content: [{ type: "text", text: hint }] };
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

      return { content: [{ type: "text", text: formatted + nextStepFooter("get_file_map") }] };
    },
  );

  server.tool(
    "get_intent",
    "Use before editing a file you haven't read -- returns the file summary plus per-chunk intent labels and line ranges.",
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
    "Use for repo-wide totals -- file/line counts, language mix, intent distribution, symbol-kind counts. Diagnostic, not for code lookup.",
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

      output += "\n### Callback Extractor Gaps\n";
      if (stats.frameworkDiagnostics.length === 0) {
        output += "- none detected\n";
      } else {
        for (const diagnostic of stats.frameworkDiagnostics) {
          output += `- ${diagnostic.framework}: missing extractor (${diagnostic.affectedFileCount} files)\n`;
        }
      }

      return { content: [{ type: "text", text: output }] };
    },
  );

  /* ------------------------------------------------------------------ */
  /*  Refactoring analysis tools                                        */
  /* ------------------------------------------------------------------ */

}
