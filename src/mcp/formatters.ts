/**
 * @file src/mcp/formatters.ts
 * @brief Text formatting helpers for MCP tool responses.
 */

import type { ReferenceRow, SearchRow, SymbolRow } from "./types.js";

/**
 * @brief Formats hybrid search results into markdown text blocks.
 * @param rows Search result rows to format.
 * @returns Markdown text payload consumed by MCP clients.
 */
export function formatSearchResults(rows: SearchRow[]): string {
  return rows
    .map((row, index) => {
      const matchSource =
        row.similarity != null && (row.keyword_score || 0) > 0
          ? "hybrid"
          : row.similarity != null
            ? "semantic"
            : "keyword";

      return [
        `### Result ${index + 1} - ${row.file_path}:${row.start_line}-${row.end_line}`,
        row.similarity != null ? `**Similarity:** ${(row.similarity * 100).toFixed(1)}%` : "",
        row.keyword_score ? `**Keyword Score:** ${row.keyword_score}` : "",
        `**Match:** ${matchSource}`,
        row.symbol_name ? `**Symbol:** ${row.symbol_type} \`${row.symbol_name}\`` : "",
        row.intent ? `**Intent:** ${row.intent}` : "",
        row.intent_detail ? `**Description:** ${row.intent_detail}` : "",
        "```",
        row.content,
        "```",
      ]
        .filter(Boolean)
        .join("\n");
    })
    .join("\n\n---\n\n");
}

/**
 * @brief Formats symbol lookup rows as markdown entries.
 * @param rows Symbol lookup rows.
 * @returns Readable markdown summary with declaration metadata.
 */
export function formatSymbolResults(rows: SymbolRow[]): string {
  return rows
    .map((row) => [
      `**${row.kind}** \`${row.qualified_name || row.name}\``,
      `  File: ${row.file_path}:${row.start_line}-${row.end_line}`,
      row.container_symbol ? `  Container: \`${row.container_symbol}\`` : "",
      row.signature ? `  Signature: \`${row.signature}\`` : "",
      row.docstring ? `  Doc: ${row.docstring.slice(0, 200)}` : "",
      row.is_primary_declaration ? "  [primary] Primary declaration" : "  [secondary] Secondary declaration",
      row.declared_in_extension ? "  [extension] Declared in extension" : "",
      row.is_exported ? "  [exported] Exported" : "",
    ]
      .filter(Boolean)
      .join("\n"))
    .join("\n\n");
}

/**
 * @brief Formats symbol reference rows as markdown entries.
 * @param rows Reference rows.
 * @param name Target symbol name used in fallback output.
 * @returns Readable markdown reference list.
 */
export function formatReferenceResults(rows: ReferenceRow[], name: string): string {
  return (
    rows
      .map((row) => {
        const targets = row.target_paths?.length ? ` -> ${row.target_paths.join(", ")}` : "";
        return [
          `**${row.source_path}:${row.line_no}**`,
          row.source_symbol_name ? `  Source Symbol: \`${row.source_symbol_name}\`` : "",
          `  Kind: ${row.reference_kind}${targets}`,
        ]
          .filter(Boolean)
          .join("\n");
      })
      .join("\n\n") || `No references found for "${name}".`
  );
}
