#!/usr/bin/env node
/**
 * @file index.ts
 * @brief CodeBrain MCP process entrypoint and utility re-exports for tests.
 */

import { pathToFileURL } from "node:url";

import { vecLiteral } from "./src/embed.js";
import { summarizeArgs } from "./src/mcp/logging.js";
import { extractKeywordTerms } from "./src/mcp/search.js";
import { startServer } from "./src/server.js";

export { extractKeywordTerms, summarizeArgs, vecLiteral };

/**
 * @brief Determines whether this module was launched directly by Node.
 * @returns True when this file is the active entrypoint script.
 */
function isEntrypointModule(): boolean {
  return process.argv[1] ? import.meta.url === pathToFileURL(process.argv[1]).href : false;
}

if (isEntrypointModule()) {
  startServer().catch((error) => {
    console.error("Fatal server startup error:", error);
    process.exit(1);
  });
}
