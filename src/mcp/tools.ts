/**
 * @file src/mcp/tools.ts
 * @brief MCP tool registration composition layer.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { registerArchitectureTools } from "./tooling/architectureTools.js";
import { registerDependencyTraceTools } from "./tooling/dependencyTraceTools.js";
import { registerHierarchyTools } from "./tooling/hierarchyTools.js";
import { registerIndexManagementTools } from "./tooling/indexManagementTools.js";
import { registerRepoSearchTools } from "./tooling/repoSearchTools.js";

/**
 * @brief Registers all CodeBrain MCP tools.
 * @param server MCP server instance.
 * @returns Void.
 */
export function registerTools(server: McpServer): void {
  registerRepoSearchTools(server);
  registerHierarchyTools(server);
  registerDependencyTraceTools(server);
  registerArchitectureTools(server);
  registerIndexManagementTools(server);
}
