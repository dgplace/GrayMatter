/**
 * @file tests/index.test.ts
 * @brief Unit tests for pure MCP utility helpers.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { extractKeywordTerms, summarizeArgs, vecLiteral } from "../index.ts";
import { formatReferenceResults } from "../src/mcp/formatters.ts";
import type { ReferenceRow } from "../src/mcp/types.ts";

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

test("find_references tool exposes confidence threshold knobs and prefers resolved target_symbol_id", () => {
  const toolsSource = readFileSync(new URL("../src/mcp/tools.ts", import.meta.url), "utf8");

  assert.match(toolsSource, /min_confidence:\s*z\s*\.\s*number/);
  assert.match(toolsSource, /include_unresolved:\s*z\s*\.\s*boolean/);
  assert.match(toolsSource, /COALESCE\(sr\.resolution_confidence, 0\) >= \$4/);
  assert.match(toolsSource, /LEFT JOIN symbols rs ON rs\.id = sr\.target_symbol_id/);
  assert.match(toolsSource, /sr\.target_symbol_id IS NULL AND lower\(s\.name\) = lower\(sr\.target_name\)/);
  assert.match(toolsSource, /COALESCE\(rs_file\.path, tf\.path\)/);
});

test("find_supertypes and find_subtypes tools walk symbol_relationships with depth and unresolved support", () => {
  const toolsSource = readFileSync(new URL("../src/mcp/tools.ts", import.meta.url), "utf8");

  assert.match(toolsSource, /"find_supertypes"/);
  assert.match(toolsSource, /"find_subtypes"/);
  assert.match(toolsSource, /WITH start_symbols AS/);
  assert.match(toolsSource, /WITH start_symbols AS[\s\S]*subtype_tree AS/);
  assert.match(toolsSource, /relationship_kind IN \('extends', 'implements'\)/);
  assert.match(toolsSource, /st\.depth < \$3/);
  assert.match(toolsSource, /sr\.target_symbol_id IS NULL AND lower\(sr\.target_name\) = lower\(ss\.name\)/);
});

test("db schema patches include resolved reference migration columns and indexes", () => {
  const dbSource = readFileSync(new URL("../src/db.ts", import.meta.url), "utf8");

  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE SET NULL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_confidence REAL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_method TEXT/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name/);
  assert.match(dbSource, /CREATE TABLE IF NOT EXISTS symbol_relationships/);
  assert.match(dbSource, /source_symbol_id INTEGER NOT NULL REFERENCES symbols\(id\) ON DELETE CASCADE/);
  assert.match(dbSource, /target_symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE SET NULL/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_rels_source_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_rels_target_symbol/);
});
