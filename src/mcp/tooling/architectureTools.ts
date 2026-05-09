/**
 * @file src/mcp/tooling/architectureTools.ts
 * @brief MCP tools for coupling, module interfaces, cycles, external dependencies, impact, and seams.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../../db.js";
import { formatCouplingAnalysis, formatModularizationSeams, formatModuleInterface } from "../formatters.js";
import { logToolInvocation } from "../logging.js";
import type { CouplingEdgeRow, ModuleInterfaceRow, SeamRow } from "../types.js";
import {
  getFirstPartyModuleNames,
  impactCategory,
  isFirstPartyModule,
  isStdlibModule,
  requireRepository,
} from "./shared.js";
import { findCycles } from "../../repositories/store.js";

/**
 * @brief Registers architecture and refactoring analysis tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerArchitectureTools(server: McpServer): void {
  server.tool(
    "analyze_coupling",
    "Quantifies how tightly coupled a subsystem is to the rest of the codebase. Reports afferent/efferent coupling, instability metric, and top cross-boundary file pairs. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().describe("Path prefix identifying the subsystem to analyze (e.g. 'src/payments/')."),
      top_n: z.number().optional().describe("Number of top coupling pairs to return (default 20)."),
    },
    async ({ repo, path_prefix, top_n = 20 }) => {
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
          SELECT id, path, language FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        outbound AS (
          SELECT
            tf.path AS internal_path,
            COALESCE(ef.path, d.external_module, '(external)') AS external_path,
            d.kind,
            COUNT(*)::integer AS edge_count
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
            COUNT(*)::integer AS edge_count
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
            COUNT(*)::integer AS edge_count
          FROM symbol_references sr
          JOIN target_files tf ON tf.id = sr.source_file_id
          JOIN symbols s
            ON (
              (sr.target_symbol_id IS NOT NULL AND s.id = sr.target_symbol_id)
              OR (sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name))
            )
          JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
          WHERE ef.path NOT LIKE $2 || '%'
            AND (
              (COALESCE(tf.language, '') = COALESCE(ef.language, ''))
              OR (
                tf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                AND ef.language IN ('typescript', 'tsx', 'javascript', 'jsx')
              )
            )
          GROUP BY tf.path, ef.path, sr.reference_kind
        ),
        ref_inbound AS (
          SELECT
            tf.path AS internal_path,
            sf.path AS external_path,
            sr.reference_kind AS kind,
            COUNT(*)::integer AS edge_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          JOIN symbols s
            ON (
              (sr.target_symbol_id IS NOT NULL AND s.id = sr.target_symbol_id)
              OR (sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name))
            )
          JOIN target_files tf ON tf.id = s.file_id
          WHERE sf.path NOT LIKE $2 || '%'
            AND (
              (COALESCE(tf.language, '') = COALESCE(sf.language, ''))
              OR (
                tf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                AND sf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
              )
            )
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
      include_unused: z.boolean().optional()
        .describe("Also show exported symbols with no external consumers."),
    },
    async ({ repo, path_prefix, include_unused = false }) => {
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
    "find_cycles",
    "Returns persisted dependency cycles for a repository from dependency_cycles materialization. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().optional().describe("Optional path prefix to filter cycle members."),
    },
    async ({ repo, path_prefix = "" }) => {
      logToolInvocation("find_cycles", { repo, path_prefix });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      const rows = await findCycles(repo, path_prefix);

      if (rows.length === 0) {
        const scope = path_prefix ? ` under \`${path_prefix}\`` : "";
        return { content: [{ type: "text", text: `No dependency cycles found for repo \`${repo}\`${scope}.` }] };
      }

      const lines: string[] = [
        `Dependency cycles for \`${repo}\`${path_prefix ? ` (path prefix: \`${path_prefix}\`)` : ""}`,
        "",
        `Cycles found: ${rows.length}`,
        "",
      ];

      for (let i = 0; i < rows.length; i++) {
        const row = rows[i] as Record<string, unknown>;
        const memberPaths = Array.isArray(row.member_paths)
          ? row.member_paths.map((value) => String(value))
          : [];
        const memberFileIds = Array.isArray(row.member_file_ids)
          ? row.member_file_ids.map((value) => Number(value))
          : [];
        const cycleSize = Number(row.cycle_size);

        lines.push(`Cycle ${i + 1} (${cycleSize} files)`);
        for (let j = 0; j < memberPaths.length; j++) {
          const fileId = memberFileIds[j];
          const fileIdText = Number.isFinite(fileId) ? `[${fileId}] ` : "";
          lines.push(`- ${fileIdText}${memberPaths[j]}`);
        }
        lines.push("");
      }

      return { content: [{ type: "text", text: lines.join("\n").trim() }] };
    },
  );

  server.tool(
    "find_external_dependencies",
    "Summarizes third-party package usage by module and version, and optionally lists consumers for a specific package. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      path_prefix: z.string().optional().describe("Optional source-file path prefix filter."),
      package_name: z.string().optional().describe("Optional external package to inspect consumer locations for."),
      limit: z.number().int().min(1).max(500).optional()
        .describe("Maximum consumer rows returned when package_name is provided (default 100)."),
      include_stdlib: z
        .boolean()
        .optional()
        .describe("When true, include stdlib modules (python/node core) in summary and consumer output."),
    },
    async ({ repo, path_prefix = "", package_name, limit = 100, include_stdlib = false }) => {
      logToolInvocation("find_external_dependencies", { repo, path_prefix, package_name, limit, include_stdlib });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const summaryResult = await query(
        `
        WITH normalized_external_deps AS (
          SELECT
            sf.path AS source_path,
            sf.language AS source_language,
            d.external_version,
            d.kind,
            CASE
              WHEN d.external_module IS NULL OR btrim(d.external_module) = '' THEN NULL
              WHEN d.external_module ~ '^(\\./|\\.\\./|/|\\.|\\.\\.)$' THEN NULL
              WHEN d.external_module LIKE './%' OR d.external_module LIKE '../%' THEN NULL
              WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx') AND d.external_module LIKE 'node:%'
                THEN split_part(replace(d.external_module, 'node:', ''), '/', 1)
              WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx') AND d.external_module LIKE '@%/%'
                THEN split_part(d.external_module, '/', 1) || '/' || split_part(d.external_module, '/', 2)
              WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                THEN split_part(d.external_module, '/', 1)
              WHEN sf.language = 'python'
                THEN split_part(replace(d.external_module, '_', '-'), '.', 1)
              ELSE d.external_module
            END AS normalized_module
          FROM dependencies d
          JOIN files sf ON sf.id = d.source_file_id
          WHERE sf.repo = $1
            AND COALESCE(d.is_external, d.external_module IS NOT NULL)
            AND d.external_module IS NOT NULL
            AND ($2 = '' OR sf.path LIKE $2 || '%')
        )
        SELECT
          ned.normalized_module AS external_module,
          MIN(ned.source_language) AS source_language,
          COALESCE(NULLIF(ned.external_version, ''), '(unknown)') AS external_version,
          COUNT(*)::integer AS usage_count,
          COUNT(DISTINCT ned.source_path)::integer AS consumer_file_count
        FROM normalized_external_deps ned
        WHERE ned.normalized_module IS NOT NULL
        GROUP BY ned.normalized_module, COALESCE(NULLIF(ned.external_version, ''), '(unknown)')
        ORDER BY usage_count DESC, ned.normalized_module, external_version
        `,
        [repo, path_prefix],
      );

      const firstPartyModules = await getFirstPartyModuleNames(repo);
      const summaryRows = (summaryResult.rows as Array<Record<string, unknown>>).filter((row) => {
        const moduleName = String(row.external_module || "");
        if (isFirstPartyModule(firstPartyModules, moduleName)) {
          return false;
        }
        if (include_stdlib) {
          return true;
        }
        return !isStdlibModule(
          row.source_language ? String(row.source_language) : null,
          moduleName,
        );
      });

      if (summaryRows.length === 0) {
        const scope = path_prefix ? ` under \`${path_prefix}\`` : "";
        return {
          content: [{
            type: "text",
            text: `No external dependencies found for repo \`${repo}\`${scope}${include_stdlib ? "." : " (after stdlib and first-party filtering)."}`,
          }],
        };
      }

      const lines = [
        `External dependencies for \`${repo}\`${path_prefix ? ` (path prefix: \`${path_prefix}\`)` : ""}:`,
        "",
        "| Package | Version | Usage Count | Consumer Files |",
        "|---|---|---:|---:|",
      ];

      for (const row of summaryRows) {
        lines.push(
          `| ${String(row.external_module)} | ${String(row.external_version)} | ${Number(row.usage_count)} | ${Number(row.consumer_file_count)} |`,
        );
      }

      if (package_name && package_name.trim()) {
        const consumerResult = await query(
          `
          WITH normalized_external_deps AS (
            SELECT
              sf.path AS source_path,
              sf.language AS source_language,
              d.source_symbol_id,
              d.external_version,
              d.kind,
              CASE
                WHEN d.external_module IS NULL OR btrim(d.external_module) = '' THEN NULL
                WHEN d.external_module ~ '^(\\./|\\.\\./|/|\\.|\\.\\.)$' THEN NULL
                WHEN d.external_module LIKE './%' OR d.external_module LIKE '../%' THEN NULL
                WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx') AND d.external_module LIKE 'node:%'
                  THEN split_part(replace(d.external_module, 'node:', ''), '/', 1)
                WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx') AND d.external_module LIKE '@%/%'
                  THEN split_part(d.external_module, '/', 1) || '/' || split_part(d.external_module, '/', 2)
                WHEN sf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                  THEN split_part(d.external_module, '/', 1)
                WHEN sf.language = 'python'
                  THEN split_part(replace(d.external_module, '_', '-'), '.', 1)
                ELSE d.external_module
              END AS normalized_module
            FROM dependencies d
            JOIN files sf ON sf.id = d.source_file_id
            WHERE sf.repo = $1
              AND COALESCE(d.is_external, d.external_module IS NOT NULL)
              AND d.external_module IS NOT NULL
              AND ($2 = '' OR sf.path LIKE $2 || '%')
          )
          SELECT
            ned.source_path AS consumer_path,
            ned.source_language,
            COALESCE(ss.name, '(file-level import)') AS consumer_symbol,
            ned.kind,
            COALESCE(NULLIF(ned.external_version, ''), '(unknown)') AS external_version,
            COUNT(*)::integer AS usage_count
          FROM normalized_external_deps ned
          LEFT JOIN symbols ss ON ss.id = ned.source_symbol_id
          WHERE ned.normalized_module IS NOT NULL
            AND lower(ned.normalized_module) = lower($3)
          GROUP BY ned.source_path, COALESCE(ss.name, '(file-level import)'), ned.kind, COALESCE(NULLIF(ned.external_version, ''), '(unknown)')
          ORDER BY usage_count DESC, ned.source_path, consumer_symbol, ned.kind
          LIMIT $4
          `,
          [repo, path_prefix, package_name, limit],
        );

        const consumerRows = (consumerResult.rows as Array<Record<string, unknown>>).filter((row) => {
          const requestedPackage = package_name.trim().toLowerCase();
          if (isFirstPartyModule(firstPartyModules, requestedPackage)) {
            return false;
          }
          if (include_stdlib) {
            return true;
          }
          return !isStdlibModule(
            row.source_language ? String(row.source_language) : null,
            requestedPackage,
          );
        });

        lines.push("", `Consumers for package \`${package_name}\`:`, "");
        if (consumerRows.length === 0) {
          lines.push("No consumers found in the selected scope.");
        } else {
          lines.push("| Consumer File | Consumer Symbol | Kind | Version | Usage Count |");
          lines.push("|---|---|---|---|---:|");
          for (const row of consumerRows) {
            lines.push(
              `| ${String(row.consumer_path)} | ${String(row.consumer_symbol)} | ${String(row.kind)} | ${String(row.external_version)} | ${Number(row.usage_count)} |`,
            );
          }
        }
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "find_impact",
    "Answers what may break if a symbol changes by reverse-traversing confidence-scored edges. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
      symbol: z.string().min(1).describe("Symbol identifier: numeric symbol id, exact name, or qualified name."),
      depth: z.number().int().min(1).max(8).optional().describe("Maximum reverse traversal depth (default 5)."),
      min_confidence: z.number().min(0).max(1).optional()
        .describe("Traversal confidence floor (default 0.55; values below are excluded)."),
    },
    async ({ repo, symbol, depth = 5, min_confidence = 0.55 }) => {
      logToolInvocation("find_impact", { repo, symbol, depth, min_confidence });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      const numericSymbolId = Number(symbol);
      let rootSymbolQueryResult;
      if (Number.isInteger(numericSymbolId) && numericSymbolId > 0) {
        rootSymbolQueryResult = await query(
          `
          SELECT s.id, s.name, s.qualified_name, f.path
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND s.id = $2
          LIMIT 1
          `,
          [repo, numericSymbolId],
        );
      } else {
        rootSymbolQueryResult = await query(
          `
          SELECT s.id, s.name, s.qualified_name, f.path
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              s.qualified_name = $2
              OR s.name = $2
              OR s.qualified_name ILIKE '%' || $2 || '%'
              OR s.name ILIKE '%' || $2 || '%'
            )
          ORDER BY
            CASE
              WHEN s.qualified_name = $2 THEN 0
              WHEN s.name = $2 THEN 1
              ELSE 2
            END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            CASE WHEN s.is_exported THEN 0 ELSE 1 END,
            f.path,
            s.start_line
          LIMIT 1
          `,
          [repo, symbol],
        );
      }

      if (rootSymbolQueryResult.rows.length === 0) {
        return { content: [{ type: "text", text: `No symbol match found for \`${symbol}\` in repo \`${repo}\`.` }] };
      }

      const rootRow = rootSymbolQueryResult.rows[0] as Record<string, unknown>;
      const rootSymbolId = Number(rootRow.id);
      const rootSymbolName = String(rootRow.name);
      const rootQualifiedName = rootRow.qualified_name ? String(rootRow.qualified_name) : rootSymbolName;
      const rootPath = String(rootRow.path);

      const impactResult = await query(
        `
        SELECT
          affected_symbol_id,
          affected_file_id,
          affected_file_path,
          affected_symbol_name,
          depth,
          edge_kind,
          path_min_confidence
        FROM impact_of($1, $2, $3)
        `,
        [rootSymbolId, depth, min_confidence],
      );

      if (impactResult.rows.length === 0) {
        return {
          content: [{
            type: "text",
            text: `No impacted symbols found for \`${rootQualifiedName}\` (${rootPath}) with depth=${depth} and min_confidence=${min_confidence.toFixed(2)}.`,
          }],
        };
      }

      type ImpactRow = {
        affected_symbol_id: number;
        affected_file_id: number;
        affected_file_path: string;
        affected_symbol_name: string;
        depth: number;
        edge_kind: string;
        path_min_confidence: number;
      };

      const rows = impactResult.rows.map((row) => {
        const raw = row as Record<string, unknown>;
        return {
          affected_symbol_id: Number(raw.affected_symbol_id),
          affected_file_id: Number(raw.affected_file_id),
          affected_file_path: String(raw.affected_file_path),
          affected_symbol_name: String(raw.affected_symbol_name),
          depth: Number(raw.depth),
          edge_kind: String(raw.edge_kind || "unknown"),
          path_min_confidence: Number(raw.path_min_confidence || 0),
        } satisfies ImpactRow;
      });

      const likelyImpact = rows.filter((row) => row.path_min_confidence >= 0.75);
      const possibleImpact = rows.filter((row) => row.path_min_confidence >= min_confidence && row.path_min_confidence < 0.75);

      const formatBand = (title: string, bandRows: ImpactRow[]): string[] => {
        if (bandRows.length === 0) {
          return [`${title}: none`, ""];
        }
        type ImpactAggregate = ImpactRow & { occurrence_count: number };
        const dedupedRows = new Map<string, ImpactAggregate>();
        for (const row of bandRows) {
          const key = [
            row.affected_symbol_id,
            row.depth,
            row.edge_kind,
            row.path_min_confidence.toFixed(6),
          ].join("|");
          const existing = dedupedRows.get(key);
          if (existing) {
            existing.occurrence_count += 1;
          } else {
            dedupedRows.set(key, { ...row, occurrence_count: 1 });
          }
        }

        const distinctRows = Array.from(dedupedRows.values()).sort((a, b) =>
          b.occurrence_count - a.occurrence_count
          || b.path_min_confidence - a.path_min_confidence
          || a.affected_file_path.localeCompare(b.affected_file_path)
          || a.affected_symbol_name.localeCompare(b.affected_symbol_name)
          || a.depth - b.depth,
        );

        const grouped = new Map<string, ImpactAggregate[]>();
        for (const row of distinctRows) {
          const category = impactCategory(row.edge_kind);
          if (!grouped.has(category)) {
            grouped.set(category, []);
          }
          grouped.get(category)!.push(row);
        }

        const lines: string[] = [`${title}: ${distinctRows.length} distinct targets (${bandRows.length} edge occurrences)`];
        for (const category of ["calls", "instantiations", "structural", "imports", "other"]) {
          const categoryRows = grouped.get(category);
          if (!categoryRows || categoryRows.length === 0) {
            continue;
          }
          lines.push(`- ${category}:`);
          for (const row of categoryRows.slice(0, 60)) {
            const occurrenceCount = row.occurrence_count > 1 ? `, occurrences ${row.occurrence_count}` : "";
            lines.push(
              `  - [symbol ${row.affected_symbol_id}] ${row.affected_symbol_name} (${row.affected_file_path}, depth ${row.depth}, edge ${row.edge_kind}, confidence ${row.path_min_confidence.toFixed(2)}${occurrenceCount})`,
            );
          }
          if (categoryRows.length > 60) {
            lines.push(`  - ... ${categoryRows.length - 60} more`);
          }
        }
        lines.push("");
        return lines;
      };

      const lines = [
        `Impact analysis for \`${rootQualifiedName}\` (symbol ${rootSymbolId}, ${rootPath})`,
        `Traversal depth: ${depth}`,
        `min_confidence: ${min_confidence.toFixed(2)} (default 0.55)`,
        "",
        ...formatBand("Likely impact (confidence >= 0.75)", likelyImpact),
        ...formatBand("Possible impact (0.55 <= confidence < 0.75)", possibleImpact),
      ];

      return { content: [{ type: "text", text: lines.join("\n").trim() }] };
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
          SELECT id, path, language FROM files
          WHERE repo = $1 AND path LIKE $2 || '%'
        ),
        module_symbols AS (
          SELECT s.id, s.name, s.qualified_name, s.kind, s.signature, s.docstring,
                 s.visibility, s.is_exported, f.path AS file_path,
                 s.start_line, s.end_line, s.container_symbol, f.language AS file_language
          FROM symbols s
          JOIN module_files f ON f.id = s.file_id
        ),
        external_refs AS (
          SELECT
            ms_target.name AS target_name,
            sf.path AS consumer_path,
            sr.reference_kind,
            COUNT(*)::integer AS ref_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          JOIN module_symbols ms_target
            ON (
              (sr.target_symbol_id IS NOT NULL AND sr.target_symbol_id = ms_target.id)
              OR (sr.target_symbol_id IS NULL AND lower(sr.target_name) = lower(ms_target.name))
            )
          WHERE sf.path NOT LIKE $2 || '%'
            AND (
              (COALESCE(sf.language, '') = COALESCE(ms_target.file_language, ''))
              OR (
                sf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                AND ms_target.file_language IN ('typescript', 'tsx', 'javascript', 'jsx')
              )
            )
          GROUP BY ms_target.name, sf.path, sr.reference_kind
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
          SELECT id, path, language FROM files
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
          COUNT(*)::integer AS usage_count
        FROM symbol_references sr
        JOIN module_files mf ON mf.id = sr.source_file_id
        JOIN symbols s
          ON (
            (sr.target_symbol_id IS NOT NULL AND s.id = sr.target_symbol_id)
            OR (sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name))
          )
        JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
        WHERE ef.path NOT LIKE $2 || '%'
          AND (
            (COALESCE(mf.language, '') = COALESCE(ef.language, ''))
            OR (
              mf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
              AND ef.language IN ('typescript', 'tsx', 'javascript', 'jsx')
            )
          )
        GROUP BY mf.path, ef.path, sr.target_name, s.kind, s.signature, sr.reference_kind
        ORDER BY usage_count DESC
        `,
        [repo, path_prefix],
      );

      // Query C: All cross-boundary seam edges (both directions)
      const seamsResult = await query(
        `
        WITH module_files AS (
          SELECT id, path, language FROM files
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
            COUNT(*)::integer AS usage_count
          FROM symbol_references sr
          JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
          JOIN symbols s
            ON (
              (sr.target_symbol_id IS NOT NULL AND s.id = sr.target_symbol_id)
              OR (sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name))
            )
          JOIN module_files mf ON mf.id = s.file_id
          WHERE sf.path NOT LIKE $2 || '%'
            AND (
              (COALESCE(mf.language, '') = COALESCE(sf.language, ''))
              OR (
                mf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                AND sf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
              )
            )
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
            COUNT(*)::integer AS usage_count
          FROM symbol_references sr
          JOIN module_files mf ON mf.id = sr.source_file_id
          JOIN symbols s
            ON (
              (sr.target_symbol_id IS NOT NULL AND s.id = sr.target_symbol_id)
              OR (sr.target_symbol_id IS NULL AND lower(s.name) = lower(sr.target_name))
            )
          JOIN files ef ON ef.id = s.file_id AND ef.repo = $1
          WHERE ef.path NOT LIKE $2 || '%'
            AND (
              (COALESCE(mf.language, '') = COALESCE(ef.language, ''))
              OR (
                mf.language IN ('typescript', 'tsx', 'javascript', 'jsx')
                AND ef.language IN ('typescript', 'tsx', 'javascript', 'jsx')
              )
            )
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

  /* ------------------------------------------------------------------ */
  /*  Index management tools                                             */
  /* ------------------------------------------------------------------ */

}
