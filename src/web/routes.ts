/**
 * @file src/web/routes.ts
 * @brief HTTP routes for the embedded semantic graph browser UI and JSON APIs.
 */

import {
  getRepositoryGraph,
  getRepositoryStats,
  listRepositories,
  repositoryExists,
} from "../repositories/store.js";
import { renderWebUi } from "./ui.js";

/**
 * @brief Registers browser UI and JSON API routes on the existing HTTP app.
 * @param app Express-compatible MCP HTTP app.
 * @returns Void.
 */
export function registerWebRoutes(app: any): void {
  app.get("/ui", (_req: any, res: any) => {
    res.status(200).type("text/html; charset=utf-8").send(renderWebUi());
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
