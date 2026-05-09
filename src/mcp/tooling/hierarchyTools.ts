/**
 * @file src/mcp/tooling/hierarchyTools.ts
 * @brief MCP tools for hierarchy, implementation, call graph, and instantiation traversal.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../../db.js";
import { logToolInvocation } from "../logging.js";
import { formatRelationshipTarget, formatSymbolLocator, requireRepository } from "./shared.js";

/**
 * @brief Registers hierarchy and call-graph traversal tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerHierarchyTools(server: McpServer): void {
  server.tool(
    "find_supertypes",
    "Returns transitive parent types/interfaces for a symbol by walking symbol_relationships where kind is extends/implements. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      symbol: z.string().describe("Exact symbol name or qualified suffix to inspect."),
      max_depth: z.number().optional().describe("Depth limit for the transitive walk (default 4)."),
    },
    async ({ repo, symbol, max_depth = 4 }) => {
      logToolInvocation("find_supertypes", { repo, symbol, max_depth });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        WITH RECURSIVE start_symbols AS (
          SELECT
            s.id,
            s.name,
            f.path AS file_path,
            s.start_line,
            s.end_line
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              lower(s.name) = lower($2)
              OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
            )
          ORDER BY
            CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            s.start_line
          LIMIT 25
        ),
        supertype_tree AS (
          SELECT
            ss.id AS root_symbol_id,
            ss.name AS root_symbol_name,
            ss.file_path AS root_symbol_path,
            ss.start_line AS root_symbol_start_line,
            ss.end_line AS root_symbol_end_line,
            sr.source_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            1 AS depth,
            ARRAY[ss.id, COALESCE(sr.target_symbol_id, -1)]::int[] AS walk_path
          FROM start_symbols ss
          JOIN symbol_relationships sr ON sr.source_symbol_id = ss.id
          WHERE sr.relationship_kind IN ('extends', 'implements')

          UNION ALL

          SELECT
            st.root_symbol_id,
            st.root_symbol_name,
            st.root_symbol_path,
            st.root_symbol_start_line,
            st.root_symbol_end_line,
            sr.source_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            st.depth + 1 AS depth,
            st.walk_path || COALESCE(sr.target_symbol_id, -1)
          FROM supertype_tree st
          JOIN symbol_relationships sr ON sr.source_symbol_id = st.target_symbol_id
          WHERE st.depth < $3
            AND st.target_symbol_id IS NOT NULL
            AND sr.relationship_kind IN ('extends', 'implements')
            AND NOT COALESCE(sr.target_symbol_id, -1) = ANY(st.walk_path)
        )
        SELECT DISTINCT
          st.root_symbol_id,
          st.root_symbol_name,
          st.root_symbol_path,
          st.root_symbol_start_line,
          st.root_symbol_end_line,
          st.relationship_kind,
          st.target_name,
          st.external_module,
          st.depth,
          tf.path AS target_path,
          ts.start_line AS target_start_line,
          ts.end_line AS target_end_line
        FROM supertype_tree st
        LEFT JOIN symbols ts ON ts.id = st.target_symbol_id
        LEFT JOIN files tf ON tf.id = ts.file_id AND tf.repo = $1
        ORDER BY st.root_symbol_name, st.depth, st.relationship_kind, st.target_name
      `,
        [repo, symbol, max_depth],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No supertypes found for "${symbol}" in repo \`${repo}\`.` }] };
      }

      const lines = [`Supertypes for \`${symbol}\` in repo \`${repo}\`:`, ""];
      let currentRoot = "";
      for (const row of result.rows as Array<Record<string, unknown>>) {
        const rootLabel = formatSymbolLocator(row);
        if (rootLabel !== currentRoot) {
          if (currentRoot) {
            lines.push("");
          }
          lines.push(`- Root: ${rootLabel}`);
          currentRoot = rootLabel;
        }
        const depth = Number(row.depth);
        lines.push(`  depth ${depth} [${String(row.relationship_kind)}] -> ${formatRelationshipTarget(row)}`);
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "find_call_graph",
    "Returns a bounded forward (callees) or reverse (callers) call graph for a symbol using resolved target_symbol_id edges. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      symbol: z.string().describe("Exact symbol name or qualified suffix to inspect."),
      direction: z.enum(["forward", "reverse"]).optional().describe("Traversal direction: forward for callees, reverse for callers."),
      depth: z.number().int().min(1).max(8).optional().describe("Maximum graph depth to traverse (default 3)."),
    },
    async ({ repo, symbol, direction = "forward", depth = 3 }) => {
      logToolInvocation("find_call_graph", { repo, symbol, direction, depth });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const graphSql = direction === "forward"
        ? `
        WITH RECURSIVE start_symbols AS (
          SELECT
            s.id,
            s.name,
            f.path AS file_path,
            s.start_line,
            s.end_line
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              lower(s.name) = lower($2)
              OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
            )
          ORDER BY
            CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            s.start_line
          LIMIT 25
        ),
        resolved_call_edges AS (
          SELECT
            source_symbol.id AS from_symbol_id,
            sr.target_symbol_id AS to_symbol_id,
            COALESCE(sr.reference_kind_v2, sr.reference_kind) AS reference_kind,
            sr.line_no
          FROM symbol_references sr
          JOIN files source_file ON source_file.id = sr.source_file_id AND source_file.repo = $1
          LEFT JOIN LATERAL (
            SELECT s.id, s.start_line
            FROM symbols s
            WHERE s.file_id = sr.source_file_id
              AND (
                (sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name))
                OR (sr.source_symbol_name IS NULL AND s.start_line <= sr.line_no AND s.end_line >= sr.line_no)
              )
            ORDER BY
              CASE
                WHEN sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name) THEN 0
                ELSE 1
              END,
              CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
              ABS(s.start_line - sr.line_no)
            LIMIT 1
          ) source_symbol ON TRUE
          WHERE source_symbol.id IS NOT NULL
            AND sr.target_symbol_id IS NOT NULL
            AND COALESCE(sr.reference_kind_v2, sr.reference_kind) IN ('call', 'member_call', 'instantiation')
        ),
        call_tree AS (
          SELECT
            ss.id AS root_symbol_id,
            ss.name AS root_symbol_name,
            ss.file_path AS root_symbol_path,
            ss.start_line AS root_symbol_start_line,
            ss.end_line AS root_symbol_end_line,
            rce.from_symbol_id,
            rce.to_symbol_id,
            rce.reference_kind,
            rce.line_no,
            1 AS depth,
            ARRAY[ss.id, rce.to_symbol_id]::int[] AS walk_path
          FROM start_symbols ss
          JOIN resolved_call_edges rce ON rce.from_symbol_id = ss.id

          UNION ALL

          SELECT
            ct.root_symbol_id,
            ct.root_symbol_name,
            ct.root_symbol_path,
            ct.root_symbol_start_line,
            ct.root_symbol_end_line,
            rce.from_symbol_id,
            rce.to_symbol_id,
            rce.reference_kind,
            rce.line_no,
            ct.depth + 1 AS depth,
            ct.walk_path || rce.to_symbol_id
          FROM call_tree ct
          JOIN resolved_call_edges rce ON rce.from_symbol_id = ct.to_symbol_id
          WHERE ct.depth < $3
            AND NOT rce.to_symbol_id = ANY(ct.walk_path)
        )
        SELECT DISTINCT
          ct.root_symbol_id,
          ct.root_symbol_name,
          ct.root_symbol_path,
          ct.root_symbol_start_line,
          ct.root_symbol_end_line,
          ct.depth,
          ct.reference_kind,
          ct.line_no,
          from_symbol.name AS from_symbol_name,
          from_file.path AS from_path,
          from_symbol.start_line AS from_start_line,
          from_symbol.end_line AS from_end_line,
          to_symbol.name AS to_symbol_name,
          to_file.path AS to_path,
          to_symbol.start_line AS to_start_line,
          to_symbol.end_line AS to_end_line
        FROM call_tree ct
        JOIN symbols from_symbol ON from_symbol.id = ct.from_symbol_id
        JOIN files from_file ON from_file.id = from_symbol.file_id AND from_file.repo = $1
        JOIN symbols to_symbol ON to_symbol.id = ct.to_symbol_id
        JOIN files to_file ON to_file.id = to_symbol.file_id AND to_file.repo = $1
        ORDER BY ct.root_symbol_name, ct.depth, from_file.path, ct.line_no, to_file.path
        `
        : `
        WITH RECURSIVE start_symbols AS (
          SELECT
            s.id,
            s.name,
            f.path AS file_path,
            s.start_line,
            s.end_line
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              lower(s.name) = lower($2)
              OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
            )
          ORDER BY
            CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            s.start_line
          LIMIT 25
        ),
        resolved_call_edges AS (
          SELECT
            source_symbol.id AS from_symbol_id,
            sr.target_symbol_id AS to_symbol_id,
            COALESCE(sr.reference_kind_v2, sr.reference_kind) AS reference_kind,
            sr.line_no
          FROM symbol_references sr
          JOIN files source_file ON source_file.id = sr.source_file_id AND source_file.repo = $1
          LEFT JOIN LATERAL (
            SELECT s.id, s.start_line
            FROM symbols s
            WHERE s.file_id = sr.source_file_id
              AND (
                (sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name))
                OR (sr.source_symbol_name IS NULL AND s.start_line <= sr.line_no AND s.end_line >= sr.line_no)
              )
            ORDER BY
              CASE
                WHEN sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name) THEN 0
                ELSE 1
              END,
              CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
              ABS(s.start_line - sr.line_no)
            LIMIT 1
          ) source_symbol ON TRUE
          WHERE source_symbol.id IS NOT NULL
            AND sr.target_symbol_id IS NOT NULL
            AND COALESCE(sr.reference_kind_v2, sr.reference_kind) IN ('call', 'member_call', 'instantiation')
        ),
        call_tree AS (
          SELECT
            ss.id AS root_symbol_id,
            ss.name AS root_symbol_name,
            ss.file_path AS root_symbol_path,
            ss.start_line AS root_symbol_start_line,
            ss.end_line AS root_symbol_end_line,
            rce.from_symbol_id,
            rce.to_symbol_id,
            rce.reference_kind,
            rce.line_no,
            1 AS depth,
            ARRAY[ss.id, rce.from_symbol_id]::int[] AS walk_path
          FROM start_symbols ss
          JOIN resolved_call_edges rce ON rce.to_symbol_id = ss.id

          UNION ALL

          SELECT
            ct.root_symbol_id,
            ct.root_symbol_name,
            ct.root_symbol_path,
            ct.root_symbol_start_line,
            ct.root_symbol_end_line,
            rce.from_symbol_id,
            rce.to_symbol_id,
            rce.reference_kind,
            rce.line_no,
            ct.depth + 1 AS depth,
            ct.walk_path || rce.from_symbol_id
          FROM call_tree ct
          JOIN resolved_call_edges rce ON rce.to_symbol_id = ct.from_symbol_id
          WHERE ct.depth < $3
            AND NOT rce.from_symbol_id = ANY(ct.walk_path)
        )
        SELECT DISTINCT
          ct.root_symbol_id,
          ct.root_symbol_name,
          ct.root_symbol_path,
          ct.root_symbol_start_line,
          ct.root_symbol_end_line,
          ct.depth,
          ct.reference_kind,
          ct.line_no,
          from_symbol.name AS from_symbol_name,
          from_file.path AS from_path,
          from_symbol.start_line AS from_start_line,
          from_symbol.end_line AS from_end_line,
          to_symbol.name AS to_symbol_name,
          to_file.path AS to_path,
          to_symbol.start_line AS to_start_line,
          to_symbol.end_line AS to_end_line
        FROM call_tree ct
        JOIN symbols from_symbol ON from_symbol.id = ct.from_symbol_id
        JOIN files from_file ON from_file.id = from_symbol.file_id AND from_file.repo = $1
        JOIN symbols to_symbol ON to_symbol.id = ct.to_symbol_id
        JOIN files to_file ON to_file.id = to_symbol.file_id AND to_file.repo = $1
        ORDER BY ct.root_symbol_name, ct.depth, from_file.path, ct.line_no, to_file.path
        `;

      const result = await query(graphSql, [repo, symbol, depth]);
      if (result.rows.length === 0) {
        return {
          content: [{ type: "text", text: `No ${direction === "forward" ? "callees" : "callers"} found for "${symbol}" in repo \`${repo}\`.` }],
        };
      }

      const lines = [
        `Call graph (${direction}) for \`${symbol}\` in repo \`${repo}\` (depth <= ${depth}):`,
        "",
      ];
      let currentRoot = "";

      for (const row of result.rows as Array<Record<string, unknown>>) {
        const rootLabel = formatSymbolLocator(row);
        if (rootLabel !== currentRoot) {
          if (currentRoot) {
            lines.push("");
          }
          lines.push(`- Root: ${rootLabel}`);
          currentRoot = rootLabel;
        }

        const edgeDepth = Number(row.depth);
        const fromName = String(row.from_symbol_name || "unknown");
        const fromPath = String(row.from_path || "unknown");
        const fromStart = Number(row.from_start_line || 0);
        const fromEnd = Number(row.from_end_line || 0);
        const toName = String(row.to_symbol_name || "unknown");
        const toPath = String(row.to_path || "unknown");
        const toStart = Number(row.to_start_line || 0);
        const toEnd = Number(row.to_end_line || 0);
        const edgeLine = Number(row.line_no || 0);
        const kind = String(row.reference_kind || "call");
        lines.push(
          `  depth ${edgeDepth} ${fromName} (${fromPath}:${fromStart}-${fromEnd}) -> ${toName} (${toPath}:${toStart}-${toEnd}) [${kind}] @L${edgeLine}`,
        );
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "find_subtypes",
    "Returns transitive child types/interfaces for a symbol by walking symbol_relationships where kind is extends/implements. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      symbol: z.string().describe("Exact symbol name or qualified suffix to inspect."),
      max_depth: z.number().optional().describe("Depth limit for the transitive walk (default 4)."),
    },
    async ({ repo, symbol, max_depth = 4 }) => {
      logToolInvocation("find_subtypes", { repo, symbol, max_depth });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        WITH RECURSIVE start_symbols AS (
          SELECT
            s.id,
            s.name,
            f.path AS file_path,
            s.start_line,
            s.end_line
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              lower(s.name) = lower($2)
              OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
            )
          ORDER BY
            CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            s.start_line
          LIMIT 25
        ),
        root_targets AS (
          SELECT
            ss.id AS root_symbol_id,
            ss.name AS root_symbol_name,
            ss.file_path AS root_symbol_path,
            ss.start_line AS root_symbol_start_line,
            ss.end_line AS root_symbol_end_line
          FROM start_symbols ss
          UNION ALL
          SELECT
            NULL::int AS root_symbol_id,
            $2::text AS root_symbol_name,
            NULL::text AS root_symbol_path,
            NULL::int AS root_symbol_start_line,
            NULL::int AS root_symbol_end_line
          WHERE NOT EXISTS (SELECT 1 FROM start_symbols)
        ),
        subtype_tree AS (
          SELECT
            rt.root_symbol_id,
            rt.root_symbol_name,
            rt.root_symbol_path,
            rt.root_symbol_start_line,
            rt.root_symbol_end_line,
            sr.source_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            1 AS depth,
            ARRAY[COALESCE(rt.root_symbol_id, -1), sr.source_symbol_id]::int[] AS walk_path
          FROM root_targets rt
          JOIN symbol_relationships sr
            ON sr.relationship_kind IN ('extends', 'implements')
           AND (
             (rt.root_symbol_id IS NOT NULL AND sr.target_symbol_id = rt.root_symbol_id)
             OR (sr.target_symbol_id IS NULL
                 AND lower(sr.target_name) = lower(rt.root_symbol_name))
           )

          UNION ALL

          SELECT
            st.root_symbol_id,
            st.root_symbol_name,
            st.root_symbol_path,
            st.root_symbol_start_line,
            st.root_symbol_end_line,
            sr.source_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            st.depth + 1 AS depth,
            st.walk_path || sr.source_symbol_id
          FROM subtype_tree st
          JOIN symbol_relationships sr
            ON sr.target_symbol_id = st.source_symbol_id
           AND sr.relationship_kind IN ('extends', 'implements')
          WHERE st.depth < $3
            AND NOT sr.source_symbol_id = ANY(st.walk_path)
        )
        SELECT DISTINCT
          st.root_symbol_id,
          st.root_symbol_name,
          st.root_symbol_path,
          st.root_symbol_start_line,
          st.root_symbol_end_line,
          st.relationship_kind,
          st.target_name,
          st.external_module,
          st.depth,
          child.name AS child_symbol_name,
          cf.path AS child_path,
          child.start_line AS child_start_line,
          child.end_line AS child_end_line
        FROM subtype_tree st
        JOIN symbols child ON child.id = st.source_symbol_id
        JOIN files cf ON cf.id = child.file_id AND cf.repo = $1
        ORDER BY st.root_symbol_name, st.depth, child.name, cf.path
      `,
        [repo, symbol, max_depth],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No subtypes found for "${symbol}" in repo \`${repo}\`.` }] };
      }

      const lines = [`Subtypes for \`${symbol}\` in repo \`${repo}\`:`, ""];
      let currentRoot = "";
      for (const row of result.rows as Array<Record<string, unknown>>) {
        const rootLabel = formatSymbolLocator(row);
        if (rootLabel !== currentRoot) {
          if (currentRoot) {
            lines.push("");
          }
          lines.push(`- Root: ${rootLabel}`);
          currentRoot = rootLabel;
        }
        const depth = Number(row.depth);
        const childName = String(row.child_symbol_name || "unknown");
        const childPath = String(row.child_path || "unknown");
        const childStart = Number(row.child_start_line || 0);
        const childEnd = Number(row.child_end_line || 0);
        lines.push(
          `  depth ${depth} [${String(row.relationship_kind)}] <- ${childName} (${childPath}:${childStart}-${childEnd})`,
        );
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "find_instantiations",
    "Returns all instantiation sites for a class symbol by filtering reference_kind_v2='instantiation'. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      symbol: z.string().describe("Class symbol name or qualified suffix to inspect."),
      limit: z.number().int().min(1).max(200).optional().describe("Maximum number of instantiation rows to return (default 100)."),
    },
    async ({ repo, symbol, limit = 100 }) => {
      logToolInvocation("find_instantiations", { repo, symbol, limit });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const classResult = await query(
        `
        SELECT
          s.id,
          s.name,
          s.kind,
          f.path AS class_path,
          s.start_line,
          s.end_line
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE f.repo = $1
          AND (
            lower(s.name) = lower($2)
            OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
          )
          AND s.kind IN ('class', 'struct')
        ORDER BY
          CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
          CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
          s.start_line
        LIMIT 25
      `,
        [repo, symbol],
      );

      if (classResult.rows.length === 0) {
        return {
          content: [{ type: "text", text: `No class symbol matches found for "${symbol}" in repo \`${repo}\`. Returning 0 instantiations.` }],
        };
      }

      const classIds = (classResult.rows as Array<Record<string, unknown>>).map((row) => Number(row.id));
      const instantiationResult = await query(
        `
        SELECT
          sr.target_symbol_id,
          tc.name AS class_name,
          tcf.path AS class_path,
          tc.start_line AS class_start_line,
          tc.end_line AS class_end_line,
          sf.path AS source_path,
          sr.line_no,
          ss.name AS containing_symbol_name,
          ss.start_line AS containing_symbol_start_line,
          ss.end_line AS containing_symbol_end_line
        FROM symbol_references sr
        JOIN symbols tc ON tc.id = sr.target_symbol_id
        JOIN files tcf ON tcf.id = tc.file_id AND tcf.repo = $1
        JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = $1
        LEFT JOIN LATERAL (
          SELECT s.name, s.start_line, s.end_line
          FROM symbols s
          WHERE s.file_id = sr.source_file_id
            AND (
              (sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name))
              OR (sr.source_symbol_name IS NULL AND s.start_line <= sr.line_no AND s.end_line >= sr.line_no)
            )
          ORDER BY
            CASE
              WHEN sr.source_symbol_name IS NOT NULL AND lower(s.name) = lower(sr.source_symbol_name) THEN 0
              ELSE 1
            END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            ABS(s.start_line - sr.line_no)
          LIMIT 1
        ) ss ON TRUE
        WHERE sr.target_symbol_id = ANY($2::int[])
          AND COALESCE(sr.reference_kind_v2, sr.reference_kind) = 'instantiation'
        ORDER BY tc.name, sf.path, sr.line_no
        LIMIT $3
      `,
        [repo, classIds, limit],
      );

      if (instantiationResult.rows.length === 0) {
        return {
          content: [{ type: "text", text: `No instantiations found for "${symbol}" in repo \`${repo}\`.` }],
        };
      }

      const lines = [`Instantiations for \`${symbol}\` in repo \`${repo}\`:`, ""];
      for (const row of instantiationResult.rows as Array<Record<string, unknown>>) {
        const className = String(row.class_name || "unknown");
        const classPath = String(row.class_path || "unknown");
        const classStart = Number(row.class_start_line || 0);
        const classEnd = Number(row.class_end_line || 0);
        const sourcePath = String(row.source_path || "unknown");
        const lineNo = Number(row.line_no || 0);
        const containerName = String(row.containing_symbol_name || "unknown");
        const containerStart = Number(row.containing_symbol_start_line || 0);
        const containerEnd = Number(row.containing_symbol_end_line || 0);

        lines.push(
          `- ${sourcePath}:${lineNo} in ${containerName} (${containerStart}-${containerEnd}) instantiates ${className} (${classPath}:${classStart}-${classEnd})`,
        );
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "find_implementations",
    "Returns direct and transitive implementers/subclasses for a symbol by walking symbol_relationships with kind=implements/extends. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      symbol: z.string().describe("Interface or abstract symbol name to inspect."),
      max_depth: z.number().optional().describe("Depth limit for the transitive walk (default 4)."),
    },
    async ({ repo, symbol, max_depth = 4 }) => {
      logToolInvocation("find_implementations", { repo, symbol, max_depth });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        WITH RECURSIVE start_symbols AS (
          SELECT
            s.id,
            s.name,
            f.path AS file_path,
            s.start_line,
            s.end_line
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              lower(s.name) = lower($2)
              OR COALESCE(s.qualified_name, '') ILIKE '%' || $2
            )
          ORDER BY
            CASE WHEN lower(s.name) = lower($2) THEN 0 ELSE 1 END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            s.start_line
          LIMIT 25
        ),
        root_targets AS (
          SELECT
            ss.id AS root_symbol_id,
            ss.name AS root_symbol_name,
            ss.file_path AS root_symbol_path,
            ss.start_line AS root_symbol_start_line,
            ss.end_line AS root_symbol_end_line
          FROM start_symbols ss
          UNION ALL
          SELECT
            NULL::int AS root_symbol_id,
            $2::text AS root_symbol_name,
            NULL::text AS root_symbol_path,
            NULL::int AS root_symbol_start_line,
            NULL::int AS root_symbol_end_line
          WHERE NOT EXISTS (SELECT 1 FROM start_symbols)
        ),
        implementation_tree AS (
          SELECT
            rt.root_symbol_id,
            rt.root_symbol_name,
            rt.root_symbol_path,
            rt.root_symbol_start_line,
            rt.root_symbol_end_line,
            sr.source_symbol_id AS implementer_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            1 AS depth,
            ARRAY[sr.source_symbol_id]::int[] AS walk_path
          FROM root_targets rt
          JOIN symbol_relationships sr
            ON sr.relationship_kind IN ('implements', 'extends')
           AND (
             (rt.root_symbol_id IS NOT NULL AND sr.target_symbol_id = rt.root_symbol_id)
             OR (sr.target_symbol_id IS NULL AND lower(sr.target_name) = lower(rt.root_symbol_name))
           )

          UNION ALL

          SELECT
            it.root_symbol_id,
            it.root_symbol_name,
            it.root_symbol_path,
            it.root_symbol_start_line,
            it.root_symbol_end_line,
            sr.source_symbol_id AS implementer_symbol_id,
            sr.target_symbol_id,
            sr.relationship_kind,
            sr.target_name,
            sr.external_module,
            sr.line_no,
            it.depth + 1 AS depth,
            it.walk_path || sr.source_symbol_id
          FROM implementation_tree it
          JOIN symbol_relationships sr
            ON sr.relationship_kind IN ('implements', 'extends')
           AND sr.target_symbol_id = it.implementer_symbol_id
          WHERE it.depth < $3
            AND NOT sr.source_symbol_id = ANY(it.walk_path)
        )
        SELECT DISTINCT
          it.root_symbol_id,
          it.root_symbol_name,
          it.root_symbol_path,
          it.root_symbol_start_line,
          it.root_symbol_end_line,
          it.depth,
          it.relationship_kind,
          impl.name AS implementer_name,
          impl.kind AS implementer_kind,
          impl_file.path AS implementer_path,
          impl.start_line AS implementer_start_line,
          impl.end_line AS implementer_end_line,
          it.target_name,
          it.external_module
        FROM implementation_tree it
        JOIN symbols impl ON impl.id = it.implementer_symbol_id
        JOIN files impl_file ON impl_file.id = impl.file_id AND impl_file.repo = $1
        WHERE impl.kind IN ('class', 'struct', 'enum')
        ORDER BY it.root_symbol_name, it.depth, impl_file.path, impl.start_line
      `,
        [repo, symbol, max_depth],
      );

      if (result.rows.length === 0) {
        return { content: [{ type: "text", text: `No implementations found for "${symbol}" in repo \`${repo}\`.` }] };
      }

      const lines = [`Implementations for \`${symbol}\` in repo \`${repo}\`:`, ""];
      let currentRoot = "";
      for (const row of result.rows as Array<Record<string, unknown>>) {
        const rootLabel = formatSymbolLocator(row);
        if (rootLabel !== currentRoot) {
          if (currentRoot) {
            lines.push("");
          }
          lines.push(`- Root: ${rootLabel}`);
          currentRoot = rootLabel;
        }

        const depth = Number(row.depth);
        const implementerName = String(row.implementer_name || "unknown");
        const implementerKind = String(row.implementer_kind || "unknown");
        const implementerPath = String(row.implementer_path || "unknown");
        const implementerStart = Number(row.implementer_start_line || 0);
        const implementerEnd = Number(row.implementer_end_line || 0);
        const relationshipKind = String(row.relationship_kind || "implements");
        lines.push(
          `  depth ${depth} [${relationshipKind}] <- ${implementerName} (${implementerKind}, ${implementerPath}:${implementerStart}-${implementerEnd})`,
        );
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

}
