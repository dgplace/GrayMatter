/**
 * @file src/mcp/toolCallStats.ts
 * @brief In-memory aggregation helpers for MCP tool invocation counts.
 */

/**
 * @brief Public snapshot row for a single MCP tool invocation counter.
 */
export type ToolCallCounter = {
  name: string;
  count: number;
};

const toolCallCounts = new Map<string, number>();
let totalToolCalls = 0;
let lastUpdatedEpochMs: number | null = null;

/**
 * @brief Records a single MCP tool invocation for dashboard reporting.
 * @param name Tool function name.
 * @returns Void.
 */
export function recordToolCall(name: string): void {
  if (!name) {
    return;
  }

  totalToolCalls += 1;
  toolCallCounts.set(name, (toolCallCounts.get(name) || 0) + 1);
  lastUpdatedEpochMs = Date.now();
}

/**
 * @brief Returns aggregated tool call counts sorted by highest usage.
 * @returns Serializable snapshot for the UI polling endpoint.
 */
export function getToolCallSnapshot(): {
  total_calls: number;
  updated_at: string | null;
  tool_calls: ToolCallCounter[];
} {
  const toolCalls = Array.from(toolCallCounts.entries())
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

  return {
    total_calls: totalToolCalls,
    updated_at: lastUpdatedEpochMs ? new Date(lastUpdatedEpochMs).toISOString() : null,
    tool_calls: toolCalls,
  };
}

/**
 * @brief Clears in-memory counters used by tests.
 * @returns Void.
 */
export function resetToolCallStats(): void {
  toolCallCounts.clear();
  totalToolCalls = 0;
  lastUpdatedEpochMs = null;
}
