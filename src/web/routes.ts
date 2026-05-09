/**
 * @file src/web/routes.ts
 * @brief HTTP routes for the embedded semantic graph browser UI and JSON APIs.
 */

import { readFile } from "node:fs/promises";
import { dirname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import {
  deleteRepository,
  getRepositoryGraph,
  getRepositoryIndexSize,
  getRepositoryStats,
  listRepositories,
  repositoryExists,
  getModuleIntents,
} from "../repositories/store.js";
import { getToolCallSnapshot } from "../mcp/toolCallStats.js";
import { renderWebUi } from "./ui.js";

const ASSETS_DIR = join(dirname(fileURLToPath(import.meta.url)), "assets");

const ASSET_CONTENT_TYPES: Record<string, string> = {
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

/**
 * @brief Resolve a request path relative to the assets directory, rejecting
 *        any traversal that would escape it.
 * @param requestPath Path segment from the URL after `/ui/assets/`.
 * @returns Absolute on-disk path inside the assets dir, or null if invalid.
 */
function resolveAssetPath(requestPath: string): string | null {
  const candidate = normalize(join(ASSETS_DIR, requestPath));
  if (!candidate.startsWith(ASSETS_DIR + "/") && candidate !== ASSETS_DIR) {
    return null;
  }
  return candidate;
}

/**
 * @brief Registers browser UI and JSON API routes on the existing HTTP app.
 * @param app Express-compatible MCP HTTP app.
 * @returns Void.
 */
export function registerWebRoutes(app: any): void {
  app.get("/ui", (_req: any, res: any) => {
    res.status(200).type("text/html; charset=utf-8").send(renderWebUi());
  });

  app.get("/ui/assets/:filename", async (req: any, res: any) => {
    const filename = String(req.params.filename || "");
    const resolved = resolveAssetPath(filename);
    if (!resolved) {
      res.status(400).json({ error: "Invalid asset path." });
      return;
    }
    const ext = resolved.slice(resolved.lastIndexOf("."));
    const contentType = ASSET_CONTENT_TYPES[ext];
    if (!contentType) {
      res.status(404).json({ error: "Unsupported asset type." });
      return;
    }
    try {
      const body = await readFile(resolved);
      res.status(200).type(contentType).send(body);
    } catch (error: any) {
      if (error?.code === "ENOENT") {
        res.status(404).json({ error: `Asset not found: ${filename}` });
        return;
      }
      console.error("Failed to read UI asset:", error);
      res.status(500).json({ error: "Failed to read UI asset." });
    }
  });

  app.get("/ui/api/repos", async (_req: any, res: any) => {
    try {
      const repositories = await listRepositories();
      res.status(200).json({ repositories });
    } catch (error) {
      console.error("Failed to list repositories:", error);
      res.status(500).json({ error: "Failed to list repositories." });
    }
  });

  app.get("/ui/api/tool-calls", (_req: any, res: any) => {
    res.status(200).json(getToolCallSnapshot());
  });

  app.get("/ui/api/repos/:repo/stats", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      const stats = await getRepositoryStats(repo);
      if (!stats) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      res.status(200).json(stats);
    } catch (error) {
      console.error("Failed to load repository stats:", error);
      res.status(500).json({ error: "Failed to load repository stats." });
    }
  });

  app.get("/ui/api/repos/:repo/size", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      const size = await getRepositoryIndexSize(repo);
      if (!size) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      res.status(200).json(size);
    } catch (error) {
      console.error("Failed to load repository size:", error);
      res.status(500).json({ error: "Failed to load repository size." });
    }
  });

  app.delete("/ui/api/repos/:repo", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      const deleted = await deleteRepository(repo);
      res.status(200).json({ deleted_files: deleted, repo });
    } catch (error) {
      console.error("Failed to delete repository:", error);
      res.status(500).json({ error: "Failed to delete repository index." });
    }
  });

  app.get("/ui/api/repos/:repo/modules", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }

      const kind = req.query.kind as string;
      const pathPrefix = req.query.path_prefix as string;
      const modules = await getModuleIntents(repo, kind, pathPrefix);
      res.status(200).json({ modules });
    } catch (error) {
      console.error("Failed to load module intents:", error);
      res.status(500).json({ error: "Failed to load module intents." });
    }
  });

  app.get("/ui/api/repos/:repo/graph", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }

      const rawLimit = Number(req.query.limit || 300);
      const graph = await getRepositoryGraph(repo, Number.isFinite(rawLimit) ? rawLimit : 300);
      res.status(200).json(graph);
    } catch (error) {
      console.error("Failed to load repository graph:", error);
      res.status(500).json({ error: "Failed to load repository graph." });
    }
  });
}
