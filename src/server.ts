/**
 * @file src/server.ts
 * @brief MCP server bootstrap and transport startup for stdio/http runtimes.
 */

import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";

import {
  MCP_ALLOWED_HOSTS,
  MCP_HTTP_HOST,
  MCP_HTTP_PORT,
  MCP_TRANSPORT,
} from "./config.js";
import { closePool, ensureSchema } from "./db.js";
import { registerResources } from "./mcp/resources.js";
import { registerTools } from "./mcp/tools.js";
import { registerWebRoutes } from "./web/routes.js";

/**
 * @brief Builds a configured MCP server instance with resources and tools.
 * @returns New MCP server.
 */
export function createServer(): McpServer {
  const server = new McpServer({
    name: "codebrain",
    version: "1.1.0",
  });

  registerResources(server);
  registerTools(server);
  return server;
}

/**
 * @brief Starts the server in stdio mode for MCP clients using local transport.
 * @returns Promise resolved when the stdio transport is attached.
 */
async function startStdioServer(): Promise<void> {
  const server = createServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("CodeBrain MCP server running (stdio)");
}

/**
 * @brief Starts standard SSE HTTP MCP transport plus semantic graph web UI routes.
 * @returns Promise that resolves once the HTTP listener is active.
 */
async function startHttpTransport(): Promise<void> {
  const app = createMcpExpressApp({
    host: MCP_HTTP_HOST,
    allowedHosts: MCP_ALLOWED_HOSTS,
  });
  registerWebRoutes(app);

  let transport: SSEServerTransport | undefined;

  app.get("/healthz", (_req: any, res: any) => {
    res.status(200).json({ ok: true });
  });

  app.get("/mcp", async (_req: any, res: any) => {
    const server = createServer();
    transport = new SSEServerTransport("/mcp/message", res);
    await server.connect(transport);
    console.error("[mcp] SSE session established");
  });

  // Compatibility for older Streamable HTTP clients that POST directly to the same endpoint
  app.post("/mcp", async (req: any, res: any) => {
    if (!transport) {
      res.status(400).json({ error: "No active SSE session" });
      return;
    }
    await transport.handlePostMessage(req, res);
  });

  app.post("/mcp/message", async (req: any, res: any) => {
    if (!transport) {
      res.status(400).json({ error: "No active SSE session" });
      return;
    }
    await transport.handlePostMessage(req, res);
  });

  const server = app.listen(MCP_HTTP_PORT, MCP_HTTP_HOST, () => {
    console.error(`CodeBrain MCP server running (http) at http://${MCP_HTTP_HOST}:${MCP_HTTP_PORT}/mcp`);
    console.error(`CodeBrain graph UI available at http://${MCP_HTTP_HOST}:${MCP_HTTP_PORT}/ui`);
  });

  const shutdown = async (signal: string) => {
    console.error(`Received ${signal}, shutting down...`);
    if (transport) {
      await transport.close().catch(() => undefined);
    }
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await closePool().catch(() => undefined);
    process.exit(0);
  };

  process.once("SIGINT", () => {
    void shutdown("SIGINT");
  });
  process.once("SIGTERM", () => {
    void shutdown("SIGTERM");
  });
}

/**
 * @brief Ensures schema patches and starts the configured transport mode.
 * @returns Promise resolved after startup completes.
 */
export async function startServer(): Promise<void> {
  await ensureSchema();
  if (MCP_TRANSPORT === "stdio") {
    await startStdioServer();
    return;
  }
  await startHttpTransport();
}
