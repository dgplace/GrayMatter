/**
 * @file src/mcp/tooling/shared.ts
 * @brief Shared MCP tool helpers and constants used across tool registries.
 */

import { builtinModules } from "node:module";

import { query } from "../../db.js";
import { repositoryExists } from "../../repositories/store.js";

/** @brief Standard MCP text response payload for tool handlers. */
export type TextToolResponse = { content: Array<{ type: "text"; text: string }> };

/** @brief Allowed code-intent labels accepted by semantic-search filters. */
export const INTENT_VALUES = [
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
  "documentation",
] as const;

/** @brief Intent value used for documentation-only chunks. */
export const DOCUMENTATION_INTENT = "documentation";

const NODE_SOURCE_LANGUAGES = new Set(["typescript", "tsx", "javascript", "jsx"]);
const PYTHON_STDLIB_MODULES = new Set([
  "argparse",
  "asyncio",
  "base64",
  "collections",
  "concurrent",
  "contextlib",
  "copy",
  "csv",
  "dataclasses",
  "datetime",
  "decimal",
  "enum",
  "functools",
  "glob",
  "hashlib",
  "heapq",
  "html",
  "http",
  "importlib",
  "inspect",
  "io",
  "itertools",
  "json",
  "logging",
  "math",
  "numbers",
  "operator",
  "os",
  "pathlib",
  "posixpath",
  "queue",
  "random",
  "re",
  "shlex",
  "shutil",
  "socket",
  "sqlite3",
  "statistics",
  "string",
  "subprocess",
  "sys",
  "tempfile",
  "threading",
  "time",
  "tomllib",
  "traceback",
  "types",
  "typing",
  "urllib",
  "uuid",
  "warnings",
  "xml",
]);
const NODE_STDLIB_AUGMENTATIONS = ["test", "test/reporters", "sea", "sqlite"];
const NODE_STDLIB_MODULES = new Set(
  [...builtinModules, ...NODE_STDLIB_AUGMENTATIONS]
    .map((moduleName) => moduleName.replace(/^node:/, ""))
    .map((moduleName) => moduleName.split("/", 1)[0]),
);

/**
 * @brief Creates a consistent not-found payload when a repo is missing.
 * @param repo Repository name supplied by the caller.
 * @returns Text payload ready for MCP content response.
 */
export function repoNotFoundText(repo: string): string {
  return `Repository \`${repo}\` is not indexed. Use \`list_repositories\` to discover available repositories.`;
}

/**
 * @brief Checks that a repo exists before executing a repo-scoped query.
 * @param repo Repository name.
 * @returns Null when present, or an MCP response object when absent.
 */
export async function requireRepository(repo: string): Promise<TextToolResponse | null> {
  if (await repositoryExists(repo)) {
    return null;
  }
  return { content: [{ type: "text", text: repoNotFoundText(repo) }] };
}

/**
 * @brief Returns the distinct first-segment top-level directories indexed for a repo.
 * @param repo Repository name.
 * @returns Sorted list of top-level directory names (e.g. ["codebrain", "desktop", "src"]).
 */
export async function getTopLevelDirs(repo: string): Promise<string[]> {
  const result = await query(
    `
    SELECT DISTINCT split_part(path, '/', 1) AS seg
    FROM files
    WHERE repo = $1
      AND path LIKE '%/%'
    ORDER BY seg
    `,
    [repo],
  );
  return result.rows
    .map((row: Record<string, unknown>) => String(row.seg || ""))
    .filter((seg) => seg.length > 0);
}

/**
 * @brief Returns the distinct second-segment sub-directories under a given top-level dir.
 * @param repo Repository name.
 * @param topDir First-segment directory (without trailing slash).
 * @returns Sorted list of "topDir/sub" entries that exist in the index.
 */
export async function getSecondLevelDirs(repo: string, topDir: string): Promise<string[]> {
  if (!topDir) {
    return [];
  }
  const result = await query(
    `
    SELECT DISTINCT split_part(substring(path FROM length($2) + 2), '/', 1) AS seg
    FROM files
    WHERE repo = $1
      AND path LIKE $2 || '/%/%'
    ORDER BY seg
    `,
    [repo, topDir],
  );
  return result.rows
    .map((row: Record<string, unknown>) => `${topDir}/${String(row.seg || "")}`)
    .filter((entry) => !entry.endsWith("/"));
}

/**
 * @brief Returns a "Next steps" footer to append to successful codebrain results.
 *
 * Embeds the next-tool suggestion in the result the agent just read, fighting the
 * habit of falling back to broad text search or whole-file reads once a
 * codebrain call succeeds.
 *
 * @param tool Name of the tool whose result is being annotated.
 * @returns Plain-text footer ready to concatenate after the main result body.
 */
