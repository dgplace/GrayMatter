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
  getRepositoryStats,
  listRepositories,
  repositoryExists,
  getModuleIntents,
  getClusters,
  findCluster,
  getClusterMembers,
  findCycles,
} from "../repositories/store.js";
import {
  fetchBrowseTablePage,
  getBrowseTableSpec,
  listBrowseTables,
} from "../repositories/indexBrowser.js";
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

  app.get("/ui/api/repos/:repo/tables", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      const tables = await listBrowseTables(repo);
      res.status(200).json({ tables });
    } catch (error) {
      console.error("Failed to list browseable tables:", error);
      res.status(500).json({ error: "Failed to list browseable tables." });
    }
  });

  app.get("/ui/api/repos/:repo/tables/:table", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      const tableName = String(req.params.table || "");
      const spec = getBrowseTableSpec(tableName);
      if (!spec) {
        res.status(404).json({ error: `Unknown table \`${tableName}\`.` });
        return;
      }
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      const limit = Number(req.query.limit ?? 100);
      const offset = Number(req.query.offset ?? 0);
      const page = await fetchBrowseTablePage(repo, spec, limit, offset);
      res.status(200).json(page);
    } catch (error) {
      console.error("Failed to load table page:", error);
      res.status(500).json({ error: "Failed to load table page." });
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

  /**
   * @brief API endpoint to fetch clusters for a repository.
   * @param req Express request object.
   * @param res Express response object.
   */
  app.get("/ui/api/repos/:repo/clusters", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      const granularity = req.query.granularity as string;
      const clusters = await getClusters(repo, granularity);
      res.status(200).json({ clusters });
    } catch (error) {
      console.error("Failed to load clusters:", error);
      res.status(500).json({ error: "Failed to load clusters." });
    }
  });

  /**
   * @brief API endpoint to fetch members of a specific cluster.
   * @param req Express request object.
   * @param res Express response object.
   */
  app.get("/ui/api/repos/:repo/clusters/:cluster/members", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      const cluster = decodeURIComponent(String(req.params.cluster || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      
      const clusterRecord = await findCluster(repo, cluster);
      if (!clusterRecord) {
        res.status(404).json({ error: `Cluster \`${cluster}\` not found in repository \`${repo}\`.` });
        return;
      }

      const limit = req.query.limit ? parseInt(req.query.limit as string, 10) : 200;
      const members = await getClusterMembers(clusterRecord.id, clusterRecord.granularity, limit);
      res.status(200).json({ members });
    } catch (error) {
      console.error("Failed to load cluster members:", error);
      res.status(500).json({ error: "Failed to load cluster members." });
    }
  });

  /**
   * @brief API endpoint to fetch dependency cycles for a repository.
   * @param req Express request object.
   * @param res Express response object.
   */
  app.get("/ui/api/repos/:repo/cycles", async (req: any, res: any) => {
    try {
      const repo = decodeURIComponent(String(req.params.repo || ""));
      if (!(await repositoryExists(repo))) {
        res.status(404).json({ error: `Repository \`${repo}\` is not indexed.` });
        return;
      }
      const pathPrefix = req.query.path_prefix as string;
      const cycles = await findCycles(repo, pathPrefix);
      res.status(200).json({ cycles });
    } catch (error) {
      console.error("Failed to load cycles:", error);
      res.status(500).json({ error: "Failed to load cycles." });
    }
  });
}
