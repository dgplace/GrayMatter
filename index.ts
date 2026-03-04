#!/usr/bin/env node
/**
 * CodeBrain MCP Server
 *
 * Exposes local codebase intelligence over MCP (Streamable HTTP by default, stdio optional).
 * Tools: semantic_search, find_symbol, trace_dependencies, get_file_map, get_intent, codebase_stats
 */

import { randomUUID } from "node:crypto";
import { createServer as createHttpServer } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import pg from "pg";

const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://codebrain:codebrain_local@applepi3:5433/codebrain";

const EMBED_API_STYLE = (process.env.EMBED_API_STYLE || "ollama").toLowerCase();
const EMBED_BASE_URL = (
  process.env.EMBED_BASE_URL ||
  process.env.OLLAMA_URL ||
  (EMBED_API_STYLE === "ollama" ? "http://applepi3:11434" : "http://applepi3:11435")
).replace(/\/+$/, "");
const EMBED_MODEL = process.env.EMBED_MODEL || "nomic-embed-text";
const EMBED_DIMENSIONS = Number(process.env.EMBED_DIMENSIONS || "768");
const EMBED_API_KEY = process.env.EMBED_API_KEY;
const MCP_TRANSPORT = (process.env.MCP_TRANSPORT || "http").toLowerCase();
const MCP_HTTP_HOST = process.env.MCP_HTTP_HOST || "127.0.0.1";
const MCP_HTTP_PORT = Number(process.env.MCP_HTTP_PORT || "3001");
const MCP_ALLOWED_HOSTS = process.env.MCP_ALLOWED_HOSTS
  ? process.env.MCP_ALLOWED_HOSTS.split(",").map((host) => host.trim()).filter(Boolean)
  : undefined;

// ── Database ──────────────────────────────────────────────

const pool = new pg.Pool({ connectionString: DATABASE_URL });

async function query(text: string, params?: any[]) {
  const client = await pool.connect();
  try {
    return await client.query(text, params);
  } finally {
    client.release();
  }
}

// ── Embedding ─────────────────────────────────────────────

async function embed(text: string): Promise<number[]> {
  const endpoint = EMBED_API_STYLE === "openai" ? "/v1/embeddings" : "/api/embed";
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (EMBED_API_STYLE === "openai" && EMBED_API_KEY) {
    headers.Authorization = `Bearer ${EMBED_API_KEY}`;
  }

  const payload =
    EMBED_API_STYLE === "openai"
      ? {
          model: EMBED_MODEL,
          input: text,
          encoding_format: "float",
          dimensions: EMBED_DIMENSIONS,
        }
      : {
          model: EMBED_MODEL,
          input: text,
        };

  const res = await fetch(`${EMBED_BASE_URL}${endpoint}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Embedding request failed: ${res.status} ${res.statusText}`);

  let embedding: number[];
  if (EMBED_API_STYLE === "openai") {
    const data = (await res.json()) as { data: Array<{ embedding: number[] }> };
    embedding = data.data[0]?.embedding;
  } else {
    const data = (await res.json()) as { embeddings: number[][] };
    embedding = data.embeddings[0];
  }

  if (!embedding) {
    throw new Error("Embedding provider returned no vectors");
  }
  if (embedding.length !== EMBED_DIMENSIONS) {
    throw new Error(`Expected ${EMBED_DIMENSIONS} dimensions, got ${embedding.length}`);
  }
  return embedding;
}

function vecLiteral(v: number[]): string {
  return `[${v.join(",")}]`;
}

function summarizeArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args).map(([key, value]) => {
    if (typeof value === "string") {
      const compact = value.length > 120 ? `${value.slice(0, 117)}...` : value;
      return `${key}=${JSON.stringify(compact)}`;
    }
    return `${key}=${JSON.stringify(value)}`;
  });
  return entries.join(", ");
}

function logToolInvocation(name: string, args: Record<string, unknown> = {}): void {
  const summary = summarizeArgs(args);
  console.error(`[mcp] tool=${name}${summary ? ` args: ${summary}` : ""}`);
}

type SearchRow = {
  chunk_id: number;
  file_path: string;
  language: string | null;
  content: string;
  symbol_name: string | null;
  symbol_type: string | null;
  intent: string | null;
  intent_detail: string | null;
  start_line: number;
  end_line: number;
  similarity: number | null;
  keyword_score?: number | null;
};

