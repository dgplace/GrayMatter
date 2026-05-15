/**
 * @file src/repositories/indexBrowser.ts
 * @brief Read-only browser over per-repository indexed tables. Exposes a
 *        whitelisted registry of tables with parameterized SQL for counting
 *        rows and paginating through them — used by the "View Index" modal
 *        in the embedded web UI.
 */

import { query } from "../db.js";

/** @brief Column descriptor for a browseable table. */
export type BrowseColumn = {
  /** Column key as it appears in the row payload. */
  key: string;
  /** Human-readable header label. */
  label: string;
};

/** @brief Whitelisted table that can be browsed for a given repo. */
export type BrowseableTable = {
  /** Stable identifier used in the URL path (e.g. "files", "code_chunks"). */
  name: string;
  /** Human-readable label shown as the tab title. */
  label: string;
  /** Short one-line description of what the table holds. */
  description: string;
  /** Ordered list of columns returned by selectSql. */
  columns: BrowseColumn[];
  /** SQL that counts rows scoped to a single repo ($1 = repo). */
  countSql: string;
  /** SQL that selects rows scoped to a single repo, with $1=repo, $2=limit, $3=offset. */
  selectSql: string;
};

/** @brief Public table-browse list entry returned to the UI. */
export type BrowseTableInfo = {
  name: string;
  label: string;
  description: string;
  row_count: number;
};

/** @brief Page of rows returned to the UI for a single table. */
export type BrowseTablePage = {
  name: string;
  label: string;
  columns: BrowseColumn[];
  filter_options: BrowseTableFilterOption[];
  active_filters: Record<string, string[]>;
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
};

/** @brief One categorical filter available for a browse table. */
export type BrowseTableFilterOption = {
  key: string;
  label: string;
  values: string[];
};

/**
 * @brief Registry of tables exposed to the index browser. Each spec scopes its
 *        queries to a single repo via the join column shown in countSql/selectSql,
 *        and excludes large columns (e.g. vector embeddings, full chunk content)
 *        in favor of compact previews.
 */
