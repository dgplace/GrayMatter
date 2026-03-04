/**
 * @file src/config.ts
 * @brief Centralized runtime configuration values for the MCP server process.
 */

/** @brief PostgreSQL connection string used by the MCP process. */
export const DATABASE_URL =
  process.env.DATABASE_URL ||
  "postgresql://codebrain:codebrain_local@applepi3:5433/codebrain";

/** @brief Embedding API style selector (`ollama` or `openai`). */
export const EMBED_API_STYLE = (process.env.EMBED_API_STYLE || "ollama").toLowerCase();

/** @brief Base URL for embedding provider endpoints. */
export const EMBED_BASE_URL = (
  process.env.EMBED_BASE_URL ||
  process.env.OLLAMA_URL ||
  (EMBED_API_STYLE === "ollama" ? "http://applepi3:11434" : "http://applepi3:11435")
).replace(/\/+$/, "");

/** @brief Embedding model identifier. */
export const EMBED_MODEL = process.env.EMBED_MODEL || "nomic-embed-text";

/** @brief Embedding vector dimensionality expected from provider responses. */
export const EMBED_DIMENSIONS = Number(process.env.EMBED_DIMENSIONS || "768");

/** @brief Optional API key for OpenAI-compatible embedding providers. */
export const EMBED_API_KEY = process.env.EMBED_API_KEY;

/** @brief MCP transport mode (`http` by default; `stdio` optional). */
export const MCP_TRANSPORT = (process.env.MCP_TRANSPORT || "http").toLowerCase();

/** @brief Host interface bound by the MCP HTTP server. */
export const MCP_HTTP_HOST = process.env.MCP_HTTP_HOST || "127.0.0.1";

/** @brief Port used by the MCP HTTP server. */
export const MCP_HTTP_PORT = Number(process.env.MCP_HTTP_PORT || "3001");

/** @brief Optional host allowlist passed through to the MCP express transport app. */
export const MCP_ALLOWED_HOSTS = process.env.MCP_ALLOWED_HOSTS
  ? process.env.MCP_ALLOWED_HOSTS.split(",").map((host) => host.trim()).filter(Boolean)
  : undefined;
