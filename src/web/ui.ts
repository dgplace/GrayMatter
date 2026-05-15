/**
 * @file src/web/ui.ts
 * @brief HTML shell for the embedded CodeBrain web UI. The shell carries no
 *        styles or scripts inline; it loads the neon-on-light theme from
 *        /ui/assets/styles.css and the operational panel + raw index browser logic from
 *        /ui/assets/app.js (bundled by scripts/build-ui.mjs).
 */

/**
 * @brief Returns the single-page HTML shell for /ui and /ui/:repo.
 * @returns Complete HTML document string.
 */
export function renderWebUi(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeBrain Index Browser</title>
  <link rel="stylesheet" href="/ui/assets/styles.css" />
</head>
<body>
  <header>
    <div>
      <h1>CodeBrain <span class="accent">Raw</span> Index Browser</h1>
      <p class="sub">Repo-scoped stats, modules, and raw table browsing.</p>
    </div>
    <div class="control">
      <label for="repoSelect">Repository</label>
      <select id="repoSelect"></select>
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
      <div id="indexBrowserWrap">
        <header class="index-browser-header">
          <h2 id="indexBrowserTitle">Raw Index</h2>
        </header>
        <nav class="modal-tabs" id="indexBrowserTabs"></nav>
        <div class="modal-pane" id="indexBrowserPane">
          <p class="warn">Select a repository.</p>
        </div>
      </div>
    </section>
  </main>

  <script src="/ui/assets/app.js" defer></script>
</body>
</html>`;
}
