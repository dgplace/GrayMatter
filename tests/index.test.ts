/**
 * @file tests/index.test.ts
 * @brief Unit tests for pure MCP utility helpers.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { extractKeywordTerms, summarizeArgs, vecLiteral } from "../index.ts";
import { formatCouplingAnalysis, formatModularizationSeams, formatReferenceResults, formatSearchResults } from "../src/mcp/formatters.ts";
import type { CouplingEdgeRow, ModuleInterfaceRow, ReferenceRow, SearchRow, SeamRow } from "../src/mcp/types.ts";

/**
 * @brief Loads MCP tool sources across split registry modules for source-level assertions.
 * @returns Concatenated source text of MCP tool registration files.
 */
function readMcpToolSource(): string {
  const toolSourceFiles = [
    "../src/mcp/tools.ts",
    "../src/mcp/tooling/shared.ts",
    "../src/mcp/tooling/repoSearchTools.ts",
    "../src/mcp/tooling/hierarchyTools.ts",
    "../src/mcp/tooling/dependencyTraceTools.ts",
    "../src/mcp/tooling/architectureTools.ts",
    "../src/mcp/tooling/indexManagementTools.ts",
    "../src/repositories/store.ts",
  ];
  return toolSourceFiles.map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
}

test("extractKeywordTerms removes stopwords, deduplicates, and caps results", () => {
  const terms = extractKeywordTerms(
    "How to configure toolbar toolbar styling for swift navigation bar TrackService PhotoService MapKit Logger",
  );

  assert.deepEqual(terms, [
    "configure",
    "toolbar",
    "styling",
    "swift",
    "navigation",
    "trackservice",
  ]);
});

test("summarizeArgs truncates long strings and preserves non-string values", () => {
  const summary = summarizeArgs({
    query: "x".repeat(130),
    limit: 10,
    exact: true,
  });

  assert.match(summary, /^query="x{117}\.\.\.", limit=10, exact=true$/);
});

test("vecLiteral formats vectors for SQL vector literals", () => {
  assert.equal(vecLiteral([1, 2.5, 3]), "[1,2.5,3]");
});

test("formatReferenceResults surfaces resolution_confidence and resolution_method per row", () => {
  const rows: ReferenceRow[] = [
    {
      source_path: "src/a.ts",
      line_no: 10,
      reference_kind: "call",
      source_symbol_name: "caller",
      target_paths: ["src/b.ts"],
      resolution_confidence: 1,
      resolution_method: "scip_typescript_exact",
    },
    {
      source_path: "src/c.ts",
      line_no: 5,
      reference_kind: "ref",
      source_symbol_name: null,
      target_paths: null,
      resolution_confidence: 0.55,
      resolution_method: "heuristic_name",
    },
  ];

  const text = formatReferenceResults(rows, "doSomething");
  assert.match(text, /confidence: 1\.00, scip_typescript_exact/);
  assert.match(text, /confidence: 0\.55, heuristic_name/);
  assert.match(text, /-> src\/b\.ts/);
});

test("formatReferenceResults omits confidence suffix when no resolution metadata is present", () => {
  const rows: ReferenceRow[] = [
    {
      source_path: "src/a.ts",
      line_no: 1,
      reference_kind: "call",
      source_symbol_name: "caller",
      target_paths: null,
      resolution_confidence: null,
      resolution_method: null,
    },
  ];

  const text = formatReferenceResults(rows, "doSomething");
  assert.doesNotMatch(text, /confidence:/);
  assert.doesNotMatch(text, /\(\)/);
});