export function nextStepFooter(tool: string): string {
  const footers: Record<string, string> = {
    find_symbol:
      "Next: `find_references name=<symbol>` for callers/usages, `describe_node kind=symbol id=<symbol>` for the gist, or `find_symbol kind=method file=<path>` to list a file's methods. Read only when you need specific implementation lines from a known range. If this missed a known declaration, verify with scoped text search by path and file type.",
    exact_symbol_search:
      "Next: `find_references name=<symbol>` for callers, `describe_node kind=symbol id=<symbol>` for the doc/summary, or `find_call_graph` for callers/callees. Read only the line range shown above. If this missed a known declaration, verify with scoped text search by path and file type.",
    semantic_search:
      "Next: if any result names a real symbol, switch to `find_symbol`/`exact_symbol_search` on that name -- the index resolves it precisely. If top results are unrelated after two attempts, switch to scoped text search rather than rephrasing.",
    describe_node:
      "Next: `find_references` for usages, `find_call_graph` for callers/callees, `find_implementations`/`find_subtypes` for related types, or `extract_module_interface path_prefix=<dir>` for the module's public API. Read only specific line ranges, never the whole file.",
    get_file_map:
      "Next: `find_symbol kind=class file=<path>` or `find_symbol kind=method file=<path>` to list a file's symbols (NOT Read the whole file), or `describe_node kind=file id=<path>` for its gist. Use `extract_module_interface path_prefix=<dir>` for the module's public API.",
    get_module_map:
      "Next: `find_symbol`/`extract_module_interface` for a specific module, or `get_file_map path_prefix=<module>` to drill into its files.",
    find_references:
      "Next: `describe_node kind=symbol id=<caller>` for any caller you want to understand. Read only the cited line range -- callers are usually short.",
    find_call_graph:
      "Next: `describe_node` or `find_references` on any node of interest. Read only specific line ranges.",
    extract_module_interface:
      "Next: `describe_node kind=symbol id=<exported>` for any symbol, or `find_references` to see who consumes the API.",
  };
  const body = footers[tool];
  return body ? `\n\n---\nNEXT STEP: ${body}` : "";
}

/**
 * @brief Builds a self-correcting message for tools that took an unmatched `path_prefix`.
 *
 * Returns suggested top-level directories indexed for the repo so the caller can
 * retry with a valid prefix instead of bailing to broad text search.
 *
 * @param repo Repository name.
 * @param badPrefix The path_prefix the caller passed that matched zero files.
 * @param toolHint Optional next-step hint specific to the calling tool.
 * @returns Human-readable text payload listing valid prefixes.
 */
