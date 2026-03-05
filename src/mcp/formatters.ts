/**
 * @file src/mcp/formatters.ts
 * @brief Text formatting helpers for MCP tool responses.
 */

import type {
  CouplingEdgeRow,
  ModuleInterfaceRow,
  ReferenceRow,
  SearchRow,
  SeamRow,
  SymbolRow,
} from "./types.js";

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

/* ------------------------------------------------------------------ */
/*  Refactoring analysis formatters                                   */
/* ------------------------------------------------------------------ */

/**
 * @brief Formats coupling analysis results into a markdown report.
 * @param pathPrefix The subsystem path prefix analyzed.
 * @param repo Repository name.
 * @param rows Cross-boundary coupling edges.
 * @param internalFileCount Number of files inside the subsystem.
 * @param topN Maximum coupling pairs to show.
 * @returns Markdown coupling report.
 */
export function formatCouplingAnalysis(
  pathPrefix: string,
  repo: string,
  rows: CouplingEdgeRow[],
  internalFileCount: number,
  topN: number,
): string {
  const outbound = rows.filter((r) => r.direction === "outbound" || r.direction === "ref_outbound");
  const inbound = rows.filter((r) => r.direction === "inbound" || r.direction === "ref_inbound");

  const totalOutbound = outbound.reduce((s, r) => s + r.edge_count, 0);
  const totalInbound = inbound.reduce((s, r) => s + r.edge_count, 0);

  const ceFiles = new Set(outbound.map((r) => r.external_path));
  const caFiles = new Set(inbound.map((r) => r.external_path));
  const ce = ceFiles.size;
  const ca = caFiles.size;
  const instability = ca + ce > 0 ? ce / (ca + ce) : 0;
  const couplingRatio = internalFileCount > 0 ? ((totalInbound + totalOutbound) / internalFileCount).toFixed(1) : "0";

  const lines = [
    `# Coupling Analysis: ${pathPrefix}`,
    "",
    `**Repository:** ${repo}`,
    `**Internal files:** ${internalFileCount}`,
    `**Outbound edges (Ce):** ${totalOutbound} across ${ce} external files`,
    `**Inbound edges (Ca):** ${totalInbound} across ${ca} external files`,
    `**Instability (Ce/(Ca+Ce)):** ${instability.toFixed(2)} (${instability > 0.7 ? "volatile -- easy to change" : instability < 0.3 ? "stable -- heavily depended upon" : "moderate"})`,
    `**Coupling ratio:** ${couplingRatio} edges per internal file`,
    "",
  ];

  // Top coupling pairs
  const sorted = [...rows].sort((a, b) => b.edge_count - a.edge_count).slice(0, topN);
  if (sorted.length > 0) {
    lines.push("## Top Coupling Points", "");
    lines.push("| Direction | Internal File | External File | Kind | Edges |");
    lines.push("|-----------|--------------|---------------|------|-------|");
    for (const row of sorted) {
      lines.push(`| ${row.direction} | ${row.internal_path} | ${row.external_path} | ${row.kind} | ${row.edge_count} |`);
    }
    lines.push("");
  }

  // Coupling by kind
  const byKind = new Map<string, { inbound: number; outbound: number }>();
  for (const row of rows) {
    const entry = byKind.get(row.kind) || { inbound: 0, outbound: 0 };
    if (row.direction.includes("inbound")) entry.inbound += row.edge_count;
    else entry.outbound += row.edge_count;
    byKind.set(row.kind, entry);
  }
  if (byKind.size > 0) {
    lines.push("## Coupling by Kind", "");
    for (const [kind, counts] of byKind) {
      lines.push(`- **${kind}:** ${counts.outbound} outbound, ${counts.inbound} inbound`);
    }
  }

  return lines.join("\n");
}

/**
 * @brief Formats module interface extraction results.
 * @param pathPrefix The module path prefix.
 * @param rows Interface symbol rows with consumer information.
 * @param includeUnused Whether over-exposed symbols are included.
 * @returns Markdown interface report.
 */
export function formatModuleInterface(
  pathPrefix: string,
  rows: ModuleInterfaceRow[],
  includeUnused: boolean,
): string {
  const active = rows.filter((r) => r.total_refs != null && r.total_refs > 0);
  const unused = rows.filter((r) => r.total_refs == null || r.total_refs === 0);

  const lines = [
    `# Module Interface: ${pathPrefix}`,
    "",
    `**Active boundary symbols (referenced externally):** ${active.length}`,
  ];

  if (includeUnused) {
    lines.push(`**Over-exposed symbols (exported but unused externally):** ${unused.length}`);
  }

  lines.push("");

  if (active.length > 0) {
    lines.push("## Active Interface", "");
    for (const row of active) {
      const consumers = row.consumer_files || [];
      lines.push(`### ${row.kind} \`${row.qualified_name || row.name}\` (${row.file_path}:${row.start_line}-${row.end_line})`);
      if (row.signature) lines.push(`  Signature: \`${row.signature}\``);
      lines.push(`  Consumers: ${row.total_refs} references from ${consumers.length} files`);
      if (row.ref_kinds?.length) lines.push(`  Used as: ${row.ref_kinds.join(", ")}`);
      if (consumers.length > 0) lines.push(`  Consumer files: ${consumers.slice(0, 10).join(", ")}${consumers.length > 10 ? ` (+${consumers.length - 10} more)` : ""}`);
      lines.push("");
    }
  }

  if (includeUnused && unused.length > 0) {
    lines.push("## Over-Exposed (exported but no external consumers)", "");
    for (const row of unused) {
      lines.push(`- ${row.kind} \`${row.qualified_name || row.name}\` (${row.file_path}:${row.start_line})`);
      if (row.signature) lines.push(`  Signature: \`${row.signature}\``);
    }
    lines.push("");
  }

  return lines.join("\n");
}