test("formatCouplingAnalysis coerces postgres count strings before totals and ratios", () => {
  const rows: CouplingEdgeRow[] = [
    { direction: "outbound", internal_path: "src/a.ts", external_path: "src/b.ts", kind: "import", edge_count: "2" as unknown as number },
    { direction: "inbound", internal_path: "src/a.ts", external_path: "src/c.ts", kind: "call", edge_count: "1" as unknown as number },
  ];

  const text = formatCouplingAnalysis("src/", "CodeBrain", rows, 2, 10);
  assert.match(text, /\*\*Outbound edges \(Ce\):\*\* 2 across 1 external files/);
  assert.match(text, /\*\*Inbound edges \(Ca\):\*\* 1 across 1 external files/);
  assert.match(text, /\*\*Coupling ratio:\*\* 1\.5 edges per internal file/);
});

test("formatModularizationSeams coerces usage-count strings before seam totals", () => {
  const requiredInterface: ModuleInterfaceRow[] = [];
  const dependencies: SeamRow[] = [
    {
      direction: "outbound",
      internal_file: "src/a.ts",
      external_file: "src/b.ts",
      symbol_name: "depA",
      symbol_kind: "function",
      signature: null,
      reference_kind: "call",
      usage_count: "2" as unknown as number,
    },
  ];
  const seams: SeamRow[] = [
    {
      direction: "outbound",
      internal_file: "src/a.ts",
      external_file: "src/b.ts",
      symbol_name: "depA",
      symbol_kind: "function",
      signature: null,
      reference_kind: "call",
      usage_count: "2" as unknown as number,
    },
    {
      direction: "inbound",
      internal_file: "src/a.ts",
      external_file: "src/c.ts",
      symbol_name: "apiA",
      symbol_kind: "function",
      signature: null,
      reference_kind: "call",
      usage_count: "3" as unknown as number,
    },
  ];

  const text = formatModularizationSeams("src/", requiredInterface, dependencies, seams, 1);
  assert.match(text, /- \*\*Total seams:\*\* 5 cross-boundary reference edges/);
});

test("find_references tool exposes confidence threshold knobs and prefers resolved target_symbol_id", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /reference_kind:\s*z\s*\.\s*enum\(\["call",\s*"member_call",\s*"type_reference",\s*"instantiation"\]\)/);
  assert.match(toolsSource, /min_confidence:\s*z\s*\.\s*number/);
  assert.match(toolsSource, /include_unresolved:\s*z\s*\.\s*boolean/);
  assert.match(toolsSource, /COALESCE\(sr\.reference_kind_v2,\s*sr\.reference_kind\)\s+AS\s+reference_kind/);
  assert.match(toolsSource, /\(\$7::text IS NULL OR COALESCE\(sr\.reference_kind_v2,\s*sr\.reference_kind\) = \$7\)/);
  assert.match(toolsSource, /COALESCE\(sr\.resolution_confidence, 0\) >= \$4/);
  assert.match(toolsSource, /LEFT JOIN symbols rs ON rs\.id = sr\.target_symbol_id/);
  assert.match(toolsSource, /sr\.target_symbol_id IS NULL AND lower\(s\.name\) = lower\(sr\.target_name\)/);
  assert.match(toolsSource, /COALESCE\(rs_file\.path, tf\.path\)/);
});

test("find_supertypes and find_subtypes tools walk symbol_relationships with depth and unresolved support", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_supertypes"/);
  assert.match(toolsSource, /"find_subtypes"/);
  assert.match(toolsSource, /WITH RECURSIVE start_symbols AS[\s\S]*supertype_tree AS/);
  assert.match(toolsSource, /WITH RECURSIVE start_symbols AS[\s\S]*root_targets AS[\s\S]*subtype_tree AS/);
  assert.match(toolsSource, /relationship_kind IN \('extends', 'implements'\)/);
  assert.match(toolsSource, /st\.depth < \$3/);
  assert.match(
    toolsSource,
    /sr\.target_symbol_id IS NULL\s*\n\s*AND lower\(sr\.target_name\) = lower\(rt\.root_symbol_name\)/,
  );
});

