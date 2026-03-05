/**
 * @file tests/web-ui.test.ts
 * @brief Unit tests for static web UI rendering helpers.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { renderWebUi } from "../src/web/ui.ts";

test("renderWebUi includes repo API hooks and graph container", () => {
  const html = renderWebUi();

  assert.match(html, /\/ui\/api\/repos/);
  assert.match(html, /\/ui\/api\/tool-calls/);
  assert.match(html, /id="repoSelect"/);
  assert.match(html, /id="graph"/);
  assert.match(html, /id="toolCallBody"/);
  assert.match(html, /CodeBrain Semantic Graph Browser/);
});
