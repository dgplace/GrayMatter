/**
 * @file src/mcp/resources.ts
 * @brief MCP resource registration for usage guidance.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const CODEBRAIN_USAGE_URI = "codebrain://usage";
const CODEBRAIN_USAGE_TEXT = [
  "# CodeBrain Usage",
  "",
  "CodeBrain query tools now require explicit repository scope.",
  "",
  "Recommended workflow:",
  "1. Start with `list_repositories` to discover indexed repos.",
  "2. Use `codebase_stats` with a `repo` value for scoped metrics.",
  "3. Use `exact_symbol_search` for exact identifier lookups.",
  "4. Use `find_symbol` when you know part of a name.",
  "5. Use `find_references` for concrete usage sites.",
  "6. Use `get_file_map` before opening many files.",
  "7. Use `trace_dependencies` before manually following call chains.",
  "8. Use `get_intent` before editing a file you have not read yet.",
  "9. Use `semantic_search` when the exact name is unknown.",
  "",
  "Search tips:",
  "- Keep queries short and technical.",
  "- Include framework names or APIs when relevant.",
  "- If semantic retrieval is weak, lower threshold or retry with tighter terms.",
  "",
  "Reliability rules:",
  "- Always inspect source files before editing.",
  "- Treat CodeBrain results as discovery guidance, not ground truth.",
].join("\n");

/**
 * @brief Registers usage documentation as an MCP resource.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerResources(server: McpServer): void {
  server.registerResource(
    "usage",
    CODEBRAIN_USAGE_URI,
    {
      title: "CodeBrain Usage",
      description: "Read this first for the recommended CodeBrain workflow and search strategy.",
      mimeType: "text/markdown",
    },
    async (uri) => ({
      contents: [
        {
          uri: uri.toString(),
          mimeType: "text/markdown",
          text: CODEBRAIN_USAGE_TEXT,
        },
      ],
    }),
  );
}