test("find_implementations tool supports unresolved roots and walks implements/extends edges", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_implementations"/);
  assert.match(toolsSource, /WITH RECURSIVE start_symbols AS[\s\S]*root_targets AS[\s\S]*implementation_tree AS/);
  assert.match(toolsSource, /NOT EXISTS \(SELECT 1 FROM start_symbols\)/);
  assert.match(toolsSource, /sr\.relationship_kind IN \('implements', 'extends'\)/);
  assert.match(toolsSource, /No implementations found for/);
  assert.match(toolsSource, /impl_file\.path AS implementer_path/);
  assert.match(toolsSource, /impl\.start_line AS implementer_start_line/);
  assert.match(toolsSource, /impl\.end_line AS implementer_end_line/);
  assert.match(toolsSource, /\[\$\{relationshipKind\}\]/);
});

test("find_call_graph tool supports forward and reverse traversal with depth bounds and cycle guards", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_call_graph"/);
  assert.doesNotMatch(toolsSource, /"call_graph"/);
  assert.match(toolsSource, /direction:\s*z\s*\.\s*enum\(\["forward",\s*"reverse"\]\)/);
  assert.match(toolsSource, /depth:\s*z\s*\.\s*number\(\)\.int\(\)\.min\(1\)\.max\(8\)/);
  assert.match(toolsSource, /resolved_call_edges AS/);
  assert.match(toolsSource, /LEFT JOIN LATERAL \(/);
  assert.match(toolsSource, /lower\(s\.name\) = lower\(sr\.source_symbol_name\)/);
  assert.match(toolsSource, /ct\.depth < \$3/);
  assert.match(toolsSource, /AND NOT rce\.to_symbol_id = ANY\(ct\.walk_path\)/);
  assert.match(toolsSource, /AND NOT rce\.from_symbol_id = ANY\(ct\.walk_path\)/);
  assert.match(toolsSource, /COALESCE\(sr\.reference_kind_v2,\s*sr\.reference_kind\) IN \('call', 'member_call', 'instantiation'\)/);
});

test("find_instantiations tool filters instantiation references and returns source + containing symbol context", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_instantiations"/);
  assert.match(toolsSource, /s\.kind IN \('class', 'struct'\)/);
  assert.match(toolsSource, /COALESCE\(sr\.reference_kind_v2,\s*sr\.reference_kind\) = 'instantiation'/);
  assert.match(toolsSource, /ss\.name AS containing_symbol_name/);
  assert.match(toolsSource, /LEFT JOIN LATERAL \(/);
  assert.match(toolsSource, /lower\(s\.name\) = lower\(sr\.source_symbol_name\)/);
  assert.match(toolsSource, /No class symbol matches found for/);
  assert.match(toolsSource, /No instantiations found for/);
});

test("trace_dependencies deduplicates rows before ordering to avoid DISTINCT+ORDER BY postgres errors", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /dedup_rows AS \(/);
  assert.match(toolsSource, /SELECT DISTINCT[\s\S]*FROM dep_tree/);
  assert.match(toolsSource, /FROM dedup_rows[\s\S]*ORDER BY[\s\S]*CASE dep_kind/);
  assert.doesNotMatch(toolsSource, /WHERE dt\.depth < \$4\s*\)\s*\),\s*dedup_rows AS \(/);
  assert.match(toolsSource, /LEFT JOIN symbols resolved_symbol ON resolved_symbol\.id = sr\.target_symbol_id/);
  assert.match(toolsSource, /COALESCE\([\s\S]*resolved_file\.id[\s\S]*fallback_file\.id[\s\S]*\) AS target_file_id/);
  assert.match(toolsSource, /\(COALESCE\(source_file\.language, ''\) = COALESCE\(tf\.language, ''\)\)/);
  assert.doesNotMatch(toolsSource, /target_symbol_names AS \(/);
});