function extractKeywordTerms(text: string): string[] {
  const stopwords = new Set([
    "a", "an", "and", "bar", "by", "code", "config", "configuration", "file", "for", "how",
    "in", "is", "of", "or", "the", "to", "with",
  ]);
  const seen = new Set<string>();
  const terms: string[] = [];

  for (const raw of text.toLowerCase().split(/[^a-z0-9_]+/)) {
    const term = raw.trim();
    if (term.length < 3 || stopwords.has(term) || seen.has(term)) {
      continue;
    }
    seen.add(term);
    terms.push(term);
  }

  return terms.slice(0, 6);
}

function formatSearchResults(rows: SearchRow[]): string {
  return rows
    .map((r, i) => {
      const matchSource =
        r.similarity != null && (r.keyword_score || 0) > 0
          ? "hybrid"
          : r.similarity != null
            ? "semantic"
            : "keyword";

      return [
        `### Result ${i + 1} — ${r.file_path}:${r.start_line}-${r.end_line}`,
        r.similarity != null ? `**Similarity:** ${(r.similarity * 100).toFixed(1)}%` : "",
        r.keyword_score ? `**Keyword Score:** ${r.keyword_score}` : "",
        `**Match:** ${matchSource}`,
        r.symbol_name ? `**Symbol:** ${r.symbol_type} \`${r.symbol_name}\`` : "",
        r.intent ? `**Intent:** ${r.intent}` : "",
        r.intent_detail ? `**Description:** ${r.intent_detail}` : "",
        "```",
        r.content,
        "```",
      ]
        .filter(Boolean)
        .join("\n");
    })
    .join("\n\n---\n\n");
}

async function keywordSearch(
  searchQuery: string,
  limit: number,
  intent?: string,
  language?: string,
  pathPrefix?: string,
): Promise<SearchRow[]> {
  const terms = extractKeywordTerms(searchQuery);
  if (terms.length === 0) {
    return [];
  }

  const params: any[] = [];
  const termSql: string[] = [];
  const scoreParts: string[] = [];

  for (const term of terms) {
    const pattern = `%${term.replace(/[%_\\]/g, "\\$&")}%`;

    params.push(pattern);
    const contentIndex = params.length;
    params.push(pattern);
    const symbolIndex = params.length;
    params.push(pattern);
    const pathIndex = params.length;

    termSql.push(
      `(cc.content ILIKE $${contentIndex} ESCAPE '\\' OR COALESCE(cc.symbol_name, '') ILIKE $${symbolIndex} ESCAPE '\\' OR f.path ILIKE $${pathIndex} ESCAPE '\\')`
    );
    scoreParts.push(
      `(CASE WHEN cc.content ILIKE $${contentIndex} ESCAPE '\\' THEN 1 ELSE 0 END + CASE WHEN COALESCE(cc.symbol_name, '') ILIKE $${symbolIndex} ESCAPE '\\' THEN 3 ELSE 0 END + CASE WHEN f.path ILIKE $${pathIndex} ESCAPE '\\' THEN 2 ELSE 0 END)`
    );
  }

  let filterSql = "";
  if (intent) {
    params.push(intent);
    filterSql += ` AND cc.intent = $${params.length}`;
  }
  if (language) {
    params.push(language);
    filterSql += ` AND f.language = $${params.length}`;
  }
  if (pathPrefix) {
    params.push(`${pathPrefix}%`);
    filterSql += ` AND f.path LIKE $${params.length}`;
  }

  params.push(Math.max(limit * 3, 20));

  const sql = `
    SELECT
      cc.id AS chunk_id,
      f.path AS file_path,
      f.language,
      cc.content,
      cc.symbol_name,
      cc.symbol_type,
      cc.intent,
      cc.intent_detail,
      cc.start_line,
      cc.end_line,
      NULL::float AS similarity,
      ${scoreParts.join(" + ")} AS keyword_score
    FROM code_chunks cc
    JOIN files f ON cc.file_id = f.id
    WHERE (${termSql.join(" OR ")})
      ${filterSql}
    ORDER BY keyword_score DESC, f.path, cc.start_line
    LIMIT $${params.length}
  `;

  const result = await query(sql, params);
  return result.rows as SearchRow[];
}

// ── MCP Server ────────────────────────────────────────────

