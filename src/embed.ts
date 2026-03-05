/**
 * @file src/embed.ts
 * @brief Embedding provider client and vector literal helpers.
 */

import {
  EMBED_API_KEY,
  EMBED_API_STYLE,
  EMBED_BASE_URL,
  EMBED_DIMENSIONS,
  EMBED_MODEL,
} from "./config.js";

/**
 * @brief Requests a text embedding from the configured provider.
 * @param text Source text to embed.
 * @returns Numeric embedding vector with the configured dimensionality.
 */
export async function embed(text: string): Promise<number[]> {
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

  const response = await fetch(`${EMBED_BASE_URL}${endpoint}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Embedding request failed: ${response.status} ${response.statusText}`);
  }

  let embedding: number[] | undefined;
  if (EMBED_API_STYLE === "openai") {
    const data = (await response.json()) as { data: Array<{ embedding: number[] }> };
    embedding = data.data[0]?.embedding;
  } else {
    const data = (await response.json()) as { embeddings: number[][] };
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

/**
 * @brief Formats a numeric vector as a pgvector literal.
 * @param vector Numeric embedding vector.
 * @returns SQL literal string (`[1,2,3]`) compatible with pgvector casts.
 */
export function vecLiteral(vector: number[]): string {
  return `[${vector.join(",")}]`;
}