test("db schema patches include resolved reference migration columns and indexes", () => {
  const dbSource = readFileSync(new URL("../src/db.ts", import.meta.url), "utf8");

  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE SET NULL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_confidence REAL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_method TEXT/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_reverse_lookup/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS symbol_relationships/);
  assert.match(dbSource, /source_symbol_id INTEGER NOT NULL REFERENCES symbols\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /target_symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE SET NULL/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_rels_reverse_lookup/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_deps_target_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_deps_reverse_lookup/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS dependency_cycles/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_dependency_cycles_repo ON dependency_cycles/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS clusters/);
  assert.match(dbSource, /cluster_key TEXT NOT NULL/);
  assert.match(dbSource, /modularity REAL NOT NULL DEFAULT 0/);
  assert.match(dbSource, /embedding vector\(768\)/);
  assert.match(dbSource, /ALTER TABLE clusters ADD COLUMN IF NOT EXISTS modularity REAL NOT NULL DEFAULT 0/);
  assert.match(dbSource, /ALTER TABLE clusters ADD COLUMN IF NOT EXISTS embedding vector\(768\)/);
  assert.match(dbSource, /granularity TEXT NOT NULL CHECK \(granularity IN \('symbol', 'file'\)\)/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_clusters_embedding ON clusters USING ivfflat/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS cluster_members/);
  assert.match(dbSource, /cluster_id INTEGER NOT NULL REFERENCES clusters\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /file_id INTEGER REFERENCES files\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_symbol_unique/);
  assert.match(dbSource, /CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_members_file_unique/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS flows/);
  assert.match(dbSource, /flow_key TEXT NOT NULL/);
  assert.match(dbSource, /dominant_intent TEXT/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_flows_repo ON flows/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS flow_members/);
  assert.match(dbSource, /flow_id INTEGER NOT NULL REFERENCES flows\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /symbol_id INTEGER NOT NULL REFERENCES symbols\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_members_symbol_unique/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS doc_links/);
  assert.match(dbSource, /embedding vector\(768\) NOT NULL/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_doc_links_target ON doc_links\(target_kind, target_id\)/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS ingestion_diagnostics/);
  assert.match(dbSource, /diagnostic_kind TEXT NOT NULL/);
  assert.match(dbSource, /affected_file_count INTEGER NOT NULL DEFAULT 0/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_ingestion_diagnostics_repo_kind ON ingestion_diagnostics/);
  assert.match(dbSource, /CREATE OR REPLACE FUNCTION impact_of/);
  assert.match(dbSource, /min_confidence\s+REAL DEFAULT 0\.55/);
});

test("find_cycles tool reads persisted dependency_cycles rows for a repository and supports path_prefix filtering", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_cycles"/);
  assert.doesNotMatch(toolsSource, /"find_dependency_cycles"/);
  assert.match(toolsSource, /FROM dependency_cycles/);
  assert.match(toolsSource, /WHERE repo = \$1/);
  assert.match(toolsSource, /FROM unnest\(member_paths\) AS member_path/);
  assert.match(toolsSource, /member_path LIKE \$2 \|\| '%'/);
  assert.match(toolsSource, /No dependency cycles found for repo/);
  assert.match(toolsSource, /member_file_ids/);
  assert.match(toolsSource, /member_paths/);
});

test("find_impact tool wraps SQL impact_of function with confidence-band output", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_impact"/);
  assert.doesNotMatch(toolsSource, /"impact_of"/);
  assert.match(toolsSource, /min_confidence:\s*z\s*\.\s*number\(\)\.min\(0\)\.max\(1\)\.optional\(\)/);
  assert.match(toolsSource, /depth:\s*z\s*\.\s*number\(\)\.int\(\)\.min\(1\)\.max\(8\)\.optional\(\)/);
  assert.match(toolsSource, /async \(\{ repo, symbol, depth = 5, min_confidence = 0\.55 \}\)/);
  assert.match(toolsSource, /FROM impact_of\(\$1, \$2, \$3\)/);
  assert.match(toolsSource, /Likely impact \(confidence >= 0\.75\)/);
  assert.match(toolsSource, /Possible impact \(0\.55 <= confidence < 0\.75\)/);
  assert.match(toolsSource, /distinct targets \(\$\{bandRows\.length\} edge occurrences\)/);
  assert.match(toolsSource, /occurrence_count/);
  assert.match(toolsSource, /occurrences \$\{row\.occurrence_count\}/);
  assert.match(toolsSource, /impactCategory/);
});