const CODEBRAIN_USAGE_URI = "codebrain://usage";
const CODEBRAIN_USAGE_TEXT = [
  "# CodeBrain Usage",
  "",
  "Use CodeBrain as the discovery layer before broad shell-based exploration.",
  "",
  "Recommended workflow:",
  "1. Start with `codebase_stats` for a quick high-level overview.",
  "2. Use `find_symbol` first when you know any part of an identifier name.",
  "3. Use `get_file_map` to understand a subsystem before opening many files.",
  "4. Use `trace_dependencies` before manually following imports or call chains.",
  "5. Use `get_intent` before editing a file you have not read yet.",
  "6. Use `semantic_search` when the exact symbol name is unknown.",
  "",
  "Search tips:",
  "- Prefer short technical phrases over long conversational questions.",
  "- Include framework names, APIs, or domain terms when possible.",
  "- If `semantic_search` is weak, retry with more specific terms or a lower threshold.",
  "- Use shell search after CodeBrain narrows the likely files or when exact text matching is required.",
  "",
  "Reliability rules:",
  "- Always inspect the actual files before editing.",
  "- Treat CodeBrain as guidance for discovery, not the final source of truth.",
].join("\n");

function registerResources(server: McpServer) {
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
    })
  );
}

function registerTools(server: McpServer) {
// Tool 1: Semantic Search
server.tool(
  "semantic_search",
  "Use for concept-based discovery when the exact symbol name is unknown. Runs hybrid retrieval: semantic similarity first, then keyword matching for exact names and sparse terms.",
  {
    query: z.string().describe("Short technical search phrase. Prefer 2-8 words with framework names, APIs, or domain terms."),
    limit: z.number().optional().default(10).describe("Max results (default 10)"),
    intent: z
      .enum([
        "data-model", "business-logic", "api-endpoint", "utility",
        "configuration", "test", "infrastructure", "ui-component",
        "integration", "orchestration", "type-definition", "middleware", "migration",
      ])
      .optional()
      .describe("Optional intent filter when you already know the kind of code you want."),
    language: z.string().optional().describe("Optional language filter (python, typescript, swift, etc.)."),
    path_prefix: z.string().optional().describe("Optional path prefix to focus search on a subsystem (for example src/api/)."),
    threshold: z.number().optional().default(0.3).describe("Semantic similarity threshold 0-1. Lower this when the codebase uses sparse or exact terminology."),
  },
  async ({ query: searchQuery, limit, intent, language, path_prefix, threshold }) => {
    logToolInvocation("semantic_search", { query: searchQuery, limit, intent, language, path_prefix, threshold });
    const embedding = await embed(searchQuery);

    const semanticResult = await query(
      `SELECT * FROM search_code($1::vector, $2, $3, $4, $5, NULL, $6)`,
      [vecLiteral(embedding), limit, intent || null, language || null, path_prefix || null, threshold]
    );
    const keywordResult = await keywordSearch(searchQuery, limit, intent, language, path_prefix);

    const merged = new Map<number, SearchRow>();

    for (const row of semanticResult.rows as SearchRow[]) {
      merged.set(row.chunk_id, { ...row, keyword_score: 0 });
    }

    for (const row of keywordResult) {
      const existing = merged.get(row.chunk_id);
      if (existing) {
        existing.keyword_score = Math.max(existing.keyword_score || 0, row.keyword_score || 0);
      } else {
        merged.set(row.chunk_id, row);
      }
    }

    const rows = Array.from(merged.values())
      .sort((a, b) => {
        const aSemantic = a.similarity ?? -1;
        const bSemantic = b.similarity ?? -1;
        if (bSemantic !== aSemantic) {
          return bSemantic - aSemantic;
        }

        const aKeyword = a.keyword_score ?? 0;
        const bKeyword = b.keyword_score ?? 0;
        if (bKeyword !== aKeyword) {
          return bKeyword - aKeyword;
        }

        return a.file_path.localeCompare(b.file_path) || a.start_line - b.start_line;
      })
      .slice(0, limit);

    if (rows.length === 0) {
      return {
        content: [{ type: "text", text: "No results found. Try broadening your query, lowering the threshold, or using more specific symbol names." }],
      };
    }

    return { content: [{ type: "text", text: formatSearchResults(rows) }] };
  }
);

// Tool 2: Find Symbol
server.tool(
  "find_symbol",
  "Use first when you know part of a symbol name. Faster and more precise than semantic search for identifiers, classes, functions, methods, types, structs, and protocols.",
  {
    name: z.string().describe("Partial or exact symbol name. Start here before broad text search when you know the identifier."),
    kind: z
      .enum(["function", "class", "interface", "type", "method", "variable", "constant", "enum", "impl", "namespace"])
      .optional()
      .describe("Optional symbol kind filter to narrow ambiguous names."),
    file: z.string().optional().describe("Optional filename filter when the symbol is likely in a known file or module."),
  },
  async ({ name, kind, file }) => {
    logToolInvocation("find_symbol", { name, kind, file });
    const result = await query(`SELECT * FROM find_symbol($1, $2, $3)`, [
      name,
      kind || null,
      file || null,
    ]);

    if (result.rows.length === 0) {
      return { content: [{ type: "text", text: `No symbols found matching "${name}".` }] };
    }

    const formatted = result.rows
      .map((r: any) => {
        return [
          `**${r.kind}** \`${r.qualified_name || r.name}\``,
          `  File: ${r.file_path}:${r.start_line}-${r.end_line}`,
          r.signature ? `  Signature: \`${r.signature}\`` : "",
          r.docstring ? `  Doc: ${r.docstring.slice(0, 200)}` : "",
          r.is_exported ? "  ✓ Exported" : "",
        ]
          .filter(Boolean)
          .join("\n");
      })
      .join("\n\n");

    return { content: [{ type: "text", text: formatted }] };
  }
);

// Tool 3: Trace Dependencies
server.tool(
  "trace_dependencies",
  "Use before manually tracing imports or call chains. Follows dependency edges to answer what depends on X, what X depends on, or both.",
  {
    path: z.string().describe("File path or distinctive partial path to trace."),
    direction: z
      .enum(["inbound", "outbound", "both"])
      .optional()
      .default("both")
      .describe("Use inbound for reverse dependencies, outbound for direct dependencies, both for a quick graph walk."),
    max_depth: z.number().optional().default(3).describe("Depth limit for the graph walk (default 3). Use 1-2 for focused tracing."),
  },
  async ({ path, direction, max_depth }) => {
    logToolInvocation("trace_dependencies", { path, direction, max_depth });
    const result = await query(`SELECT * FROM trace_dependencies($1, $2, $3)`, [
      path,
      direction,
      max_depth,
    ]);

    if (result.rows.length === 0) {
      return { content: [{ type: "text", text: `No dependencies found for "${path}".` }] };
    }

    const formatted = result.rows
      .map((r: any) => {
        const arrow = "→";
        const src = r.source_symbol ? `${r.source_path} (${r.source_symbol})` : r.source_path;
        const tgt = r.target_symbol
          ? `${r.target_path_out} (${r.target_symbol})`
          : r.target_path_out || r.external_module;
        return `${"  ".repeat(r.depth - 1)}${src} ${arrow} ${tgt} [${r.dep_kind}]`;
      })
      .join("\n");

    return { content: [{ type: "text", text: `Dependency trace for "${path}":\n\n${formatted}` }] };
  }
);

// Tool 4: File Map
server.tool(
  "get_file_map",
  "Use before opening many files. Gives an architectural map of a directory or subsystem, including each file's role, summary, and exported symbols.",
  {
    path_prefix: z.string().optional().default("").describe("Directory or path prefix to inspect first (for example src/api/ or App/Views/)."),
    repo: z.string().optional().describe("Optional repository name when multiple repositories are indexed."),
  },
  async ({ path_prefix, repo }) => {
    logToolInvocation("get_file_map", { path_prefix, repo });
    let sql = `
      SELECT f.path, f.language, f.line_count, f.role, f.summary,
             array_agg(DISTINCT s.name || ' (' || s.kind || ')') FILTER (WHERE s.name IS NOT NULL) AS symbols
      FROM files f
      LEFT JOIN symbols s ON s.file_id = f.id AND s.is_exported = true
      WHERE f.path LIKE $1 || '%'
    `;
    const params: any[] = [path_prefix];

    if (repo) {
      sql += ` AND f.repo = $2`;
      params.push(repo);
    }

    sql += ` GROUP BY f.id ORDER BY f.path`;

    const result = await query(sql, params);

    if (result.rows.length === 0) {
      return { content: [{ type: "text", text: `No files found under "${path_prefix}".` }] };
    }

    const formatted = result.rows
      .map((r: any) => {
        const symbols = r.symbols ? r.symbols.join(", ") : "none";
        return [
          `📄 **${r.path}** (${r.language}, ${r.line_count} lines)`,
          `   Role: ${r.role || "unclassified"}`,
          r.summary ? `   Summary: ${r.summary}` : "",
          `   Exports: ${symbols}`,
        ]
          .filter(Boolean)
          .join("\n");
      })
      .join("\n\n");

    return { content: [{ type: "text", text: formatted }] };
  }
);

// Tool 5: Get Intent
server.tool(
  "get_intent",
  "Use before editing an unfamiliar file. Summarizes what the file is for and what its indexed code sections are doing.",
  {
    path: z.string().describe("File path or distinctive partial path to inspect before modifying it."),
  },
  async ({ path }) => {
    logToolInvocation("get_intent", { path });
    const fileResult = await query(
      `SELECT f.summary, f.role FROM files f WHERE f.path LIKE '%' || $1 || '%' LIMIT 1`,
      [path]
    );

    const chunkResult = await query(
      `SELECT cc.symbol_name, cc.symbol_type, cc.intent, cc.intent_detail, cc.start_line, cc.end_line
       FROM code_chunks cc JOIN files f ON cc.file_id = f.id
       WHERE f.path LIKE '%' || $1 || '%'
       ORDER BY cc.chunk_index`,
      [path]
    );

    if (fileResult.rows.length === 0) {
      return { content: [{ type: "text", text: `File "${path}" not found in the index.` }] };
    }

    const file = fileResult.rows[0];
    const chunks = chunkResult.rows;

    let output = `# ${path}\n\n`;
    output += `**Role:** ${file.role || "unknown"}\n`;
    output += `**Summary:** ${file.summary || "no summary"}\n\n`;
    output += `## Code Sections\n\n`;

    for (const c of chunks) {
      output += `- **${c.symbol_type || "block"}** `;
      if (c.symbol_name) output += `\`${c.symbol_name}\` `;
      output += `(L${c.start_line}–${c.end_line})`;
      if (c.intent) output += ` — *${c.intent}*`;
      if (c.intent_detail) output += `: ${c.intent_detail}`;
      output += "\n";
    }

    return { content: [{ type: "text", text: output }] };
  }
);

// Tool 6: Codebase Stats
server.tool(
  "codebase_stats",
  "Use first for a quick overview. Returns high-level metrics for the indexed codebase, including file counts, languages, chunk counts, and symbol distribution.",
  {},
  async () => {
    logToolInvocation("codebase_stats");
    const result = await query(`
      SELECT
        f.repo,
        COUNT(DISTINCT f.id) AS total_files,
        SUM(f.line_count) AS total_lines,
        COUNT(DISTINCT cc.id) AS total_chunks,
        COUNT(DISTINCT s.id) AS total_symbols
      FROM files f
      LEFT JOIN code_chunks cc ON cc.file_id = f.id
      LEFT JOIN symbols s ON s.file_id = f.id
      GROUP BY f.repo
    `);

    const langResult = await query(`
      SELECT f.repo, f.language, COUNT(*) AS cnt
      FROM files f GROUP BY f.repo, f.language ORDER BY cnt DESC
    `);

    const intentResult = await query(`
      SELECT cc.intent, COUNT(*) AS cnt
      FROM code_chunks cc GROUP BY cc.intent ORDER BY cnt DESC
    `);

    let output = "# Codebase Statistics\n\n";

    for (const r of result.rows) {
      output += `## ${r.repo}\n`;
      output += `- **Files:** ${r.total_files}\n`;
      output += `- **Lines:** ${Number(r.total_lines).toLocaleString()}\n`;
      output += `- **Chunks:** ${r.total_chunks}\n`;
      output += `- **Symbols:** ${r.total_symbols}\n\n`;
    }

    output += "### Languages\n";
    for (const r of langResult.rows) {
      output += `- ${r.language || "unknown"}: ${r.cnt} files\n`;
    }

    output += "\n### Intent Distribution\n";
    for (const r of intentResult.rows) {
      output += `- ${r.intent || "unclassified"}: ${r.cnt} chunks\n`;
    }

    return { content: [{ type: "text", text: output }] };
  }
);
}

function createServer(): McpServer {
  const server = new McpServer({
    name: "codebrain",
    version: "1.0.0",
  });
  registerResources(server);
  registerTools(server);
  return server;
}

// ── Start ─────────────────────────────────────────────────

async function startStdioServer() {
  const server = createServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("CodeBrain MCP server running (stdio)");
}

async function startHttpTransport() {
  const app = createMcpExpressApp({
    host: MCP_HTTP_HOST,
    allowedHosts: MCP_ALLOWED_HOSTS,
  });
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
        `[mcp] request method=${req.method} rpc=${rpcMethod || "unknown"} session=${sessionId || "new"}${
          requestedTool ? ` tool=${requestedTool}` : ""
        }`
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
    await pool.end().catch(() => undefined);
    process.exit(0);
  };

  process.once("SIGINT", () => { void shutdown("SIGINT"); });
  process.once("SIGTERM", () => { void shutdown("SIGTERM"); });
}

async function main() {
  if (MCP_TRANSPORT === "stdio") {
    await startStdioServer();
    return;
  }
  await startHttpTransport();
}

main().catch((err) => {
  console.error("Fatal error:", err);
  void pool.end().catch(() => undefined);
  process.exit(1);
});
