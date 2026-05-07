/**
 * @file tests/index.test.ts
 * @brief Unit tests for pure MCP utility helpers.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { extractKeywordTerms, summarizeArgs, vecLiteral } from "../index.ts";

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

test("db schema patches include resolved reference migration columns and indexes", () => {
  const dbSource = readFileSync(new URL("../src/db.ts", import.meta.url), "utf8");

  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS target_symbol_id INTEGER REFERENCES symbols\(id\) ON DELETE SET NULL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_confidence REAL/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS resolution_method TEXT/);
  assert.match(dbSource, /ADD COLUMN IF NOT EXISTS reference_kind_v2 TEXT/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind/);
  assert.match(dbSource, /CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name/);
});