test("find_external_dependencies tool groups by external_module and external_version and supports package consumer lookup", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_external_dependencies"/);
  assert.match(toolsSource, /package_name:\s*z\s*\.\s*string\(\)\.optional\(\)/);
  assert.match(toolsSource, /normalized_external_deps AS/);
  assert.match(toolsSource, /normalized_module/);
  assert.match(toolsSource, /d\.external_module LIKE '\.\/%'/);
  assert.match(toolsSource, /d\.external_module LIKE 'node:%'/);
  assert.match(toolsSource, /GROUP BY ned\.normalized_module, COALESCE\(NULLIF\(ned\.external_version, ''\), '\(unknown\)'\)/);
  assert.match(toolsSource, /Consumers for package/);
  assert.match(toolsSource, /lower\(ned\.normalized_module\) = lower\(\$3\)/);
  assert.match(toolsSource, /include_stdlib:\s*z\s*\.\s*boolean\(\)\s*\.\s*optional\(\)/);
  assert.match(toolsSource, /isStdlibModule\(/);
});

test("PYTHON_STDLIB_MODULES includes posixpath, shutil, and tomllib so they stay out of external dependency reports", () => {
  const toolsSource = readMcpToolSource();
  const stdlibBlock = toolsSource.match(/const PYTHON_STDLIB_MODULES = new Set\(\[([\s\S]*?)\]\);/);
  assert.ok(stdlibBlock, "PYTHON_STDLIB_MODULES set declaration not found");
  const stdlibText = stdlibBlock[1];
  for (const expected of ["posixpath", "shutil", "tomllib"]) {
    assert.match(stdlibText, new RegExp(`"${expected}"`));
  }
});

test("NODE_STDLIB_AUGMENTATIONS keeps node:test and node:sqlite from leaking into external dependency reports", () => {
  const toolsSource = readMcpToolSource();
  const augBlock = toolsSource.match(/const NODE_STDLIB_AUGMENTATIONS = \[([\s\S]*?)\];/);
  assert.ok(augBlock, "NODE_STDLIB_AUGMENTATIONS declaration not found");
  for (const expected of ["test", "sqlite"]) {
    assert.match(augBlock[1], new RegExp(`"${expected}"`));
  }
});

test("find_external_dependencies filters first-party modules computed from indexed file paths", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /async function getFirstPartyModuleNames\(repo: string\): Promise<Set<string>>/);
  assert.match(toolsSource, /split_part\(f\.path, '\/', 1\)/);
  assert.match(toolsSource, /regexp_replace\(regexp_replace\(f\.path, '\.\*\/', ''\), '\\\\\.\[\^\.\]\+\$', ''\)/);
  assert.match(toolsSource, /function isFirstPartyModule\(/);
  assert.match(toolsSource, /isFirstPartyModule\(firstPartyModules,/);
  assert.match(toolsSource, /first-party filtering/);
});

test("semantic_search exposes include_documentation and filters documentation-intent rows by default", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"semantic_search"/);
  assert.match(toolsSource, /include_documentation:\s*z\s*\.\s*boolean\(\)\s*\.\s*optional\(\)/);
  assert.match(toolsSource, /include_documentation = false/);
  assert.match(toolsSource, /row\.intent !== DOCUMENTATION_INTENT/);
});

