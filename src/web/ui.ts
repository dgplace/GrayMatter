/**
 * @file src/web/ui.ts
 * @brief Static HTML renderer for the embedded semantic graph browser.
 */

/**
 * @brief Returns the single-page UI used to browse repo stats and graph data.
 * @returns Complete HTML document string.
 */
export function renderWebUi(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeBrain Graph Browser</title>
  <style>
    :root {
      --bg: #f4efe6;
      --ink: #13201b;
      --muted: #476258;
      --panel: #fffdf8;
      --line: #d8cdbb;
      --accent: #0f7a5c;
      --accent-soft: #d0f1e4;
      --warn: #c95d1f;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: "Space Grotesk", "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, #fff8e8 0, transparent 34%),
        radial-gradient(circle at 86% 2%, #d2ece4 0, transparent 38%),
        var(--bg);
    }

    header {
      padding: 1.1rem 1.25rem;
      border-bottom: 2px solid var(--line);
      background: rgba(255, 253, 248, 0.88);
      backdrop-filter: blur(6px);
      position: sticky;
      top: 0;
      z-index: 5;
      display: grid;
      gap: 0.75rem;
      grid-template-columns: 1fr auto auto;
      align-items: end;
    }

    h1 {
      margin: 0;
      font-size: 1.15rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }

    .sub {
      margin: 0.15rem 0 0;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .control {
      display: grid;
      gap: 0.35rem;
      min-width: 210px;
    }

    label {
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
    }

    select, button {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0.55rem 0.7rem;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }

    button {
      background: linear-gradient(135deg, var(--accent) 0%, #128b67 100%);
      color: #fff;
      border: none;
      cursor: pointer;
      font-weight: 700;
    }

    main {
      padding: 1rem;
      display: grid;
      gap: 1rem;
      grid-template-columns: 320px 1fr;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 10px 24px rgba(19, 32, 27, 0.06);
      overflow: hidden;
    }

    .panel h2 {
      margin: 0;
      padding: 0.85rem 1rem;
      border-bottom: 1px solid var(--line);
      font-size: 0.86rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: var(--accent-soft);
    }

    .panel .body {
      padding: 0.9rem 1rem;
      display: grid;
      gap: 0.75rem;
      max-height: 76vh;
      overflow: auto;
    }

    .metric {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 1px dashed var(--line);
      padding-bottom: 0.3rem;
      font-size: 0.95rem;
    }

    .metric strong {
      color: var(--accent);
      font-size: 1.05rem;
    }

    .mini-list {
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 0.3rem;
      font-size: 0.89rem;
    }

    .mini-list li {
      display: flex;
      justify-content: space-between;
      gap: 0.5rem;
      border-bottom: 1px dotted var(--line);
      padding-bottom: 0.2rem;
    }

    .workspace {
      display: grid;
      gap: 0.75rem;
      grid-template-rows: auto 1fr auto;
      min-width: 0;
    }

    #graphWrap {
      position: relative;
      min-height: 450px;
      background: linear-gradient(180deg, #fff, #faf6ed);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
    }

    #graph {
      width: 100%;
      height: 100%;
      min-height: 450px;
      display: block;
    }

    .legend {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      font-size: 0.82rem;
      color: var(--muted);
      padding: 0.45rem 0.2rem;
    }

    .edge-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.83rem;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
    }

    .edge-table th,
    .edge-table td {
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 0.42rem 0.5rem;
      vertical-align: top;
    }

    .edge-table th {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: #f6f2e8;
    }

    .pill {
      display: inline-block;
      border: 1px solid #b9dccf;
      background: #e9f8f2;
      border-radius: 999px;
      padding: 0.08rem 0.45rem;
      font-size: 0.75rem;
      color: #125b45;
      font-weight: 700;
    }

    .warn {
      color: var(--warn);
      font-weight: 700;
      font-size: 0.87rem;
      margin: 0;
    }

    @media (max-width: 1000px) {
      header {
        grid-template-columns: 1fr;
        align-items: stretch;
      }

      .control {
        min-width: 0;
      }

      main {
        grid-template-columns: 1fr;
      }

      .panel .body {
        max-height: none;
      }

      #graphWrap,
      #graph {
        min-height: 360px;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>CodeBrain Semantic Graph Browser</h1>
      <p class="sub">Repo-scoped stats and semantic dependency graph exploration.</p>
    </div>
    <div class="control">
      <label for="repoSelect">Repository</label>
      <select id="repoSelect"></select>
    </div>
    <div class="control">
      <label>&nbsp;</label>
      <button id="refreshBtn" type="button">Refresh</button>
    </div>
  </header>

  <main>
    <section class="panel">
      <h2>Repository Stats</h2>
      <div class="body" id="statsBody">
        <p class="warn" id="status">Loading repositories...</p>
      </div>
    </section>

    <section class="workspace">
      <div id="graphWrap">
        <svg id="graph" viewBox="0 0 1000 620" preserveAspectRatio="xMidYMid meet"></svg>
      </div>
      <div class="legend" id="legend"></div>
      <div style="overflow:auto;">
        <table class="edge-table" id="edgeTable">
          <thead>
            <tr>
              <th>From</th>
              <th>To</th>
              <th>Kind</th>
              <th>Weight</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const repoSelect = document.getElementById('repoSelect');
    const refreshBtn = document.getElementById('refreshBtn');
    const statsBody = document.getElementById('statsBody');
    const statusEl = document.getElementById('status');
    const graphEl = document.getElementById('graph');
    const edgeTableBody = document.querySelector('#edgeTable tbody');
    const legend = document.getElementById('legend');

    function esc(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    async function getJson(url) {
      const response = await fetch(url);
      if (!response.ok) {
        const body = await response.text();
        throw new Error(body || ('HTTP ' + response.status));
      }
      return response.json();
    }

    function updateStatus(message) {
      statusEl.textContent = message;
    }

    function buildMiniList(items, keyLabel) {
      if (!items.length) {
        return '<p class="warn">No data.</p>';
      }
      return '<ul class="mini-list">' + items.map((item) => (
        '<li><span>' + esc(item[keyLabel]) + '</span><strong>' + Number(item.count).toLocaleString() + '</strong></li>'
      )).join('') + '</ul>';
    }

    function renderStats(stats) {
      const summary = stats.summary;
      statsBody.innerHTML = [
        '<div class="metric"><span>Repo</span><strong>' + esc(summary.repo) + '</strong></div>',
        '<div class="metric"><span>Files</span><strong>' + Number(summary.total_files).toLocaleString() + '</strong></div>',
        '<div class="metric"><span>Lines</span><strong>' + Number(summary.total_lines).toLocaleString() + '</strong></div>',
        '<div class="metric"><span>Chunks</span><strong>' + Number(summary.total_chunks).toLocaleString() + '</strong></div>',
        '<div class="metric"><span>Symbols</span><strong>' + Number(summary.total_symbols).toLocaleString() + '</strong></div>',
        '<h3 style="margin:0.2rem 0 0;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.08em;color:#476258;">Languages</h3>',
        buildMiniList(stats.languages.slice(0, 12), 'language'),
        '<h3 style="margin:0.2rem 0 0;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.08em;color:#476258;">Intents</h3>',
        buildMiniList(stats.intents.slice(0, 12), 'intent'),
        '<h3 style="margin:0.2rem 0 0;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.08em;color:#476258;">Symbol Kinds</h3>',
        buildMiniList(stats.symbolKinds.slice(0, 12), 'kind'),
      ].join('');
    }

    function degreeColor(degree, maxDegree) {
      const scale = maxDegree > 0 ? degree / maxDegree : 0;
      const hue = 154 - Math.round(scale * 36);
      const light = 78 - Math.round(scale * 24);
      return 'hsl(' + hue + ' 72% ' + light + '%)';
    }

    function renderGraph(graph) {
      const maxNodes = 80;
      const nodes = graph.nodes.slice(0, maxNodes);
      const nodeIds = new Set(nodes.map((node) => node.id));
      const edges = graph.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
      const maxDegree = nodes.reduce((acc, node) => Math.max(acc, node.degree), 1);
      const maxWeight = edges.reduce((acc, edge) => Math.max(acc, edge.weight), 1);

      const cx = 500;
      const cy = 310;
      const outer = 255;
      const inner = 145;
      const positions = new Map();
      for (let i = 0; i < nodes.length; i += 1) {
        const ring = i < Math.ceil(nodes.length * 0.42) ? inner : outer;
        const theta = (i / Math.max(nodes.length, 1)) * Math.PI * 2 - Math.PI / 2;
        positions.set(nodes[i].id, {
          x: cx + Math.cos(theta) * ring,
          y: cy + Math.sin(theta) * ring,
        });
      }

      const edgeSvg = edges.map((edge) => {
        const from = positions.get(edge.source);
        const to = positions.get(edge.target);
        if (!from || !to) {
          return '';
        }
        const stroke = 0.7 + (edge.weight / maxWeight) * 2.4;
        return '<line x1="' + from.x.toFixed(2) + '" y1="' + from.y.toFixed(2) + '" x2="' + to.x.toFixed(2) + '" y2="' + to.y.toFixed(2) + '" stroke="#447466" stroke-opacity="0.2" stroke-width="' + stroke.toFixed(2) + '" />';
      }).join('');

      const nodeSvg = nodes.map((node, index) => {
        const point = positions.get(node.id);
        const radius = 4 + Math.min(12, (node.degree / maxDegree) * 11);
        const label = index < 24 ? ('<text x="' + (point.x + 8).toFixed(2) + '" y="' + (point.y + 3).toFixed(2) + '" fill="#13201b" font-size="10">' + esc(node.id.split('/').slice(-1)[0]) + '</text>') : '';
        return '<g data-node="' + esc(node.id) + '" style="cursor:pointer">'
          + '<circle cx="' + point.x.toFixed(2) + '" cy="' + point.y.toFixed(2) + '" r="' + radius.toFixed(2) + '" fill="' + degreeColor(node.degree, maxDegree) + '" stroke="#2b4f43" stroke-width="1" />'
          + label
          + '</g>';
      }).join('');

      graphEl.innerHTML = '<rect x="0" y="0" width="1000" height="620" fill="url(#bggrid)" />'
        + '<defs><pattern id="bggrid" width="26" height="26" patternUnits="userSpaceOnUse"><path d="M 26 0 L 0 0 0 26" fill="none" stroke="#efe6d7" stroke-width="1"/></pattern></defs>'
        + edgeSvg
        + nodeSvg;

      legend.innerHTML = [
        '<span><span class="pill">Nodes</span> ' + nodes.length + '</span>',
        '<span><span class="pill">Edges</span> ' + edges.length + '</span>',
        '<span>Click a node to filter the edge table.</span>',
      ].join('');

      bindNodeClicks(edges);
      renderEdgeTable(edges, null);
    }

    function renderEdgeTable(edges, nodeFilter) {
      const rows = (nodeFilter
        ? edges.filter((edge) => edge.source === nodeFilter || edge.target === nodeFilter)
        : edges).slice(0, 180);

      edgeTableBody.innerHTML = rows.map((edge) => (
        '<tr>'
        + '<td>' + esc(edge.source) + '</td>'
        + '<td>' + esc(edge.target) + '</td>'
        + '<td><span class="pill">' + esc(edge.kind) + '</span></td>'
        + '<td>' + Number(edge.weight).toLocaleString() + '</td>'
        + '</tr>'
      )).join('');
    }

    function bindNodeClicks(edges) {
      graphEl.querySelectorAll('g[data-node]').forEach((group) => {
        group.addEventListener('click', () => {
          const nodeId = group.getAttribute('data-node');
          renderEdgeTable(edges, nodeId);
          legend.innerHTML = '<span><span class="pill">Filtered</span> ' + esc(nodeId) + '</span><span>Click refresh to reset table.</span>';
        });
      });
    }

    async function loadRepo(repo) {
      updateStatus('Loading stats and graph for ' + repo + '...');
      const encodedRepo = encodeURIComponent(repo);
      const [stats, graph] = await Promise.all([
        getJson('/ui/api/repos/' + encodedRepo + '/stats'),
        getJson('/ui/api/repos/' + encodedRepo + '/graph?limit=350'),
      ]);
      renderStats(stats);
      renderGraph(graph);
      updateStatus('Showing ' + repo + '.');
    }

    async function boot() {
      try {
        const data = await getJson('/ui/api/repos');
        const repos = data.repositories || [];
        if (!repos.length) {
          updateStatus('No indexed repositories found. Run ingestion first.');
          repoSelect.innerHTML = '<option value="">No repositories</option>';
          return;
        }

        repoSelect.innerHTML = repos.map((repo) => (
          '<option value="' + esc(repo.repo) + '">' + esc(repo.repo) + ' (' + Number(repo.total_files).toLocaleString() + ' files)</option>'
        )).join('');

        await loadRepo(repoSelect.value);
      } catch (error) {
        updateStatus('Failed to load repositories: ' + (error && error.message ? error.message : String(error)));
      }
    }

    repoSelect.addEventListener('change', async () => {
      if (!repoSelect.value) {
        return;
      }
      try {
        await loadRepo(repoSelect.value);
      } catch (error) {
        updateStatus('Failed to load repo data: ' + (error && error.message ? error.message : String(error)));
      }
    });

    refreshBtn.addEventListener('click', async () => {
      if (!repoSelect.value) {
        return;
      }
      try {
        await loadRepo(repoSelect.value);
      } catch (error) {
        updateStatus('Refresh failed: ' + (error && error.message ? error.message : String(error)));
      }
    });

    boot();
  </script>
</body>
</html>`;
}
