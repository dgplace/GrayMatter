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
