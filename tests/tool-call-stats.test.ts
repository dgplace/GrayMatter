/**
 * @file tests/tool-call-stats.test.ts
 * @brief Unit tests for in-memory MCP tool call aggregation.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  getToolCallSnapshot,
  recordToolCall,
  resetToolCallStats,
} from "../src/mcp/toolCallStats.ts";

test("tool call stats aggregate counts and order by highest count", () => {
  resetToolCallStats();

  recordToolCall("semantic_search");
  recordToolCall("find_symbol");
  recordToolCall("semantic_search");

  const snapshot = getToolCallSnapshot();
  assert.equal(snapshot.total_calls, 3);
  assert.equal(snapshot.tool_calls.length, 2);
  assert.deepEqual(snapshot.tool_calls[0], { name: "semantic_search", count: 2 });
  assert.deepEqual(snapshot.tool_calls[1], { name: "find_symbol", count: 1 });
  assert.ok(snapshot.updated_at);
});

test("tool call stats ignore empty names", () => {
  resetToolCallStats();

  recordToolCall("");

  const snapshot = getToolCallSnapshot();
  assert.equal(snapshot.total_calls, 0);
  assert.equal(snapshot.tool_calls.length, 0);
  assert.equal(snapshot.updated_at, null);
});
