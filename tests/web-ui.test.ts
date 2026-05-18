/**
 * @file tests/web-ui.test.ts
 * @brief Unit tests for the web UI HTML shell and the bundled client source.
 *        The shell carries no inline JS; verify it references the bundled
 *        assets, exposes the panel/table mount points, and that the client
 *        source still wires the expected MCP-backed API endpoints.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { renderWebUi } from "../src/web/ui.ts";

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const appSrc = readFileSync(join(repoRoot, "src", "web", "assets", "app.ts"), "utf8");
const routeSrc = readFileSync(join(repoRoot, "src", "web", "routes.ts"), "utf8");
const indexJobsSrc = readFileSync(join(repoRoot, "src", "web", "indexJobs.ts"), "utf8");
const mcpDockerfile = readFileSync(join(repoRoot, "docker", "Dockerfile.mcp"), "utf8");
const composeFile = readFileSync(join(repoRoot, "docker", "docker-compose.yml"), "utf8");
const buildSh = readFileSync(join(repoRoot, "scripts", "build.sh"), "utf8");
const buildBat = readFileSync(join(repoRoot, "scripts", "build.bat"), "utf8");

test("renderWebUi shell exposes panel mount points and references bundled assets", () => {
  const html = renderWebUi();

  assert.match(html, /href="\/ui\/assets\/styles\.css"/);
  assert.match(html, /src="\/ui\/assets\/app\.js"/);
  assert.match(html, /id="repoSelect"/);
  assert.match(html, /id="statsBody"/);
  assert.match(html, /id="toolCallBody"/);
  assert.match(html, /id="indexMgmtBody"/);
  assert.match(html, /id="indexBrowserTabs"/);
  assert.match(html, /id="indexBrowserPane"/);
  assert.doesNotMatch(html, /id="modulesBody"/);
  assert.doesNotMatch(html, /id="edgeTable"/);
  assert.doesNotMatch(html, /id="refreshBtn"/);
  assert.doesNotMatch(html, /<script>[^<]/);
});

test("client app source wires the expected /ui/api endpoints", () => {
  assert.match(appSrc, /\/ui\/api\/repos/);
  assert.match(appSrc, /\/ui\/api\/tool-calls/);
  assert.match(appSrc, /\/stats/);
  assert.match(appSrc, /\/tables/);
  assert.match(appSrc, /\/index-jobs/);
  assert.doesNotMatch(appSrc, /\/modules/);
});

test("client app source owns inline raw index table browsing", () => {
  assert.match(appSrc, /loadIndexBrowser/);
  assert.match(appSrc, /loadIndexTablePage/);
  assert.match(appSrc, /renderTablePageHtml/);
  assert.match(appSrc, /tableFiltersByTable/);
});

test("index management wires web-initiated indexing jobs", () => {
  assert.match(appSrc, /id="indexRepoBtn"/);
  assert.match(appSrc, /startIndexFromDialog/);
  assert.match(appSrc, /localStorage\.getItem/);
  assert.match(appSrc, /localStorage\.setItem/);
  assert.match(appSrc, /id="indexWorkersInput"/);
  assert.match(appSrc, /getIndexWorkerCount/);
  assert.doesNotMatch(appSrc, /indexChooseBtn/);
  assert.doesNotMatch(appSrc, /indexFolderInput/);
  assert.match(routeSrc, /\/ui\/api\/repos\/:repo\/index-jobs/);
  assert.match(routeSrc, /startIndexJob/);
  assert.match(indexJobsSrc, /codebrain\.ingest/);
  assert.match(indexJobsSrc, /TTY_INTERACTIVE=1/);
  assert.match(indexJobsSrc, /writeMutableLogLine/);
  assert.match(indexJobsSrc, /stripAnsi/);
  assert.match(indexJobsSrc, /"--workers"/);
  assert.match(indexJobsSrc, /String\(workerCount\)/);
  assert.match(indexJobsSrc, /CODEBRAIN_REPO_ROOT/);
  assert.match(indexJobsSrc, /isContainerRuntime/);
  assert.match(indexJobsSrc, /docker", \["inspect", hostname\(\)\]/);
  assert.match(indexJobsSrc, /buildContainerDockerArgs/);
  assert.match(indexJobsSrc, /CODEBRAIN_INDEXER_IMAGE/);
  assert.match(mcpDockerfile, /docker-compose-plugin/);
  assert.match(composeFile, /CODEBRAIN_REPO_ROOT: \/workspace/);
  assert.match(composeFile, /\/var\/run\/docker\.sock:\/var\/run\/docker\.sock/);
  assert.match(composeFile, /classifier_proxy:\n\s+condition: service_started/);
  assert.match(buildSh, /resolve-container-endpoints\.py/);
  assert.match(buildSh, /export "\$line"/);
  assert.match(buildSh, /recreate_targets=\(embed_proxy classifier_proxy postgres_frontdoor mcp mcp_frontdoor\)/);
  assert.match(buildBat, /resolve-container-endpoints\.ps1/);
  assert.match(buildBat, /embed_proxy classifier_proxy postgres_frontdoor mcp mcp_frontdoor/);
});
