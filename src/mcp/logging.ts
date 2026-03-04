/**
 * @file src/mcp/logging.ts
 * @brief MCP tool invocation logging helpers.
 */

/**
 * @brief Summarizes tool arguments for compact structured log lines.
 * @param args Tool argument object.
 * @returns Single-line key/value summary with long strings truncated.
 */
export function summarizeArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args).map(([key, value]) => {
    if (typeof value === "string") {
      const compact = value.length > 120 ? `${value.slice(0, 117)}...` : value;
      return `${key}=${JSON.stringify(compact)}`;
    }
    return `${key}=${JSON.stringify(value)}`;
  });

  return entries.join(", ");
}

/**
 * @brief Emits a standardized stderr log line for MCP tool calls.
 * @param name MCP tool name.
 * @param args Tool argument map.
 * @returns Void.
 */
export function logToolInvocation(name: string, args: Record<string, unknown> = {}): void {
  const summary = summarizeArgs(args);
  console.error(`[mcp] tool=${name}${summary ? ` args: ${summary}` : ""}`);
}
