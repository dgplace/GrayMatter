/**
 * @file src/web/ui.ts
 * @brief HTML shell for the embedded CodeBrain web UI. The shell carries no
 *        styles or scripts inline; it loads the neon-on-light theme from
 *        /ui/assets/styles.css and the WebGL graph + panel logic from
 *        /ui/assets/app.js (bundled by scripts/build-ui.mjs).
 */

/**
 * @brief Returns the single-page HTML shell for /ui.
 * @returns Complete HTML document string.
 */
export function renderWebUi(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeBrain Graph Browser</title>
  <link rel="stylesheet" href="/ui/assets/styles.css" />
</head>
<body>
  <header>
    <div>
      <h1>CodeBrain <span class="accent">Semantic</span> Graph Browser</h1>
      <p class="sub">Repo-scoped stats, modules, and an interactive WebGL dependency graph.</p>
    </div>
    <div class="control">
      <label for="repoSelect">Repository</label>
      <select id="repoSelect"></select>
      <div class="repo-load-progress" id="repoLoadProgress" hidden>
        <div class="repo-load-progress-track">
          <div class="repo-load-progress-fill" id="repoLoadProgressBar"></div>
        </div>
        <div class="repo-load-progress-text" id="repoLoadProgressText">Loading repository...</div>
      </div>
    </div>
  </header>

  <main>
    <div class="sidebar">
      <section class="panel">
        <h2>Repository Stats</h2>
        <div class="body" id="statsBody">
          <p class="warn" id="status">Loading repositories...</p>
        </div>
      </section>

      <section class="panel">
        <h2>Module Intents</h2>
        <div class="body" id="modulesBody">
          <p class="warn" id="modulesStatus">Select a repository.</p>
        </div>
      </section>

      <section class="panel">
        <h2>Index Management</h2>
        <div class="body" id="indexMgmtBody">
          <p class="warn" id="indexStatus">Select a repository.</p>
        </div>
      </section>

      <section class="panel">
        <h2>MCP Tool Calls</h2>
        <div class="body" id="toolCallBody">
          <p class="warn">Waiting for tool calls...</p>
        </div>
      </section>
    </div>

    <section class="workspace">
      <div id="graphWrap">
        <div id="graph"></div>
        <div id="graphHint">WebGL &middot; drag &middot; scroll</div>
        <div class="legend" id="legend"></div>
      </div>
    </section>
  </main>

  <script src="/ui/assets/app.js" defer></script>
</body>
</html>`;
}
