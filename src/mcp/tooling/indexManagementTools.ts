/**
 * @file src/mcp/tooling/indexManagementTools.ts
 * @brief MCP tools for index management, modules/clusters, and node description.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { query } from "../../db.js";
import {
  deleteRepository,
  getModuleIntents,
  getRepositoryIndexSize,
  repositoryExists,
} from "../../repositories/store.js";
import { logToolInvocation } from "../logging.js";
import { getNodeDocLinks, repoNotFoundText, requireRepository } from "./shared.js";

/**
 * @brief Registers index and cluster management tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerIndexManagementTools(server: McpServer): void {
  server.tool(
    "get_index_size",
    "Reports the estimated storage size and row counts for a repository's index in the database. Useful for understanding how much data is indexed.",
    {
      repo: z.string().min(1).describe("Repository name. Required."),
    },
    async ({ repo }) => {
      logToolInvocation("get_index_size", { repo });

      const size = await getRepositoryIndexSize(repo);
      if (!size) {
        return { content: [{ type: "text", text: repoNotFoundText(repo) }] };
      }

      function humanBytes(bytes: number): string {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
      }

      const lines = [
        `# Index Size: ${size.repo}`,
        "",
        `| Metric | Value |`,
        `|--------|-------|`,
        `| Files | ${size.file_count.toLocaleString()} |`,
        `| Chunks | ${size.chunk_count.toLocaleString()} |`,
        `| Symbols | ${size.symbol_count.toLocaleString()} |`,
        `| References | ${size.ref_count.toLocaleString()} |`,
        `| Content text | ${humanBytes(size.content_bytes)} |`,
        `| Embeddings (est.) | ${humanBytes(size.estimated_embedding_bytes)} |`,
        `| **Total (est.)** | **${humanBytes(size.estimated_total_bytes)}** |`,
        "",
        "_Embedding estimate: 768-dim float32 vectors × chunk count._",
      ];

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "get_module_map",
    "Returns a repository-scoped module map, detailing directory-based and logical modules and their roles.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      path_prefix: z.string().optional().describe("Optional path prefix to filter modules."),
      kind: z.enum(["directory", "logical", "all"]).optional().describe("Module kind to return."),
    },
    async ({ repo, path_prefix, kind = "all" }) => {
      logToolInvocation("get_module_map", { repo, path_prefix, kind });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) return repoCheck;

      const modules = await getModuleIntents(repo, kind, path_prefix);
      if (modules.length === 0) {
        return { content: [{ type: "text", text: `No modules found in \`${repo}\`. You may need to run module synthesis first.` }] };
      }

      const formatted = modules.map((m: any) => {
        return `## ${m.module_path} (${m.kind})\n` +
               `Role: ${m.role}\n` +
               `Dominant Intent: ${m.dominant_intent}\n` +
               `Files: ${m.file_count}, Chunks: ${m.chunk_count}\n` +
               `Summary: ${m.summary}\n`;
      }).join("\n");

      return { content: [{ type: "text", text: formatted }] };
    },
  );

  server.tool(
    "clusters",
    "Lists repository clusters with name, summary, member count, modularity, and granularity. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      granularity: z.enum(["symbol", "file"]).optional().describe("Optional cluster granularity filter."),
    },
    async ({ repo, granularity }) => {
      logToolInvocation("clusters", { repo, granularity });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const result = await query(
        `
        SELECT
          c.id,
          c.cluster_key,
          c.name,
          c.summary,
          c.modularity,
          c.granularity,
          COUNT(cm.id)::integer AS size
        FROM clusters c
        LEFT JOIN cluster_members cm ON cm.cluster_id = c.id
        WHERE c.repo = $1
          AND ($2::text IS NULL OR c.granularity = $2)
        GROUP BY c.id, c.cluster_key, c.name, c.summary, c.modularity, c.granularity
        ORDER BY c.granularity, size DESC, c.name, c.cluster_key
        `,
        [repo, granularity || null],
      );

      if (result.rows.length === 0) {
        const scope = granularity ? ` (granularity: \`${granularity}\`)` : "";
        return { content: [{ type: "text", text: `No clusters found for repo \`${repo}\`${scope}.` }] };
      }

      const lines = [
        `Clusters for \`${repo}\`${granularity ? ` (granularity: \`${granularity}\`)` : ""}:`,
        "",
        "| Cluster | Name | Summary | Size | Modularity | Granularity |",
        "|---:|---|---|---:|---:|---|",
      ];
      for (const row of result.rows as Array<Record<string, unknown>>) {
        const summary = row.summary ? String(row.summary).replace(/\n+/g, " ").trim() : "";
        const compactSummary = summary.length > 180 ? `${summary.slice(0, 177)}...` : summary;
        lines.push(
          `| ${Number(row.id)} (\`${String(row.cluster_key)}\`) | ${String(row.name)} | ${compactSummary || "(none)"} | ${Number(row.size)} | ${Number(row.modularity || 0).toFixed(4)} | ${String(row.granularity)} |`,
        );
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "cluster_members",
    "Lists weighted members of a repository cluster. Member shape follows the cluster granularity (symbol or file). Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      cluster: z.string().min(1).describe("Cluster selector: id, cluster_key, or cluster name."),
      limit: z.number().int().min(1).max(500).optional().describe("Maximum members to return (default 200)."),
    },
    async ({ repo, cluster, limit = 200 }) => {
      logToolInvocation("cluster_members", { repo, cluster, limit });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      const clusterResult = await query(
        `
        SELECT id, cluster_key, name, granularity
        FROM clusters
        WHERE repo = $1
          AND (
            ($2 ~ '^[0-9]+$' AND id = $2::int)
            OR cluster_key = $2
            OR name ILIKE $2
            OR name ILIKE '%' || $2 || '%'
          )
        ORDER BY
          CASE
            WHEN cluster_key = $2 THEN 0
            WHEN name ILIKE $2 THEN 1
            WHEN ($2 ~ '^[0-9]+$' AND id = $2::int) THEN 2
            ELSE 3
          END,
          id
        LIMIT 1
        `,
        [repo, cluster],
      );

      if (clusterResult.rows.length === 0) {
        return {
          content: [{
            type: "text",
            text: `Cluster \`${cluster}\` was not found in repo \`${repo}\`. Use \`clusters\` to list available cluster ids and keys.`,
          }],
        };
      }

      const clusterRow = clusterResult.rows[0] as Record<string, unknown>;
      const clusterId = Number(clusterRow.id);
      const clusterKey = String(clusterRow.cluster_key);
      const clusterName = String(clusterRow.name);
      const clusterGranularity = String(clusterRow.granularity);

      if (clusterGranularity === "symbol") {
        const membersResult = await query(
          `
          SELECT
            cm.membership_weight,
            s.id AS symbol_id,
            s.name,
            s.kind,
            s.qualified_name,
            s.start_line,
            s.end_line,
            f.path AS file_path
          FROM cluster_members cm
          JOIN symbols s ON s.id = cm.symbol_id
          JOIN files f ON f.id = s.file_id
          WHERE cm.cluster_id = $1
          ORDER BY cm.membership_weight DESC NULLS LAST, f.path, s.start_line, s.name
          LIMIT $2
          `,
          [clusterId, limit],
        );

        const lines = [
          `Cluster members for \`${repo}\` -> ${clusterName} (\`${clusterKey}\`, symbol, id=${clusterId})`,
          "",
          "| Symbol ID | Name | Kind | Location | Weight |",
          "|---:|---|---|---|---:|",
        ];
        for (const row of membersResult.rows as Array<Record<string, unknown>>) {
          const qualifiedName = row.qualified_name ? ` (${String(row.qualified_name)})` : "";
          lines.push(
            `| ${Number(row.symbol_id)} | ${String(row.name)}${qualifiedName} | ${String(row.kind)} | ${String(row.file_path)}:${Number(row.start_line)}-${Number(row.end_line)} | ${Number(row.membership_weight || 0).toFixed(4)} |`,
          );
        }
        if (membersResult.rows.length === 0) {
          lines.push("| - | (none) | - | - | - |");
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      }

      const membersResult = await query(
        `
        SELECT
          cm.membership_weight,
          f.id AS file_id,
          f.path AS file_path,
          f.language,
          f.summary
        FROM cluster_members cm
        JOIN files f ON f.id = cm.file_id
        WHERE cm.cluster_id = $1
        ORDER BY cm.membership_weight DESC NULLS LAST, f.path
        LIMIT $2
        `,
        [clusterId, limit],
      );

      const lines = [
        `Cluster members for \`${repo}\` -> ${clusterName} (\`${clusterKey}\`, file, id=${clusterId})`,
        "",
        "| File ID | Path | Language | Summary | Weight |",
        "|---:|---|---|---|---:|",
      ];
      for (const row of membersResult.rows as Array<Record<string, unknown>>) {
        const summary = row.summary ? String(row.summary).replace(/\n+/g, " ").trim() : "";
        const compactSummary = summary.length > 140 ? `${summary.slice(0, 137)}...` : summary;
        lines.push(
          `| ${Number(row.file_id)} | ${String(row.file_path)} | ${String(row.language || "unknown")} | ${compactSummary || "(none)"} | ${Number(row.membership_weight || 0).toFixed(4)} |`,
        );
      }
      if (membersResult.rows.length === 0) {
        lines.push("| - | (none) | - | - | - |");
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "describe_node",
    "Returns a unified description for a file, symbol, or cluster and includes all linked doc_links prose rows. Repository scope is required.",
    {
      repo: z.string().min(1).describe("Repository name to search in. Required."),
      kind: z.enum(["file", "symbol", "cluster"]).describe("Node kind to describe."),
      id: z.string().min(1).describe("Node identifier: numeric id or kind-specific selector (file path, symbol name, cluster key/name)."),
    },
    async ({ repo, kind, id }) => {
      logToolInvocation("describe_node", { repo, kind, id });

      const repoCheck = await requireRepository(repo);
      if (repoCheck) {
        return repoCheck;
      }

      let nodeId = -1;
      let heading = "";
      const detailLines: string[] = [];
      const isNumericId = /^[0-9]+$/.test(id);

      if (kind === "file") {
        const fileResult = await query(
          `
          SELECT id, path, language, line_count, role, summary
          FROM files
          WHERE repo = $1
            AND (
              ($2 ~ '^[0-9]+$' AND id = $2::int)
              OR path = $2
              OR path LIKE '%' || $2 || '%'
            )
          ORDER BY
            CASE
              WHEN ($2 ~ '^[0-9]+$' AND id = $2::int) THEN 0
              WHEN path = $2 THEN 1
              ELSE 2
            END,
            path
          LIMIT 1
          `,
          [repo, id],
        );
        if (fileResult.rows.length === 0) {
          return { content: [{ type: "text", text: `Unknown file node \`${id}\` in repo \`${repo}\`.` }] };
        }
        const row = fileResult.rows[0] as Record<string, unknown>;
        nodeId = Number(row.id);
        heading = `File ${String(row.path)} (id=${nodeId})`;
        detailLines.push(
          `- language: ${String(row.language || "unknown")}`,
          `- lines: ${Number(row.line_count || 0)}`,
          `- role: ${String(row.role || "unknown")}`,
          `- summary: ${String(row.summary || "(none)")}`,
        );
      } else if (kind === "symbol") {
        const symbolResult = await query(
          `
          SELECT
            s.id,
            s.name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.docstring,
            s.start_line,
            s.end_line,
            s.visibility,
            s.is_exported,
            f.path AS file_path
          FROM symbols s
          JOIN files f ON f.id = s.file_id
          WHERE f.repo = $1
            AND (
              ($2 ~ '^[0-9]+$' AND s.id = $2::int)
              OR s.qualified_name = $2
              OR s.name = $2
              OR s.qualified_name ILIKE '%' || $2 || '%'
              OR s.name ILIKE '%' || $2 || '%'
            )
          ORDER BY
            CASE
              WHEN ($2 ~ '^[0-9]+$' AND s.id = $2::int) THEN 0
              WHEN s.qualified_name = $2 THEN 1
              WHEN s.name = $2 THEN 2
              ELSE 3
            END,
            CASE WHEN s.is_primary_declaration THEN 0 ELSE 1 END,
            f.path,
            s.start_line
          LIMIT 1
          `,
          [repo, id],
        );
        if (symbolResult.rows.length === 0) {
          return { content: [{ type: "text", text: `Unknown symbol node \`${id}\` in repo \`${repo}\`.` }] };
        }
        const row = symbolResult.rows[0] as Record<string, unknown>;
        nodeId = Number(row.id);
        heading = `Symbol ${String(row.name)} (id=${nodeId})`;
        detailLines.push(
          `- qualified_name: ${String(row.qualified_name || "(none)")}`,
          `- kind: ${String(row.kind || "unknown")}`,
          `- location: ${String(row.file_path)}:${Number(row.start_line || 0)}-${Number(row.end_line || 0)}`,
          `- visibility: ${String(row.visibility || "unknown")}`,
          `- exported: ${Boolean(row.is_exported)}`,
          `- signature: ${String(row.signature || "(none)")}`,
          `- docstring: ${String(row.docstring || "(none)")}`,
        );
      } else {
        const clusterResult = await query(
          `
          SELECT id, cluster_key, name, summary, modularity, granularity
          FROM clusters
          WHERE repo = $1
            AND (
              ($2 ~ '^[0-9]+$' AND id = $2::int)
              OR cluster_key = $2
              OR name ILIKE $2
              OR name ILIKE '%' || $2 || '%'
            )
          ORDER BY
            CASE
              WHEN ($2 ~ '^[0-9]+$' AND id = $2::int) THEN 0
              WHEN cluster_key = $2 THEN 1
              WHEN name ILIKE $2 THEN 2
              ELSE 3
            END,
            id
          LIMIT 1
          `,
          [repo, id],
        );
        if (clusterResult.rows.length === 0) {
          return { content: [{ type: "text", text: `Unknown cluster node \`${id}\` in repo \`${repo}\`.` }] };
        }
        const row = clusterResult.rows[0] as Record<string, unknown>;
        nodeId = Number(row.id);
        heading = `Cluster ${String(row.name)} (id=${nodeId})`;
        detailLines.push(
          `- cluster_key: ${String(row.cluster_key)}`,
          `- granularity: ${String(row.granularity)}`,
          `- modularity: ${Number(row.modularity || 0).toFixed(4)}`,
          `- summary: ${String(row.summary || "(none)")}`,
        );
      }

      const docLinks = await getNodeDocLinks(repo, kind, nodeId);
      const lines = [
        `Describe node for repo \`${repo}\`:`,
        "",
        `## ${heading}`,
        ...detailLines,
        "",
        `## Linked Doc Links (${docLinks.length})`,
      ];
      if (docLinks.length === 0) {
        lines.push("", "None.");
      } else {
        for (const [index, row] of docLinks.entries()) {
          const source = String(row.source || "unknown");
          const sourcePath = row.source_path ? String(row.source_path) : "(none)";
          const content = String(row.content || "");
          lines.push(
            "",
            `### ${index + 1}. source=${source} source_path=${sourcePath}`,
            content,
          );
        }
      }

      if (isNumericId && nodeId !== Number(id)) {
        lines.splice(3, 0, `- resolved_from_id: ${id}`);
      }

      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.tool(
    "delete_index",
    "Permanently deletes all indexed data for a repository from the database (files, chunks, embeddings, symbols, references). This cannot be undone. Pass confirm=true to proceed.",
    {
      repo: z.string().min(1).describe("Repository name to delete. Required."),
      confirm: z
        .boolean()
        .describe("Must be true to proceed. Prevents accidental deletion."),
    },
    async ({ repo, confirm }) => {
      logToolInvocation("delete_index", { repo, confirm });

      if (!confirm) {
        return {
          content: [
            {
              type: "text",
              text: `Deletion aborted. Pass \`confirm: true\` to permanently delete the index for \`${repo}\`.`,
            },
          ],
        };
      }

      const exists = await repositoryExists(repo);
      if (!exists) {
        return { content: [{ type: "text", text: repoNotFoundText(repo) }] };
      }

      const deleted = await deleteRepository(repo);
      return {
        content: [
          {
            type: "text",
            text: `Deleted index for \`${repo}\`: ${deleted} file records removed (chunks, embeddings, symbols, and references cascade-deleted).`,
          },
        ],
      };
    },
  );
}