export async function buildPathPrefixHint(
  repo: string,
  badPrefix: string,
  toolHint?: string,
): Promise<string> {
  const topDirs = await getTopLevelDirs(repo);
  const lines: string[] = [];
  lines.push(
    `No files matched \`path_prefix=${badPrefix || "(empty)"}\` in repo \`${repo}\`.`,
  );

  if (topDirs.length === 0) {
    lines.push("Repository has no indexed directories. The index may be empty or stale -- consider re-ingesting.");
    return lines.join("\n");
  }

  lines.push("");
  lines.push("Indexed top-level directories:");
  for (const dir of topDirs) {
    lines.push(`  - \`${dir}/\``);
  }

  if (badPrefix) {
    const guess = badPrefix.split("/")[0] ?? "";
    const matches = topDirs.filter((d) => d.toLowerCase().includes(guess.toLowerCase()));
    if (matches.length > 0 && !topDirs.includes(guess)) {
      lines.push("");
      lines.push(`Closest matches to your prefix: ${matches.map((m) => `\`${m}/\``).join(", ")}`);
    }
  }

  lines.push("");
  lines.push(
    toolHint ??
      "Retry with one of the listed prefixes, or call with no `path_prefix` to see the indexed repo. If these indexed prefixes contradict the visible working tree, use scoped text search and refresh the index.",
  );
  return lines.join("\n");
}

/**
 * @brief Render resolved or unresolved relationship targets as readable labels.
 * @param row Relationship row with optional resolved file/location columns.
 * @returns Display label for a structural target symbol.
 */
export function formatRelationshipTarget(row: Record<string, unknown>): string {
  const targetName = String(row.target_name || "unknown");
  const targetPath = row.target_path ? String(row.target_path) : null;
  const targetStart = row.target_start_line ? Number(row.target_start_line) : null;
  const targetEnd = row.target_end_line ? Number(row.target_end_line) : null;
  if (targetPath && targetStart && targetEnd) {
    return `${targetName} (${targetPath}:${targetStart}-${targetEnd})`;
  }

  const externalModule = row.external_module ? String(row.external_module) : null;
  if (externalModule) {
    return `${targetName} (external: ${externalModule})`;
  }
  return targetName;
}

/**
 * @brief Build a stable symbol locator string for output headers.
 * @param row Query row containing symbol/path/line metadata.
 * @returns Human-readable symbol locator.
 */
export function formatSymbolLocator(row: Record<string, unknown>): string {
  const symbolName = String(row.root_symbol_name || row.symbol_name || "unknown");
  const symbolPath = row.root_symbol_path ? String(row.root_symbol_path) : row.symbol_path ? String(row.symbol_path) : null;
  const start = row.root_symbol_start_line ? Number(row.root_symbol_start_line) : row.symbol_start_line ? Number(row.symbol_start_line) : null;
  const end = row.root_symbol_end_line ? Number(row.root_symbol_end_line) : row.symbol_end_line ? Number(row.symbol_end_line) : null;
  if (symbolPath && start && end) {
    return `${symbolName} (${symbolPath}:${start}-${end})`;
  }
  return symbolName;
}

/**
 * @brief Maps raw edge kinds to normalized impact categories shown in MCP output.
 * @param edgeKind Raw traversal edge kind.
 * @returns One of calls, instantiations, structural, imports, or other.
 */
export function impactCategory(edgeKind: string): "calls" | "instantiations" | "structural" | "imports" | "other" {
  if (edgeKind === "instantiation" || edgeKind === "injection") {
    return "instantiations";
  }
  if (edgeKind === "import") {
    return "imports";
  }
  if (["extends", "implements", "mixin", "type_alias", "inheritance"].includes(edgeKind)) {
    return "structural";
  }
  if (["call", "member_call", "service_usage"].includes(edgeKind)) {
    return "calls";
  }
  return "other";
}

/**
 * @brief Checks whether a normalized package name should be treated as language stdlib.
 * @param language Source file language associated with the dependency record.
 * @param moduleName Normalized package/module name.
 * @returns True when the dependency is likely from Python/Node standard libraries.
 */
export function isStdlibModule(language: string | null, moduleName: string): boolean {
  const normalized = moduleName.trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  if (language === "python") {
    return PYTHON_STDLIB_MODULES.has(normalized);
  }
  if (language && NODE_SOURCE_LANGUAGES.has(language)) {
    return NODE_STDLIB_MODULES.has(normalized);
  }
  return false;
}

/**
 * @brief Computes first-party module candidate names from indexed file paths for a repo.
 * @param repo Repository name.
 * @returns Lowercased set of names that should not be reported as external packages.
 */
export async function getFirstPartyModuleNames(repo: string): Promise<Set<string>> {
  const result = await query(
    `
    SELECT DISTINCT lower(split_part(f.path, '/', 1)) AS name
    FROM files f
    WHERE f.repo = $1
      AND split_part(f.path, '/', 1) <> ''
      AND split_part(f.path, '/', 1) NOT LIKE '.%'
    UNION
    SELECT DISTINCT lower(regexp_replace(regexp_replace(f.path, '.*/', ''), '\\.[^.]+$', '')) AS name
    FROM files f
    WHERE f.repo = $1
    `,
    [repo],
  );
  const names = new Set<string>();
  for (const row of result.rows as Array<Record<string, unknown>>) {
    const name = row.name ? String(row.name).trim() : "";
    if (name) {
      names.add(name);
    }
  }
  return names;
}

/**
 * @brief Returns true when a module name should be treated as repo-local first-party code.
 * @param firstParty Lowercased first-party name set produced by `getFirstPartyModuleNames`.
 * @param moduleName Normalized module name from the dependency row.
 * @returns True when the module matches a first-party candidate.
 */
export function isFirstPartyModule(firstParty: Set<string>, moduleName: string): boolean {
  const normalized = moduleName.trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  if (firstParty.has(normalized)) {
    return true;
  }
  const dehyphenated = normalized.replace(/-/g, "_");
  return dehyphenated !== normalized && firstParty.has(dehyphenated);
}

/**
 * @brief Loads doc-link prose rows associated with a persisted node.
 * @param repo Repository scope for the query.
 * @param kind Node kind (`file`, `symbol`, or `cluster`).
 * @param targetId Numeric node id in the target table.
 * @returns Ordered doc-link rows with source metadata and content text.
 */
export async function getNodeDocLinks(
  repo: string,
  kind: "file" | "symbol" | "cluster",
  targetId: number,
): Promise<Array<Record<string, unknown>>> {
  const result = await query(
    `
    SELECT
      source,
      source_path,
      content,
      created_at
    FROM doc_links
    WHERE repo = $1
      AND target_kind = $2
      AND target_id = $3
    ORDER BY created_at ASC, id ASC
    `,
    [repo, kind, targetId],
  );
  return result.rows as Array<Record<string, unknown>>;
}
