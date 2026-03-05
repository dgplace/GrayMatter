/**
 * @file src/server.ts
 * @brief MCP server bootstrap and transport startup for stdio/http runtimes.
 */

import { randomUUID } from "node:crypto";
import { createServer as createHttpServer } from "node:http";

import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";

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
 * @brief Starts streamable HTTP MCP transport plus semantic graph web UI routes.
 * @returns Promise that resolves once the HTTP listener is active.
 */
async function startHttpTransport(): Promise<void> {
  const app = createMcpExpressApp({
    host: MCP_HTTP_HOST,
    allowedHosts: MCP_ALLOWED_HOSTS,
  });
  registerWebRoutes(app);

  const sessions: Record<string, { server: McpServer; transport: StreamableHTTPServerTransport }> = {};
  const listener = createHttpServer(app);

  app.get("/healthz", (_req: any, res: any) => {
    res.status(200).json({ ok: true });
  });

  app.all("/mcp", async (req: any, res: any) => {
    try {
      const header = req.headers["mcp-session-id"];
      const sessionId = Array.isArray(header) ? header[0] : header;
      const session = sessionId ? sessions[sessionId] : undefined;
      const rpcMethod = req.body?.method;
      const requestedTool = rpcMethod === "tools/call" ? req.body?.params?.name : undefined;

      console.error(
        `[mcp] request method=${req.method} rpc=${rpcMethod || "unknown"} session=${sessionId || "new"}${requestedTool ? ` tool=${requestedTool}` : ""}`,
      );

      if (session) {
        await session.transport.handleRequest(req, res, req.body);
        return;
      }

      if (!sessionId && req.method === "POST" && isInitializeRequest(req.body)) {
        const server = createServer();
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          onsessioninitialized: (newSessionId) => {
            sessions[newSessionId] = { server, transport };
          },
        });

        transport.onclose = () => {
          const activeSessionId = transport.sessionId;
          if (activeSessionId && sessions[activeSessionId]) {
            delete sessions[activeSessionId];
          }
        };
        transport.onerror = (error) => {
          console.error("MCP transport error:", error);
        };

        await server.connect(transport);
        await transport.handleRequest(req, res, req.body);
        return;
      }

      if (sessionId) {
        res.status(404).json({
          jsonrpc: "2.0",
          error: {
            code: -32001,
            message: "Session not found",
          },
          id: null,
        });
        return;
      }

      res.status(400).json({
        jsonrpc: "2.0",
        error: {
          code: -32000,
          message: "Bad Request: initialize via POST /mcp first",
        },
        id: null,
      });
    } catch (error) {
      console.error("Error handling MCP request:", error);
      if (!res.headersSent) {
        res.status(500).json({
          jsonrpc: "2.0",
          error: {
            code: -32603,
            message: "Internal server error",
          },
          id: null,
        });
      }
    }
  });

  await new Promise<void>((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(MCP_HTTP_PORT, MCP_HTTP_HOST, () => {
      listener.off("error", reject);
      console.error(`CodeBrain MCP server running (http) at http://${MCP_HTTP_HOST}:${MCP_HTTP_PORT}/mcp`);
      console.error(`CodeBrain graph UI available at http://${MCP_HTTP_HOST}:${MCP_HTTP_PORT}/ui`);
      resolve();
    });
  });

  const shutdown = async (signal: string) => {
    console.error(`Received ${signal}, shutting down...`);
    for (const [sessionId, session] of Object.entries(sessions)) {
      delete sessions[sessionId];
      await session.server.close().catch(() => undefined);
    }
    await new Promise<void>((resolve) => listener.close(() => resolve()));
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