const TABLES: BrowseableTable[] = [
  {
    name: "files",
    label: "Files",
    description: "Source files indexed for this repository.",
    columns: [
      { key: "id", label: "id" },
      { key: "path", label: "path" },
      { key: "language", label: "language" },
      { key: "line_count", label: "lines" },
      { key: "size_bytes", label: "bytes" },
      { key: "role", label: "role" },
      { key: "indexed_at", label: "indexed_at" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM files WHERE repo = $1`,
    selectSql: `
      SELECT id, path, language, line_count, size_bytes, role, indexed_at
      FROM files
      WHERE repo = $1
      ORDER BY path
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "code_chunks",
    label: "Code Chunks",
    description: "AST-aware chunks with classified intent. Content is previewed.",
    columns: [
      { key: "id", label: "id" },
      { key: "path", label: "file" },
      { key: "chunk_index", label: "idx" },
      { key: "start_line", label: "start" },
      { key: "end_line", label: "end" },
      { key: "symbol_name", label: "symbol" },
      { key: "symbol_type", label: "symbol_type" },
      { key: "intent", label: "intent" },
      { key: "content_preview", label: "content (200 ch)" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM code_chunks cc JOIN files f ON f.id = cc.file_id
      WHERE f.repo = $1
    `,
    selectSql: `
      SELECT cc.id, f.path, cc.chunk_index, cc.start_line, cc.end_line,
             cc.symbol_name, cc.symbol_type, cc.intent,
             LEFT(cc.content, 200) AS content_preview
      FROM code_chunks cc JOIN files f ON f.id = cc.file_id
      WHERE f.repo = $1
      ORDER BY f.path, cc.chunk_index
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "symbols",
    label: "Symbols",
    description: "Functions, classes, types, and other declarations.",
    columns: [
      { key: "id", label: "id" },
      { key: "path", label: "file" },
      { key: "name", label: "name" },
      { key: "qualified_name", label: "qualified" },
      { key: "kind", label: "kind" },
      { key: "container_symbol", label: "container" },
      { key: "start_line", label: "start" },
      { key: "end_line", label: "end" },
      { key: "is_exported", label: "exported" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM symbols s JOIN files f ON f.id = s.file_id
      WHERE f.repo = $1
    `,
    selectSql: `
      SELECT s.id, f.path, s.name, s.qualified_name, s.kind,
             s.container_symbol, s.start_line, s.end_line, s.is_exported
      FROM symbols s JOIN files f ON f.id = s.file_id
      WHERE f.repo = $1
      ORDER BY f.path, s.start_line
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "symbol_references",
    label: "Symbol References",
    description: "Lexical/call references extracted from chunks.",
    columns: [
      { key: "id", label: "id" },
      { key: "source_path", label: "source_file" },
      { key: "source_symbol_name", label: "source_symbol" },
      { key: "target_name", label: "target_name" },
      { key: "target_symbol_id", label: "target_id" },
      { key: "reference_kind", label: "kind" },
      { key: "reference_kind_v2", label: "kind_v2" },
      { key: "resolution_confidence", label: "confidence" },
      { key: "line_no", label: "line" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM symbol_references sr JOIN files f ON f.id = sr.source_file_id
      WHERE f.repo = $1
    `,
    selectSql: `
      SELECT sr.id, f.path AS source_path, sr.source_symbol_name,
             sr.target_name, sr.target_symbol_id,
             sr.reference_kind, sr.reference_kind_v2,
             sr.resolution_confidence, sr.line_no
      FROM symbol_references sr JOIN files f ON f.id = sr.source_file_id
      WHERE f.repo = $1
      ORDER BY f.path, sr.line_no
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "symbol_relationships",
    label: "Symbol Relationships",
    description: "Structural edges between declarations (extends, implements, etc.).",
    columns: [
      { key: "id", label: "id" },
      { key: "source_path", label: "source_file" },
      { key: "source_symbol_id", label: "source_id" },
      { key: "target_symbol_id", label: "target_id" },
      { key: "target_name", label: "target_name" },
      { key: "relationship_kind", label: "kind" },
      { key: "external_module", label: "external" },
      { key: "line_no", label: "line" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM symbol_relationships sr JOIN files f ON f.id = sr.source_file_id
      WHERE f.repo = $1
    `,
    selectSql: `
      SELECT sr.id, f.path AS source_path, sr.source_symbol_id, sr.target_symbol_id,
             sr.target_name, sr.relationship_kind, sr.external_module, sr.line_no
      FROM symbol_relationships sr JOIN files f ON f.id = sr.source_file_id
      WHERE f.repo = $1
      ORDER BY f.path, sr.line_no
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "dependencies",
    label: "Dependencies",
    description: "Directed graph of imports and inferred calls between files.",
    columns: [
      { key: "id", label: "id" },
      { key: "source_path", label: "source_file" },
      { key: "target_path", label: "target_file" },
      { key: "kind", label: "kind" },
      { key: "imported_name", label: "imported" },
      { key: "local_alias", label: "alias" },
      { key: "external_module", label: "external" },
      { key: "is_external", label: "ext?" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM dependencies d JOIN files f ON f.id = d.source_file_id
      WHERE f.repo = $1
    `,
    selectSql: `
      SELECT d.id, sf.path AS source_path, tf.path AS target_path, d.kind,
             d.imported_name, d.local_alias, d.external_module, d.is_external
      FROM dependencies d
      JOIN files sf ON sf.id = d.source_file_id
      LEFT JOIN files tf ON tf.id = d.target_file_id
      WHERE sf.repo = $1
      ORDER BY sf.path, d.kind
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "dependency_cycles",
    label: "Dependency Cycles",
    description: "Materialized strongly-connected components per repository.",
    columns: [
      { key: "id", label: "id" },
      { key: "cycle_hash", label: "hash" },
      { key: "cycle_size", label: "size" },
      { key: "member_paths", label: "members" },
      { key: "created_at", label: "created_at" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM dependency_cycles WHERE repo = $1`,
    selectSql: `
      SELECT id, cycle_hash, cycle_size, member_paths, created_at
      FROM dependency_cycles
      WHERE repo = $1
      ORDER BY cycle_size DESC, cycle_hash
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "clusters",
    label: "Clusters",
    description: "Semantic groupings of symbols/files (excludes embedding).",
    columns: [
      { key: "id", label: "id" },
      { key: "cluster_key", label: "key" },
      { key: "name", label: "name" },
      { key: "granularity", label: "granularity" },
      { key: "modularity", label: "modularity" },
      { key: "summary", label: "summary" },
      { key: "created_at", label: "created_at" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM clusters WHERE repo = $1`,
    selectSql: `
      SELECT id, cluster_key, name, granularity, modularity, summary, created_at
      FROM clusters
      WHERE repo = $1
      ORDER BY granularity, name
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "cluster_members",
    label: "Cluster Members",
    description: "Per-cluster file or symbol membership.",
    columns: [
      { key: "id", label: "id" },
      { key: "cluster_name", label: "cluster" },
      { key: "granularity", label: "granularity" },
      { key: "symbol_id", label: "symbol_id" },
      { key: "file_id", label: "file_id" },
      { key: "membership_weight", label: "weight" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM cluster_members cm JOIN clusters c ON c.id = cm.cluster_id
      WHERE c.repo = $1
    `,
    selectSql: `
      SELECT cm.id, c.name AS cluster_name, c.granularity,
             cm.symbol_id, cm.file_id, cm.membership_weight
      FROM cluster_members cm JOIN clusters c ON c.id = cm.cluster_id
      WHERE c.repo = $1
      ORDER BY c.name, cm.id
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "flows",
    label: "Flows",
    description: "Call-graph / intent-derived execution flows.",
    columns: [
      { key: "id", label: "id" },
      { key: "flow_key", label: "key" },
      { key: "name", label: "name" },
      { key: "dominant_intent", label: "intent" },
      { key: "summary", label: "summary" },
      { key: "created_at", label: "created_at" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM flows WHERE repo = $1`,
    selectSql: `
      SELECT id, flow_key, name, dominant_intent, summary, created_at
      FROM flows
      WHERE repo = $1
      ORDER BY name
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "flow_members",
    label: "Flow Members",
    description: "Symbol membership within each flow.",
    columns: [
      { key: "id", label: "id" },
      { key: "flow_name", label: "flow" },
      { key: "symbol_id", label: "symbol_id" },
      { key: "role", label: "role" },
      { key: "reason", label: "reason" },
    ],
    countSql: `
      SELECT COUNT(*)::int AS c
      FROM flow_members fm JOIN flows fl ON fl.id = fm.flow_id
      WHERE fl.repo = $1
    `,
    selectSql: `
      SELECT fm.id, fl.name AS flow_name, fm.symbol_id, fm.role, fm.reason
      FROM flow_members fm JOIN flows fl ON fl.id = fm.flow_id
      WHERE fl.repo = $1
      ORDER BY fl.name, fm.id
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "module_intents",
    label: "Module Intents",
    description: "Per-module summary, role, and dominant intent.",
    columns: [
      { key: "module_path", label: "path" },
      { key: "kind", label: "kind" },
      { key: "module_name", label: "name" },
      { key: "role", label: "role" },
      { key: "dominant_intent", label: "intent" },
      { key: "file_count", label: "files" },
      { key: "chunk_count", label: "chunks" },
      { key: "summary", label: "summary" },
      { key: "updated_at", label: "updated_at" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM module_intents WHERE repo = $1`,
    selectSql: `
      SELECT module_path, kind, module_name, role, dominant_intent,
             file_count, chunk_count, summary, updated_at
      FROM module_intents
      WHERE repo = $1
      ORDER BY kind, module_path
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "doc_links",
    label: "Doc Links",
    description: "Documentation prose mapped to files/symbols/clusters (content previewed).",
    columns: [
      { key: "id", label: "id" },
      { key: "source", label: "source" },
      { key: "source_path", label: "source_path" },
      { key: "target_kind", label: "target_kind" },
      { key: "target_id", label: "target_id" },
      { key: "content_preview", label: "content (200 ch)" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM doc_links WHERE repo = $1`,
    selectSql: `
      SELECT id, source, source_path, target_kind, target_id,
             LEFT(content, 200) AS content_preview
      FROM doc_links
      WHERE repo = $1
      ORDER BY source, id
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "ingestion_diagnostics",
    label: "Ingestion Diagnostics",
    description: "Missing-extractor and other diagnostics from indexing.",
    columns: [
      { key: "diagnostic_kind", label: "kind" },
      { key: "framework", label: "framework" },
      { key: "affected_file_count", label: "affected" },
      { key: "details", label: "details" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM ingestion_diagnostics WHERE repo = $1`,
    selectSql: `
      SELECT diagnostic_kind, framework, affected_file_count, details
      FROM ingestion_diagnostics
      WHERE repo = $1
      ORDER BY diagnostic_kind, framework
      LIMIT $2 OFFSET $3
    `,
  },
  {
    name: "ingestion_runs",
    label: "Ingestion Runs",
    description: "Recent ingestion runs for this repository.",
    columns: [
      { key: "id", label: "id" },
      { key: "started_at", label: "started" },
      { key: "completed_at", label: "completed" },
      { key: "files_processed", label: "files" },
      { key: "chunks_created", label: "chunks" },
      { key: "symbols_found", label: "symbols" },
      { key: "status", label: "status" },
    ],
    countSql: `SELECT COUNT(*)::int AS c FROM ingestion_runs WHERE repo = $1`,
    selectSql: `
      SELECT id, started_at, completed_at, files_processed, chunks_created,
             symbols_found, status
      FROM ingestion_runs
      WHERE repo = $1
      ORDER BY started_at DESC
      LIMIT $2 OFFSET $3
    `,
  },
];

/**
 * @brief Lookup a table spec by name. Returns null when the name is not in the
 *        whitelist — callers should respond with 404.
 */
export function getBrowseTableSpec(name: string): BrowseableTable | null {
  return TABLES.find((t) => t.name === name) ?? null;
}

const FILTER_VALUE_LIMIT = 40;
const FILTER_VALUE_MAX_LENGTH = 80;
const FILTER_KEY_HINT = /(kind|granularity|language|role|intent|container|status)/i;
const NON_FILTER_KEY = /(^id$|_id$|_key$|^path$|_path$|^name$|_name$|^summary$|^details$|^content_preview$|^reason$|^module_path$|_hash$)/i;

type SelectSqlParts = {
  baseSql: string;
  orderBySql: string;
};

/**
 * @brief Extract the base SELECT and ORDER BY fragments from a table SQL spec.
 * @param selectSql Parameterized table select SQL.
 * @returns Parsed fragments, or null when the SQL shape is unexpected.
 */
function splitSelectSql(selectSql: string): SelectSqlParts | null {
  const match = selectSql.match(/([\s\S]*?)\bORDER BY\b([\s\S]*?)\bLIMIT\s+\$2\s+OFFSET\s+\$3\s*$/i);
  if (!match) return null;
  return {
    baseSql: match[1].trim(),
    orderBySql: match[2].trim(),
  };
}

/**
 * @brief Quotes an identifier for SQL usage after strict validation.
 * @param identifier Column key to quote.
 * @returns Safe quoted identifier, or null when invalid.
 */
function quoteIdentifier(identifier: string): string | null {
  if (!/^[a-z_][a-z0-9_]*$/i.test(identifier)) return null;
  return `"${identifier.replaceAll("\"", "\"\"")}"`;
}

/**
 * @brief Normalize raw filter payload into non-empty string arrays.
 * @param rawFilters Raw input object from route query parsing.
 * @returns Normalized filter values by key.
 */
function normalizeFilters(rawFilters?: Record<string, string | string[] | undefined>): Record<string, string[]> {
  if (!rawFilters) return {};
  const out: Record<string, string[]> = {};
  for (const [key, raw] of Object.entries(rawFilters)) {
    const values = (Array.isArray(raw) ? raw : [raw])
      .map((value) => String(value ?? "").trim())
      .filter((value) => value.length > 0);
    if (values.length > 0) out[key] = values;
  }
  return out;
}

/**
 * @brief Build a SQL WHERE fragment and params for table filters.
 * @param candidateKeys Allowed column keys for filtering.
 * @param filters Normalized requested filters.
 * @param startParamIndex 1-based SQL parameter index to start from.
 * @returns SQL fragment, params, and accepted filters.
 */
function buildFilterClause(
  candidateKeys: Set<string>,
  filters: Record<string, string[]>,
  startParamIndex: number,
): { sql: string; params: unknown[]; active: Record<string, string[]> } {
  const clauses: string[] = [];
  const params: unknown[] = [];
  const active: Record<string, string[]> = {};

  for (const key of Object.keys(filters).sort()) {
    if (!candidateKeys.has(key)) continue;
    const quoted = quoteIdentifier(key);
    if (!quoted) continue;
    const values = Array.from(new Set(filters[key]));
    if (values.length === 0) continue;
    const paramIndex = startParamIndex + params.length + 1;
    clauses.push(`base.${quoted}::text = ANY($${paramIndex}::text[])`);
    params.push(values);
    active[key] = values;
  }

  if (clauses.length === 0) {
    return { sql: "", params: [], active: {} };
  }
  return { sql: ` WHERE ${clauses.join(" AND ")}`, params, active };
}

/**
 * @brief Build a deterministic outer ORDER BY over exposed output columns.
 * @param spec Browse-table spec with ordered output columns.
 * @returns SQL ORDER BY fragment safe for derived-table queries.
 */
function buildSafeOuterOrderBy(spec: BrowseableTable): string {
  const sortable = spec.columns
    .map((column) => quoteIdentifier(column.key))
    .filter((quoted): quoted is string => Boolean(quoted));
  if (!sortable.length) return "1";
  return sortable.map((quoted) => `base.${quoted}`).join(", ");
}

/**
 * @brief Returns true for columns that are likely categorical filter candidates.
 * @param key Column key.
 * @returns True when the key should be considered for categorical filters.
 */
function isCategoricalCandidateKey(key: string): boolean {
  if (NON_FILTER_KEY.test(key)) return false;
  if (key === "id" || key.endsWith("_id") || key.endsWith("_key")) return false;
  return FILTER_KEY_HINT.test(key);
}

/**
 * @brief Resolve categorical filter options by probing distinct values.
 * @param repo Repository scope.
 * @param spec Table specification.
 * @param parts Parsed select SQL parts.
 * @returns Available filter options for the table.
 */
async function resolveFilterOptions(repo: string, spec: BrowseableTable, parts: SelectSqlParts): Promise<BrowseTableFilterOption[]> {
  const options: BrowseTableFilterOption[] = [];
  for (const column of spec.columns) {
    const key = column.key;
    if (!isCategoricalCandidateKey(key)) continue;
    const quoted = quoteIdentifier(key);
    if (!quoted) continue;
    try {
      const distinctResult = await query(
        `
        SELECT DISTINCT base.${quoted}::text AS value
        FROM (${parts.baseSql}) base
        WHERE base.${quoted} IS NOT NULL
          AND base.${quoted}::text <> ''
          AND length(base.${quoted}::text) <= $2
        ORDER BY value
        LIMIT $3
        `,
        [repo, FILTER_VALUE_MAX_LENGTH, FILTER_VALUE_LIMIT + 1],
      );
      const values = distinctResult.rows
        .map((row: Record<string, unknown>) => String(row.value ?? "").trim())
        .filter((value) => value.length > 0);
      if (values.length === 0 || values.length > FILTER_VALUE_LIMIT) continue;
      options.push({
        key,
        label: column.label,
        values,
      });
    } catch (error) {
      console.warn(`Failed to build filter options for ${spec.name}.${key}:`, error);
    }
  }
  return options;
}

/**
 * @brief Lists browseable tables for a repo with row counts. Tables that fail
 *        to count (e.g. missing optional table) are reported with row_count=0
 *        rather than failing the whole listing.
 */
export async function listBrowseTables(repo: string): Promise<BrowseTableInfo[]> {
  const out: BrowseTableInfo[] = [];
  for (const spec of TABLES) {
    let rowCount = 0;
    try {
      const result = await query(spec.countSql, [repo]);
      rowCount = Number(result.rows[0]?.c ?? 0);
    } catch (error) {
      console.warn(`Failed to count rows for table ${spec.name}:`, error);
    }
    out.push({
      name: spec.name,
      label: spec.label,
      description: spec.description,
      row_count: rowCount,
    });
  }
  return out;
}

/**
 * @brief Fetches a page of rows for the named table, scoped to a repo.
 * @param repo Repository name to scope the query to.
 * @param spec Table spec selected from the whitelist registry.
 * @param limit Maximum rows to return; clamped to [1, 500].
 * @param offset Starting row offset; clamped to >= 0.
 */
export async function fetchBrowseTablePage(
  repo: string,
  spec: BrowseableTable,
  limit: number,
  offset: number,
  rawFilters?: Record<string, string | string[] | undefined>,
): Promise<BrowseTablePage> {
  const safeLimit = Math.max(1, Math.min(500, Number.isFinite(limit) ? Math.floor(limit) : 100));
  const safeOffset = Math.max(0, Number.isFinite(offset) ? Math.floor(offset) : 0);
  const parts = splitSelectSql(spec.selectSql);
  const filters = normalizeFilters(rawFilters);

  if (!parts) {
    const countResult = await query(spec.countSql, [repo]);
    const total = Number(countResult.rows[0]?.c ?? 0);
    const rowsResult = await query(spec.selectSql, [repo, safeLimit, safeOffset]);
    return {
      name: spec.name,
      label: spec.label,
      columns: spec.columns,
      filter_options: [],
      active_filters: {},
      rows: rowsResult.rows as Record<string, unknown>[],
      total,
      limit: safeLimit,
      offset: safeOffset,
    };
  }

  const filterOptions = await resolveFilterOptions(repo, spec, parts);
  const allowedKeys = new Set(filterOptions.map((option) => option.key));
  const filterClause = buildFilterClause(allowedKeys, filters, 1);
  const hasActiveFilters = Object.keys(filterClause.active).length > 0;

  if (!hasActiveFilters) {
    const countResult = await query(spec.countSql, [repo]);
    const total = Number(countResult.rows[0]?.c ?? 0);
    const rowsResult = await query(spec.selectSql, [repo, safeLimit, safeOffset]);
    return {
      name: spec.name,
      label: spec.label,
      columns: spec.columns,
      filter_options: filterOptions,
      active_filters: {},
      rows: rowsResult.rows as Record<string, unknown>[],
      total,
      limit: safeLimit,
      offset: safeOffset,
    };
  }

  const countResult = await query(
    `
    SELECT COUNT(*)::int AS c
    FROM (${parts.baseSql}) base
    ${filterClause.sql}
    `,
    [repo, ...filterClause.params],
  );
  const total = Number(countResult.rows[0]?.c ?? 0);

  const pageLimitParam = 1 + filterClause.params.length + 1;
  const pageOffsetParam = 1 + filterClause.params.length + 2;
  const safeOuterOrderBy = buildSafeOuterOrderBy(spec);
  const rowsResult = await query(
    `
    SELECT *
    FROM (${parts.baseSql}) base
    ${filterClause.sql}
    ORDER BY ${safeOuterOrderBy}
    LIMIT $${pageLimitParam} OFFSET $${pageOffsetParam}
    `,
    [repo, ...filterClause.params, safeLimit, safeOffset],
  );
  return {
    name: spec.name,
    label: spec.label,
    columns: spec.columns,
    filter_options: filterOptions,
    active_filters: filterClause.active,
    rows: rowsResult.rows as Record<string, unknown>[],
    total,
    limit: safeLimit,
    offset: safeOffset,
  };
}