/**
 * @brief Formats dependency cycle detection results.
 * @param repo Repository name.
 * @param pathPrefix Scope prefix used.
 * @param cycles Array of cycles (each an array of file paths).
 * @param rankings Files ranked by cycle participation count.
 * @returns Markdown cycle report.
 */
export function formatCycles(
  repo: string,
  pathPrefix: string,
  cycles: string[][],
  rankings: Array<{ node: string; cycleCount: number }>,
): string {
  const lines = [
    `# Dependency Cycles in ${repo}`,
    "",
    `**Scope:** ${pathPrefix || "(entire repo)"}`,
    `**Cycles found:** ${cycles.length}`,
    "",
  ];

  for (let i = 0; i < cycles.length; i++) {
    const cycle = cycles[i];
    const label = cycle.length === 2 ? "Mutual dependency" : `length ${cycle.length}`;
    lines.push(`## Cycle ${i + 1} (${label})`);
    lines.push(`  ${cycle.join(" -> ")} -> ${cycle[0]}`);
    lines.push("");
  }

  if (rankings.length > 0) {
    lines.push("## Files by cycle participation", "");
    lines.push("| File | Cycles |");
    lines.push("|------|--------|");
    for (const { node, cycleCount } of rankings.slice(0, 20)) {
      lines.push(`| ${node} | ${cycleCount} |`);
    }
  }

  return lines.join("\n");
}

/**
 * @brief Formats modularization seam analysis results.
 * @param pathPrefix The module path prefix to extract.
 * @param requiredInterface Symbols that external code calls into (must preserve).
 * @param dependencies External symbols the module needs (must inject).
 * @param seams Cross-boundary reference edges.
 * @param internalFileCount Number of files in the module.
 * @returns Markdown seam report.
 */
export function formatModularizationSeams(
  pathPrefix: string,
  requiredInterface: ModuleInterfaceRow[],
  dependencies: SeamRow[],
  seams: SeamRow[],
  internalFileCount: number,
): string {
  // Deduplicate dependencies by symbol name
  const depMap = new Map<string, SeamRow>();
  for (const dep of dependencies) {
    const existing = depMap.get(dep.symbol_name);
    if (!existing || dep.usage_count > existing.usage_count) {
      depMap.set(dep.symbol_name, dep);
    }
  }
  const uniqueDeps = Array.from(depMap.values()).sort((a, b) => b.usage_count - a.usage_count);

  const totalSeams = seams.reduce((s, r) => s + r.usage_count, 0);
  const difficulty =
    requiredInterface.length > 10 || uniqueDeps.length > 10 || totalSeams > 50
      ? "HIGH"
      : requiredInterface.length > 5 || uniqueDeps.length > 5 || totalSeams > 20
        ? "MODERATE"
        : "LOW";

  const lines = [
    `# Modularization Plan: ${pathPrefix}`,
    "",
    `**Internal files:** ${internalFileCount}`,
    "",
  ];

  // Section 1: Required interfaces
  lines.push("## 1. Required Interfaces (what consumers need from this module)", "");
  if (requiredInterface.length === 0) {
    lines.push("No external consumers found -- module may already be isolated.", "");
  } else {
    lines.push("| Symbol | Kind | Signature | Consumers | Total Refs |");
    lines.push("|--------|------|-----------|-----------|------------|");
    for (const row of requiredInterface) {
      const consumers = row.consumer_files?.length || 0;
      lines.push(`| \`${row.name}\` | ${row.kind} | \`${row.signature || "n/a"}\` | ${consumers} files | ${row.total_refs || 0} |`);
    }
    lines.push("");
  }

  // Section 2: Dependencies to inject
  lines.push("## 2. Dependencies to Inject (what this module needs from outside)", "");
  if (uniqueDeps.length === 0) {
    lines.push("No external dependencies found -- module is self-contained.", "");
  } else {
    lines.push("| Symbol | Kind | Provider File | Usage Count |");
    lines.push("|--------|------|--------------|-------------|");
    for (const dep of uniqueDeps.slice(0, 30)) {
      lines.push(`| \`${dep.symbol_name}\` | ${dep.symbol_kind || "unknown"} | ${dep.external_file} | ${dep.usage_count} |`);
    }
    if (uniqueDeps.length > 30) {
      lines.push(`| ... | | | (+${uniqueDeps.length - 30} more) |`);
    }
    lines.push("");
  }

  // Section 3: Seam edges
  lines.push("## 3. Seams to Cut (cross-boundary reference edges)", "");
  if (seams.length === 0) {
    lines.push("No cross-boundary edges found.", "");
  } else {
    lines.push("| Direction | Internal File | External File | Symbol | Kind | Count |");
    lines.push("|-----------|--------------|---------------|--------|------|-------|");
    const topSeams = [...seams].sort((a, b) => b.usage_count - a.usage_count).slice(0, 40);
    for (const s of topSeams) {
      lines.push(`| ${s.direction} | ${s.internal_file} | ${s.external_file} | \`${s.symbol_name}\` | ${s.reference_kind} | ${s.usage_count} |`);
    }
    if (seams.length > 40) {
      lines.push(`| ... | | | | | (+${seams.length - 40} more) |`);
    }
    lines.push("");
  }

  // Summary
  lines.push("## Summary", "");
  lines.push(`- **Boundary surface:** ${requiredInterface.length} symbols must be preserved as public interface`);
  lines.push(`- **Injection points:** ${uniqueDeps.length} external dependencies need adapters or DI`);
  lines.push(`- **Total seams:** ${totalSeams} cross-boundary reference edges`);
  lines.push(`- **Estimated difficulty:** ${difficulty}`);

  return lines.join("\n");
}
