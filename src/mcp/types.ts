/**
 * @file src/mcp/types.ts
 * @brief Shared MCP query row and tool typing constants.
 */

/** @brief Supported symbol kinds for indexed declarations. */
export const SYMBOL_KIND_VALUES = [
  "function",
  "class",
  "struct",
  "protocol",
  "interface",
  "type",
  "method",
  "property",
  "variable",
  "constant",
  "enum",
  "impl",
  "namespace",
  "extension",
] as const;

/** @brief Semantic and keyword search row shape. */
export type SearchRow = {
  chunk_id: number;
  file_path: string;
  language: string | null;
  content: string;
  symbol_name: string | null;
  symbol_type: string | null;
  intent: string | null;
  intent_detail: string | null;
  start_line: number;
  end_line: number;
  similarity: number | null;
  keyword_score?: number | null;
};

/** @brief Symbol lookup row shape. */
export type SymbolRow = {
  symbol_id: number;
  name: string;
  qualified_name: string | null;
  kind: string;
  signature: string | null;
  docstring: string | null;
  file_path: string;
  start_line: number;
  end_line: number;
  is_exported: boolean;
  container_symbol: string | null;
  declared_in_extension: boolean;
  is_primary_declaration: boolean;
};

/** @brief Symbol reference result row shape. */
export type ReferenceRow = {
  source_path: string;
  line_no: number;
  reference_kind: string;
  source_symbol_name: string | null;
  target_paths: string[] | null;
};

/** @brief Cross-boundary coupling edge returned by analyze_coupling. */
export type CouplingEdgeRow = {
  direction: string;
  internal_path: string;
  external_path: string;
  kind: string;
  edge_count: number;
};

/** @brief Module interface symbol with external consumer information. */
export type ModuleInterfaceRow = {
  name: string;
  qualified_name: string | null;
  kind: string;
  signature: string | null;
  docstring: string | null;
  file_path: string;
  start_line: number;
  end_line: number;
  visibility: string | null;
  container_symbol: string | null;
  consumer_files: string[] | null;
  total_refs: number | null;
  ref_kinds: string[] | null;
};

/** @brief Lightweight directed edge for graph algorithms. */
export type GraphEdge = {
  source: string;
  target: string;
};

/** @brief Cross-boundary seam edge for modularization planning. */
export type SeamRow = {
  direction: string;
  internal_file: string;
  external_file: string;
  symbol_name: string;
  symbol_kind: string | null;
  signature: string | null;
  reference_kind: string;
  usage_count: number;
};
