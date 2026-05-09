/**
 * @file tests/web-ui.test.ts
 * @brief Unit tests for the web UI HTML shell and the bundled client source.
 *        The shell carries no inline JS; verify it references the bundled
 *        assets, exposes the panel/graph mount points, and that the client
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

test("renderWebUi shell exposes panel mount points and references bundled assets", () => {
  const html = renderWebUi();

  assert.match(html, /href="\/ui\/assets\/styles\.css"/);
  assert.match(html, /src="\/ui\/assets\/app\.js"/);
  assert.match(html, /id="repoSelect"/);
  assert.match(html, /id="graph"/);
  assert.match(html, /id="statsBody"/);
  assert.match(html, /id="toolCallBody"/);
  assert.match(html, /id="modulesBody"/);
  assert.match(html, /id="indexMgmtBody"/);
  assert.match(html, /id="edgeTable"/);
  assert.doesNotMatch(html, /<script>[^<]/);
});

test("client app source wires the expected /ui/api endpoints", () => {
  assert.match(appSrc, /\/ui\/api\/repos/);
  assert.match(appSrc, /\/ui\/api\/tool-calls/);
  assert.match(appSrc, /\/stats/);
  assert.match(appSrc, /\/graph/);
  assert.match(appSrc, /\/modules/);
  assert.match(appSrc, /\/size/);
});

test("client app source uses the WebGL Sigma renderer and exposes edge-kind colors", () => {
  assert.match(appSrc, /from "sigma"/);
  assert.match(appSrc, /from "graphology"/);
  assert.match(appSrc, /forceAtlas2/);
  assert.match(appSrc, /defaultEdgeType:\s*"arrow"/);
  assert.match(appSrc, /edgeKindMap/);
});