test("formatSearchResults truncates oversized chunk content to keep MCP responses under the token cap", () => {
  const longLine = "x".repeat(200);
  const oversizedContent = Array.from({ length: 200 }, () => longLine).join("\n");

  const row: SearchRow = {
    chunk_id: 1,
    file_path: "src/big.ts",
    language: "typescript",
    content: oversizedContent,
    symbol_name: "huge",
    symbol_type: "function",
    intent: "utility",
    intent_detail: null,
    start_line: 1,
    end_line: 200,
    similarity: 0.9,
    keyword_score: 0,
  };

  const formatted = formatSearchResults([row]);
  assert.match(formatted, /\[truncated\]/);
  assert.ok(
    formatted.length < oversizedContent.length,
    `expected truncated output (${formatted.length}) to be smaller than input (${oversizedContent.length})`,
  );

  const small: SearchRow = { ...row, content: "short body\nline two" };
  const smallFormatted = formatSearchResults([small]);
  assert.doesNotMatch(smallFormatted, /\[truncated\]/);
  assert.match(smallFormatted, /short body/);
  assert.match(smallFormatted, /line two/);
});

test("clusters tool returns required cluster fields including modularity and granularity", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"clusters"/);
  assert.match(toolsSource, /COUNT\(cm\.id\)::integer AS size/);
  assert.match(toolsSource, /c\.modularity/);
  assert.match(toolsSource, /c\.granularity/);
  assert.match(toolsSource, /No clusters found for repo/);
});

test("cluster_members tool resolves cluster selector and emits symbol-or-file member shapes with weights", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"cluster_members"/);
  assert.match(toolsSource, /Cluster selector: id, cluster_key, or cluster name/);
  assert.match(toolsSource, /\(\$2 ~ '\^\[0-9\]\+\$' AND id = \$2::int\)/);
  assert.match(toolsSource, /cm\.membership_weight/);
  assert.match(toolsSource, /JOIN symbols s ON s\.id = cm\.symbol_id/);
  assert.match(toolsSource, /JOIN files f ON f\.id = cm\.file_id/);
  assert.match(toolsSource, /was not found in repo/);
});

test("find_flows tool supports symbol-to-flow and flow-to-members lookup directions", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"find_flows"/);
  assert.match(toolsSource, /Exactly one selector must be set: `symbol` or `flow`/);
  assert.match(toolsSource, /Specify exactly one selector: `symbol` \(to list flow memberships\) or `flow` \(to list flow members\)/);
  assert.match(toolsSource, /Execution flows for/);
  assert.match(toolsSource, /Execution flow for/);
  assert.match(toolsSource, /FROM flow_members fm/);
  assert.match(toolsSource, /JOIN flows fl ON fl.id = fm\.flow_id/);
  assert.match(toolsSource, /JOIN symbols s ON s\.id = fm\.symbol_id/);
});

test("codebase_stats includes callback extractor-gap diagnostics", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /### Callback Extractor Gaps/);
  assert.match(toolsSource, /FROM ingestion_diagnostics/);
  assert.match(toolsSource, /diagnostic_kind = 'missing_extractor'/);
});

test("describe_node supports file/symbol/cluster kinds and includes linked doc_links rows", () => {
  const toolsSource = readMcpToolSource();

  assert.match(toolsSource, /"describe_node"/);
  assert.match(toolsSource, /kind:\s*z\.enum\(\["file", "symbol", "cluster"\]\)/);
  assert.match(toolsSource, /SELECT[\s\S]*source,[\s\S]*source_path,[\s\S]*content,[\s\S]*created_at[\s\S]*FROM doc_links/);
  assert.match(toolsSource, /WHERE repo = \$1[\s\S]*AND target_kind = \$2[\s\S]*AND target_id = \$3/);
  assert.match(toolsSource, /Unknown file node/);
  assert.match(toolsSource, /Unknown symbol node/);
  assert.match(toolsSource, /Unknown cluster node/);
  assert.match(toolsSource, /Linked Doc Links/);
});
