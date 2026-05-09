/**
 * @file scripts/build-ui.mjs
 * @brief Bundles the browser-side web UI assets (TypeScript -> ES2020 IIFE)
 *        and copies static CSS into the dist tree alongside the compiled
 *        TypeScript server output.
 */

import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const srcAssets = join(root, "src", "web", "assets");
const outAssets = join(root, "dist", "src", "web", "assets");

mkdirSync(outAssets, { recursive: true });

await build({
  entryPoints: [join(srcAssets, "app.ts")],
  bundle: true,
  format: "iife",
  target: "es2020",
  outfile: join(outAssets, "app.js"),
  sourcemap: true,
  minify: true,
  logLevel: "info",
});

copyFileSync(join(srcAssets, "styles.css"), join(outAssets, "styles.css"));
console.log(`UI assets bundled to ${outAssets}`);
