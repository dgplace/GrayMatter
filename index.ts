#!/usr/bin/env node
/**
 * CodeBrain MCP Server
 *
 * Exposes local codebase intelligence over MCP (stdio transport).
 * Tools: semantic_search, find_symbol, trace_dependencies, get_file_map, get_intent, codebase_stats
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import pg from "pg";

const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://codebrain:codebrain_local@localhost:5433/codebrain";

const EMBED_API_STYLE = (process.env.EMBED_API_STYLE || "ollama").toLowerCase();
const EMBED_BASE_URL = (
  process.env.EMBED_BASE_URL ||
  process.env.OLLAMA_URL ||
  (EMBED_API_STYLE === "ollama" ? "http://localhost:11434" : "http://localhost:11435")
).replace(/\/+$/, "");
const EMBED_MODEL = process.env.EMBED_MODEL || "nomic-embed-text";
const EMBED_DIMENSIONS = Number(process.env.EMBED_DIMENSIONS || "768");
const EMBED_API_KEY = process.env.EMBED_API_KEY;

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

// ── MCP Server ────────────────────────────────────────────

const server = new McpServer({
  name: "codebrain",
  version: "1.0.0",
});

// Tool 1: Semantic Search
server.tool(
  "semantic_search",
  "Search codebase by meaning. Finds code related to a concept even without keyword matches.",
  {
    query: z.string().describe("Natural language description of what you're looking for"),
    limit: z.number().optional().default(10).describe("Max results (default 10)"),
    intent: z
      .enum([
        "data-model", "business-logic", "api-endpoint", "utility",
        "configuration", "test", "infrastructure", "ui-component",
        "integration", "orchestration", "type-definition", "middleware", "migration",
      ])
      .optional()
      .describe("Filter by code intent category"),
    language: z.string().optional().describe("Filter by language (python, typescript, etc.)"),
    path_prefix: z.string().optional().describe("Filter by file path prefix (e.g. src/api/)"),
    threshold: z.number().optional().default(0.3).describe("Similarity threshold 0-1 (default 0.3)"),
  },
  async ({ query: searchQuery, limit, intent, language, path_prefix, threshold }) => {
    const embedding = await embed(searchQuery);

    const result = await query(
      `SELECT * FROM search_code($1::vector, $2, $3, $4, $5, NULL, $6)`,
      [vecLiteral(embedding), limit, intent || null, language || null, path_prefix || null, threshold]
    );

    if (result.rows.length === 0) {
      return {
        content: [{ type: "text", text: "No results found. Try broadening your query or lowering the threshold." }],
      };
    }

    const formatted = result.rows
      .map((r: any, i: number) => {
        return [
          `### Result ${i + 1} — ${r.file_path}:${r.start_line}-${r.end_line}`,
          `**Similarity:** ${(r.similarity * 100).toFixed(1)}%`,
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

    return { content: [{ type: "text", text: formatted }] };
  }
);

// Tool 2: Find Symbol
server.tool(
  "find_symbol",
  "Locate functions, classes, types, interfaces by name. Supports partial matching.",
  {
    name: z.string().describe("Symbol name to search for (partial match supported)"),
    kind: z
      .enum(["function", "class", "interface", "type", "method", "variable", "constant", "enum", "impl", "namespace"])
      .optional()
      .describe("Filter by symbol kind"),
    file: z.string().optional().describe("Filter by filename (partial match)"),
  },
  async ({ name, kind, file }) => {
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
  "Follow import and dependency chains to/from a file or module. Answers 'what depends on X?' and 'what does X depend on?'",
  {
    path: z.string().describe("File path or partial path to trace"),
    direction: z
      .enum(["inbound", "outbound", "both"])
      .optional()
      .default("both")
      .describe("inbound = what depends on this, outbound = what this depends on"),
    max_depth: z.number().optional().default(3).describe("How many levels deep to trace (default 3)"),
  },
  async ({ path, direction, max_depth }) => {
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
  "Get an architectural overview of files in a directory. Shows each file's role, summary, and key symbols.",
  {
    path_prefix: z.string().optional().default("").describe("Directory path prefix to filter (e.g. src/api/)"),
    repo: z.string().optional().describe("Repository name (if multiple repos indexed)"),
  },
  async ({ path_prefix, repo }) => {
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
  "Understand what a specific file or code section is trying to accomplish.",
  {
    path: z.string().describe("File path to analyze"),
  },
  async ({ path }) => {
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
  "Get high-level metrics about the indexed codebase(s): file counts, languages, symbol distribution.",
  {},
  async () => {
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

// ── Start ─────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("CodeBrain MCP server running (stdio)");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
