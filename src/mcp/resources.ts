/**
 * @file src/mcp/resources.ts
 * @brief MCP resource registration for usage guidance.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const CODEBRAIN_USAGE_URI = "codebrain://usage";
export const CODEBRAIN_SERVER_INSTRUCTIONS = [
  "Use CodeBrain first for repository-scoped discovery (symbol lookup, references, semantic search, dependency and architecture analysis); use `rg`/`grep` to complement it for exact text matches and to verify index-backed findings.",
  "Read the `codebrain://usage` resource for the full per-tool workflow guide, parameter conventions, and anti-patterns.",
].join(" ");
const CODEBRAIN_USAGE_TEXT = [
  "# CodeBrain Usage",
  "",
  "All query tools (everything except `list_repositories`) are repository-scoped and require the `repo` parameter.",
  "",
  "## Self-Discovery Policy",
  "",
  "- **Always start with** `list_repositories` to find the correct `repo` string. Do not guess it from the working directory.",
  "- **Use MCP tools first** for semantic, symbol, reference, dependency, hierarchy, and architecture analysis.",
  "- **Use external text search (`rg`, `grep`)** as a precision and verification complement, or when index coverage may be stale.",
  "- If the index appears stale or inconsistent with the working tree, re-ingest rather than working around it with ad-hoc heuristics.",
  "",
  "## Workflows",
  "",
  "### 1. Concept discovery",
  "When you need to find where a feature lives but don't know exact names:",
  "- `semantic_search` with a 2-8 word technical phrase. Optional filters: `intent` (e.g., `business-logic`, `data-model`), `language`, `path_prefix`, `threshold`. Documentation chunks are filtered out unless `include_documentation: true`.",
  "- If you already know a partial identifier, prefer `find_symbol` -- faster and more precise.",
  "",
  "### 2. Symbol and reference lookups",
  "- `find_symbol` -- partial-name lookup ranked by match strength; supports `kind` and `file` filters.",
  "- `exact_symbol_search` -- exact identifier match for grep-like precision.",
  "- `find_references` -- lexical and call references. Defaults to resolved, high-confidence matches (`min_confidence` 0.55). Pass `include_unresolved: true` for heuristic-only matches; set `reference_kind` to filter to `call` / `member_call` / `type_reference` / `instantiation`.",
  "",
  "### 3. Control flow and impact",
  "- `find_call_graph` -- bounded forward (callees) or reverse (callers) graph, resolved edges only.",
  "- `find_impact` -- reverse-traverses confidence-scored edges. Returns \"likely\" (>=0.75) and \"possible\" (>=`min_confidence`) bands grouped by edge category.",
  "- `trace_dependencies` -- inbound/outbound/both walk across file boundaries up to `max_depth`. Pass `summary: true` for aggregated counts.",
  "",
  "### 4. Type hierarchies and interfaces",
  "- `find_supertypes` / `find_subtypes` -- transitive walk over `extends`/`implements` relationships.",
  "- `find_implementations` -- direct and transitive implementers/subclasses of an interface or abstract type.",
  "- `find_instantiations` -- where a `class`/`struct` is actually constructed.",
  "",
  "### 5. Architectural and subsystem overview",
  "- `get_file_map` -- per-file roles, summaries, and exported symbols under a `path_prefix`.",
  "- `get_intent` -- file summary plus per-chunk intent labels and line ranges. Use before editing a file you haven't read.",
  "- `get_module_map` -- directory and logical modules with role, dominant intent, and counts. Filter by `kind` (`directory` | `logical` | `all`) or `path_prefix`.",
  "- `codebase_stats` -- repo-level totals, language mix, intent distribution, symbol-kind counts.",
  "- `get_index_size` -- estimated storage and row counts.",
  "",
  "### 6. Clusters and execution flows",
  "- `clusters` -- named clusters with size, modularity, and granularity (`symbol` or `file`).",
  "- `cluster_members` -- weighted members of a cluster. Accepts cluster id, `cluster_key`, or name.",
  "- `find_flows` -- execution-flow memberships. Pass exactly one of `symbol` (lists flows that include the symbol) or `flow` (lists members of the flow). Flow selector accepts id, `flow_key`, or name.",
  "- `describe_node` -- unified description of a `file`, `symbol`, or `cluster`, including any linked doc_links prose.",
  "",
  "### 7. Refactoring and modularization",
  "- `analyze_coupling` -- afferent/efferent coupling and instability for a `path_prefix`, plus top cross-boundary file pairs.",
  "- `extract_module_interface` -- the public surface of a directory (exports actually consumed externally). Pass `include_unused: true` to also list exported-but-unused symbols.",
  "- `find_modularization_seams` -- extraction plan: required interfaces, dependencies to inject, and seam edges to cut.",
  "- `find_cycles` -- persisted dependency cycles, optionally filtered by `path_prefix`.",
  "- `find_external_dependencies` -- third-party packages summarized by usage, with optional `package_name` to drill into consumers. Stdlib excluded unless `include_stdlib: true`.",
  "",
  "### 8. Index management",
  "- `delete_index` -- destructive; permanently removes all indexed data for a repo. Requires `confirm: true`. Do not call without explicit user instruction.",
  "",
  "## Anti-Patterns",
  "",
  "- **Do not call query tools without a `repo`.** Run `list_repositories` first.",
  "- **Do not loop `semantic_search` with slightly different phrasings.** If two attempts miss, switch to `find_symbol`/`exact_symbol_search` or `rg`.",
  "- **Do not bypass stale-index symptoms with per-tool workarounds.** Re-ingest first.",
  "- **Do not call `delete_index` to \"clean up\".** It is destructive and requires explicit user instruction.",
  "- **Do not assume `find_references` includes unresolved heuristic matches.** They are filtered by default; set `include_unresolved: true` if you specifically want them.",
].join("\n");

/**
 * @brief Registers usage documentation as an MCP resource.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerResources(server: McpServer): void {
  server.registerResource(
    "usage",
    CODEBRAIN_USAGE_URI,
    {
      title: "CodeBrain Usage",
      description: "Read this first for the recommended CodeBrain workflow and search strategy.",
      mimeType: "text/markdown",
    },
    async (uri) => ({
      contents: [
        {
          uri: uri.toString(),
          mimeType: "text/markdown",
          text: CODEBRAIN_USAGE_TEXT,
        },
      ],
    }),
  );
}
