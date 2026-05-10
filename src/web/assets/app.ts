/**
 * @file src/web/assets/app.ts
 * @brief Browser entrypoint for the CodeBrain web UI. Owns operational panel
 *        rendering (Repository Stats, Module Intents, Index Management, MCP
 *        Tool Calls) and the 3D WebGL graph renderer (3d-force-graph + Three.js,
 *        rotate/zoom/pan via OrbitControls). Bundled to
 *        dist/src/web/assets/app.js by scripts/build-ui.mjs.
 */

import ForceGraph3D from "3d-force-graph";

interface RepoListItem { repo: string; total_files: number }
interface RepoListResponse { repositories: RepoListItem[] }

interface GraphNode { id: string; degree: number }
interface GraphEdge { source: string; target: string; kind: string; weight: number }
interface GraphResponse { nodes: GraphNode[]; edges: GraphEdge[] }

interface StatsSummary { repo: string; total_files: number; total_lines: number; total_chunks: number; total_symbols: number }
interface CountedRow { count: number; [key: string]: any }
interface RepositoryStats {
  summary: StatsSummary;
  languages: CountedRow[];
  intents: CountedRow[];
  symbolKinds: CountedRow[];
}

interface BrowseColumn { key: string; label: string }
interface BrowseTableInfo { name: string; label: string; description: string; row_count: number }
interface BrowseTablePage {
  name: string;
  label: string;
  columns: BrowseColumn[];
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
}

interface ModuleIntent {
  module_name?: string;
  module_path: string;
  kind: string;
  role?: string;
  dominant_intent?: string;
  file_count: number;
  chunk_count: number;
  summary?: string;
}

interface Cluster {
  id: number;
  cluster_key: string;
  name: string;
  summary: string;
  modularity: number;
  granularity: string;
  size: number;
}

interface ClusterMember {
  file_path?: string;
  symbol_name?: string;
}

interface Cycle {
  cycle_hash: string;
  cycle_size: number;
  member_file_ids: number[];
  member_paths: string[];
}

interface ToolCallSnapshot {
  total_calls: number;
  tool_calls: { name: string; count: number }[];
}

type ForceLink = { source: string; target: string; kind: string; weight: number };
type ForceNode = { id: string; degree: number };
type ForceGraphInstance = ReturnType<typeof ForceGraph3D> extends (el: HTMLElement) => infer R ? R : never;

const repoSelect = document.getElementById("repoSelect") as HTMLSelectElement;
const statsBody = document.getElementById("statsBody") as HTMLElement;
const statusEl = document.getElementById("status") as HTMLElement;
const toolCallBody = document.getElementById("toolCallBody") as HTMLElement;
const indexMgmtBody = document.getElementById("indexMgmtBody") as HTMLElement;
const modulesBody = document.getElementById("modulesBody") as HTMLElement;
const graphContainer = document.getElementById("graph") as HTMLElement;
const legend = document.getElementById("legend") as HTMLElement;

let toolCallPollId: number | null = null;
let graphInstance: ForceGraphInstance | null = null;
let lastGraphPayload: GraphResponse | null = null;
const hiddenKinds = new Set<string>();
const hiddenNodes = new Set<string>();

const tokens = {
  edgeDefault: "#5d6884",
  edgeKindMap: {
    call: "#00d3a7",
    member_call: "#2bb8ff",
    instantiation: "#b269ff",
    type_reference: "#ff6fbf",
    depends_on: "#ffb547",
    imports: "#ffb547",
    extends: "#ff5f3a",
    implements: "#ff5f3a",
    returns: "#7a8aff",
    field_type: "#7a8aff",
  } as Record<string, string>,
  nodeAccent: "#00b08c",
  nodeAccentHover: "#00d3a7",
  nodeMuted: "#b0b6c4",
  background: "#f6f8ff",
  cycleEdge: "#ff3333",
  folderPalette: [
    "#00b08c", "#2bb8ff", "#b269ff", "#ff6fbf",
    "#ffb547", "#ff5f3a", "#7a8aff", "#3acb6c",
    "#f25c7d", "#ffa726", "#26c6da", "#ab47bc",
    "#9ccc65", "#ec407a",
  ],
};

const clusterColors = new Map<number, string>();
const nodeToCluster = new Map<string, number>();
const clusterInfo = new Map<number, Cluster>();
const cycleEdges = new Set<string>();
const folderColors = new Map<string, string>();

/** @brief Normalize file paths for consistent map keys (strips leading ./). */
function normalizePath(p: string): string {
  return p.replace(/^\.\//, "");
}

/**
 * @brief Grouping key for the folder a node belongs to. Uses the first two
 *        path segments of the directory portion so that all files under, e.g.,
 *        `a/b/c/...` and `a/b/d/...` collapse onto the same `a/b` group.
 *        Top-level files map to `<root>`.
 */
function folderKey(id: string): string {
  const slash = id.lastIndexOf("/");
  if (slash <= 0) return "<root>";
  return id.slice(0, slash).split("/").slice(0, 2).join("/");
}

/**
 * @brief Assign a deterministic colour to each folder present in the payload
 *        so unclustered nodes still get a stable, distinguishable colour.
 */
function buildFolderColors(payload: GraphResponse): void {
  folderColors.clear();
  const folders = Array.from(new Set(payload.nodes.map((n) => folderKey(n.id)))).sort();
  for (let i = 0; i < folders.length; i += 1) {
    folderColors.set(folders[i], tokens.folderPalette[i % tokens.folderPalette.length]);
  }
}

/**
 * @brief Resolve a 3d-force-graph link endpoint to its node id. After the
 *        force simulation has run, source/target are upgraded from string
 *        ids to live node references.
 */
function endpointId(endpoint: any): string {
  const id = typeof endpoint === "object" && endpoint !== null ? String(endpoint.id) : String(endpoint);
  return normalizePath(id);
}

/**
 * @brief Visibility predicate for a single link, accounting for both the
 *        kind-toggle filter and the per-node hide-on-click filter.
 */
function isLinkVisible(l: ForceLink): boolean {
  if (hiddenKinds.has(l.kind)) return false;
  if (hiddenNodes.has(endpointId(l.source)) || hiddenNodes.has(endpointId(l.target))) return false;
  return true;
}

/**
 * @brief Color accessor: muted grey for hidden nodes, the cluster colour when
 *        the node belongs to a file-granularity cluster, otherwise a
 *        deterministic per-folder colour so unclustered nodes still vary.
 */
function colorForNode(n: ForceNode): string {
  if (hiddenNodes.has(n.id)) return tokens.nodeMuted;
  const cid = nodeToCluster.get(normalizePath(n.id));
  if (cid !== undefined && clusterColors.has(cid)) {
    return clusterColors.get(cid)!;
  }
  return folderColors.get(folderKey(n.id)) || tokens.nodeAccent;
}

/** @brief Check if an edge is part of a dependency cycle. */
function isCycleEdge(l: ForceLink): boolean {
  return cycleEdges.has(endpointId(l.source) + "|" + endpointId(l.target));
}

/** @brief Edge color accessor: red for cycles, otherwise by kind. */
function colorForEdge(l: ForceLink): string {
  if (isCycleEdge(l)) return tokens.cycleEdge;
  return colorForEdgeKind(l.kind);
}

/** @brief Escape a value for safe HTML interpolation. */
function esc(value: unknown): string {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** @brief Fetch JSON from a URL or throw with body context on non-2xx. */
async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || ("HTTP " + response.status));
  }
  return response.json() as Promise<T>;
}

/** @brief Set the loading-status message in the stats panel. */
function updateStatus(message: string): void {
  statusEl.textContent = message;
}

/** @brief Render a name/count list inside a panel section. */
function buildMiniList(items: CountedRow[], keyLabel: string): string {
  if (!items.length) {
    return '<p class="warn">No data.</p>';
  }
  return '<ul class="mini-list">' + items.map((item) => (
    "<li><span>" + esc(item[keyLabel]) + "</span><strong>" + Number(item.count).toLocaleString() + "</strong></li>"
  )).join("") + "</ul>";
}

/** @brief Render the Repository Stats panel from a stats payload. */
function renderStats(stats: RepositoryStats): void {
  const summary = stats.summary;
  statsBody.innerHTML = [
    '<div class="metric"><span>Repo</span><strong>' + esc(summary.repo) + "</strong></div>",
    '<div class="metric"><span>Files</span><strong>' + Number(summary.total_files).toLocaleString() + "</strong></div>",
    '<div class="metric"><span>Lines</span><strong>' + Number(summary.total_lines).toLocaleString() + "</strong></div>",
    '<div class="metric"><span>Chunks</span><strong>' + Number(summary.total_chunks).toLocaleString() + "</strong></div>",
    '<div class="metric"><span>Symbols</span><strong>' + Number(summary.total_symbols).toLocaleString() + "</strong></div>",
    '<h3 style="margin:0.2rem 0 0;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.1em;color:var(--muted);">Languages</h3>',
    buildMiniList(stats.languages.slice(0, 12), "language"),
    '<h3 style="margin:0.2rem 0 0;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.1em;color:var(--muted);">Intents</h3>',
    buildMiniList(stats.intents.slice(0, 12), "intent"),
    '<h3 style="margin:0.2rem 0 0;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.1em;color:var(--muted);">Symbol Kinds</h3>',
    buildMiniList(stats.symbolKinds.slice(0, 12), "kind"),
  ].join("");
}

/** @brief Render the MCP Tool Calls panel from a snapshot payload. */
function renderToolCalls(snapshot: ToolCallSnapshot): void {
  const toolCalls = snapshot.tool_calls || [];
  const totalCalls = Number(snapshot.total_calls || 0);
  if (!toolCalls.length) {
    toolCallBody.innerHTML = '<div class="metric"><span>Total Calls</span><strong>' + totalCalls.toLocaleString() + '</strong></div><p class="warn">No MCP tool calls yet.</p>';
    return;
  }
  toolCallBody.innerHTML = [
    '<div class="metric"><span>Total Calls</span><strong>' + totalCalls.toLocaleString() + "</strong></div>",
    ...toolCalls.map((tc) => '<div class="metric"><span>' + esc(tc.name) + "</span><strong>" + Number(tc.count).toLocaleString() + "</strong></div>"),
  ].join("");
}

/** @brief Poll the tool-call snapshot endpoint and re-render the panel. */
async function refreshToolCalls(): Promise<void> {
  try {
    const snapshot = await getJson<ToolCallSnapshot>("/ui/api/tool-calls");
    renderToolCalls(snapshot);
  } catch (error: any) {
    toolCallBody.innerHTML = '<p class="warn">Failed to load tool call counters: ' + esc(error?.message || String(error)) + "</p>";
  }
}

/**
 * @brief Render the Index Management panel: two action buttons (View / Delete)
 *        scoped to the currently selected repository.
 */
function renderIndexMgmt(repo: string): void {
  indexMgmtBody.innerHTML = [
    '<button id="viewIndexBtn" type="button">View Index</button>',
    '<button class="btn-danger" id="deleteIndexBtn" type="button">Delete Index</button>',
  ].join("");

  const viewBtn = document.getElementById("viewIndexBtn") as HTMLButtonElement;
  viewBtn.addEventListener("click", () => { void openIndexModal(repo); });

  const deleteBtn = document.getElementById("deleteIndexBtn") as HTMLButtonElement;
  deleteBtn.addEventListener("click", async () => {
    if (!repo) return;
    if (!confirm('Permanently delete the index for "' + repo + '"? This cannot be undone.')) return;
    deleteBtn.disabled = true;
    deleteBtn.textContent = "Deleting...";
    try {
      const res = await fetch("/ui/api/repos/" + encodeURIComponent(repo), { method: "DELETE" });
      if (!res.ok) {
        const body: any = await res.json().catch(() => ({}));
        throw new Error(body.error || ("HTTP " + res.status));
      }
      const data: { deleted_files: number } = await res.json();
      indexMgmtBody.innerHTML = '<p class="warn">Index deleted (' + Number(data.deleted_files).toLocaleString() + " files removed). Refreshing...</p>";
      setTimeout(() => { void reloadRepoList(); }, 800);
    } catch (e: any) {
      indexMgmtBody.innerHTML = '<p class="warn">Delete failed: ' + esc(e?.message || String(e)) + "</p>";
    }
  });
}

/** @brief Lazily create the index-browser modal DOM and return its references. */
function ensureIndexModal(): {
  overlay: HTMLElement; titleEl: HTMLElement; tabs: HTMLElement; pane: HTMLElement;
} {
  let overlay = document.getElementById("indexModal") as HTMLElement | null;
  if (overlay) {
    return {
      overlay,
      titleEl: document.getElementById("indexModalTitle") as HTMLElement,
      tabs: document.getElementById("indexModalTabs") as HTMLElement,
      pane: document.getElementById("indexModalPane") as HTMLElement,
    };
  }
  overlay = document.createElement("div");
  overlay.id = "indexModal";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = [
    '<div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="indexModalTitle">',
    '  <header class="modal-header">',
    '    <h2 id="indexModalTitle">Index</h2>',
    '    <button class="modal-close" id="indexModalClose" type="button" aria-label="Close">&times;</button>',
    '  </header>',
    '  <nav class="modal-tabs" id="indexModalTabs"></nav>',
    '  <div class="modal-pane" id="indexModalPane"><p class="warn">Loading...</p></div>',
    '</div>',
  ].join("");
  document.body.appendChild(overlay);

  const close = () => closeIndexModal();
  (document.getElementById("indexModalClose") as HTMLButtonElement).addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay && !overlay.hidden) close();
  });

  return {
    overlay,
    titleEl: document.getElementById("indexModalTitle") as HTMLElement,
    tabs: document.getElementById("indexModalTabs") as HTMLElement,
    pane: document.getElementById("indexModalPane") as HTMLElement,
  };
}

/** @brief Hide the index-browser modal. */
function closeIndexModal(): void {
  const overlay = document.getElementById("indexModal");
  if (overlay) overlay.hidden = true;
}

/**
 * @brief Open the View Index modal for a repo: fetch the table list, render
 *        a tab per table, and show the first tab.
 */
async function openIndexModal(repo: string): Promise<void> {
  const refs = ensureIndexModal();
  refs.titleEl.textContent = 'Index — ' + repo;
  refs.tabs.innerHTML = "";
  refs.pane.innerHTML = '<p class="warn">Loading tables...</p>';
  refs.overlay.hidden = false;

  try {
    const data = await getJson<{ tables: BrowseTableInfo[] }>(
      "/ui/api/repos/" + encodeURIComponent(repo) + "/tables",
    );
    const tables = data.tables || [];
    if (!tables.length) {
      refs.pane.innerHTML = '<p class="warn">No browseable tables for this repository.</p>';
      return;
    }
    refs.tabs.innerHTML = tables
      .map((t) => (
        '<button class="modal-tab" type="button" data-table="' + esc(t.name) + '" title="' + esc(t.description) + '">'
        + esc(t.label) + ' <span class="modal-tab-count">' + Number(t.row_count).toLocaleString() + '</span>'
        + '</button>'
      ))
      .join("");
    refs.tabs.querySelectorAll<HTMLButtonElement>("button.modal-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        refs.tabs.querySelectorAll(".modal-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        void loadIndexTablePage(repo, btn.dataset.table || "", 0);
      });
    });
    const first = refs.tabs.querySelector<HTMLButtonElement>("button.modal-tab");
    if (first) {
      first.classList.add("active");
      void loadIndexTablePage(repo, first.dataset.table || "", 0);
    }
  } catch (e: any) {
    refs.pane.innerHTML = '<p class="warn">Failed to load tables: ' + esc(e?.message || String(e)) + "</p>";
  }
}

/**
 * @brief Fetch one page of a table and render it into the modal pane with
 *        Prev/Next pagination controls.
 */
async function loadIndexTablePage(repo: string, table: string, offset: number): Promise<void> {
  const refs = ensureIndexModal();
  refs.pane.innerHTML = '<p class="warn">Loading ' + esc(table) + '...</p>';
  const limit = 100;
  try {
    const url = "/ui/api/repos/" + encodeURIComponent(repo)
      + "/tables/" + encodeURIComponent(table)
      + "?limit=" + limit + "&offset=" + Math.max(0, offset);
    const page = await getJson<BrowseTablePage>(url);
    refs.pane.innerHTML = renderTablePageHtml(page);
    wireTablePageControls(refs.pane, repo, page);
  } catch (e: any) {
    refs.pane.innerHTML = '<p class="warn">Failed to load ' + esc(table) + ': ' + esc(e?.message || String(e)) + "</p>";
  }
}

/** @brief Format any cell value into a compact HTML-safe string. */
function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '<span class="cell-null">null</span>';
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return esc("[" + value.map((v) => String(v)).join(", ") + "]");
  if (typeof value === "object") return esc(JSON.stringify(value));
  const str = String(value);
  if (str.length > 240) return esc(str.slice(0, 240)) + '<span class="cell-trunc">…</span>';
  return esc(str);
}

/** @brief Build the HTML for a single table page (header bar + table + pager). */
function renderTablePageHtml(page: BrowseTablePage): string {
  const start = page.total === 0 ? 0 : page.offset + 1;
  const end = Math.min(page.total, page.offset + page.rows.length);
  const headerCells = page.columns.map((c) => '<th>' + esc(c.label) + '</th>').join("");
  const bodyRows = page.rows.length
    ? page.rows.map((row) => (
        '<tr>' + page.columns.map((c) => '<td>' + formatCell(row[c.key]) + '</td>').join("") + '</tr>'
      )).join("")
    : '<tr><td class="cell-empty" colspan="' + page.columns.length + '">No rows.</td></tr>';
  const prevDisabled = page.offset <= 0 ? ' disabled' : '';
  const nextDisabled = end >= page.total ? ' disabled' : '';
  return [
    '<div class="modal-pager">',
    '  <span class="modal-pager-info">',
    esc(page.label) + ' &middot; ' + start.toLocaleString() + '–' + end.toLocaleString()
      + ' of ' + Number(page.total).toLocaleString(),
    '  </span>',
    '  <span class="modal-pager-buttons">',
    '    <button type="button" data-page="prev"' + prevDisabled + '>Prev</button>',
    '    <button type="button" data-page="next"' + nextDisabled + '>Next</button>',
    '  </span>',
    '</div>',
    '<div class="modal-table-wrap">',
    '  <table class="modal-table"><thead><tr>' + headerCells + '</tr></thead>',
    '  <tbody>' + bodyRows + '</tbody></table>',
    '</div>',
  ].join("");
}

/** @brief Wire up Prev/Next buttons to fetch adjacent pages of the same table. */
function wireTablePageControls(pane: HTMLElement, repo: string, page: BrowseTablePage): void {
  const prev = pane.querySelector<HTMLButtonElement>('button[data-page="prev"]');
  const next = pane.querySelector<HTMLButtonElement>('button[data-page="next"]');
  if (prev) prev.addEventListener("click", () => {
    void loadIndexTablePage(repo, page.name, Math.max(0, page.offset - page.limit));
  });
  if (next) next.addEventListener("click", () => {
    void loadIndexTablePage(repo, page.name, page.offset + page.limit);
  });
}

/** @brief Render the Module Intents panel from a list of modules. */
function renderModules(modules: ModuleIntent[]): void {
  if (!modules.length) {
    modulesBody.innerHTML = '<p class="warn">No modules found. Run module synthesis from the Desktop application or CLI.</p>';
    return;
  }
  modulesBody.innerHTML = modules.map((m) => (
    '<div style="margin-bottom:1rem; border-bottom:1px solid var(--line); padding-bottom:0.5rem;">'
    + '<div class="metric"><span style="font-weight:bold;">' + esc(m.module_name || m.module_path) + '</span><span class="pill">' + esc(m.kind) + "</span></div>"
    + '<div style="font-size:0.83rem; color:var(--muted); margin:0.3rem 0;">' + esc(m.role || "unknown") + " &bull; " + esc(m.dominant_intent || "unknown") + "</div>"
    + '<div style="font-size:0.8rem; margin-bottom:0.3rem;">' + Number(m.file_count) + " files, " + Number(m.chunk_count) + " chunks</div>"
    + '<div style="font-size:0.87rem;">' + esc(m.summary || "") + "</div>"
    + "</div>"
  )).join("");
}

/** @brief Fetch and render module intents for a repo. */
async function loadModules(repo: string): Promise<void> {
  try {
    const data = await getJson<{ modules: ModuleIntent[] }>("/ui/api/repos/" + encodeURIComponent(repo) + "/modules");
    renderModules(data.modules || []);
  } catch (e: any) {
    modulesBody.innerHTML = '<p class="warn">Failed to load modules: ' + esc(e?.message || String(e)) + "</p>";
  }
}

/** @brief Pick the neon color for an edge given its kind. */
function colorForEdgeKind(kind: string): string {
  return tokens.edgeKindMap[kind] || tokens.edgeDefault;
}

/** @brief Display label for a node id (basename of path-like ids). */
function shortLabel(id: string): string {
  const slash = id.lastIndexOf("/");
  return slash >= 0 ? id.slice(slash + 1) : id;
}

/**
 * @brief Render a color-swatch legend that maps each edge kind to its arrow
 *        colour, plus node/edge counts and a rotate hint.
 */
function renderLegend(payload: GraphResponse): void {
  const counts = new Map<string, number>();
  for (const e of payload.edges) counts.set(e.kind, (counts.get(e.kind) || 0) + 1);

  const visibleEdgeCount = payload.edges.filter((e) => (
    !hiddenKinds.has(e.kind)
    && !hiddenNodes.has(e.source)
    && !hiddenNodes.has(e.target)
  )).length;
  const visibleNodeCount = payload.nodes.length - hiddenNodes.size;

  const swatches = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => {
      const color = colorForEdgeKind(kind);
      const hidden = hiddenKinds.has(kind);
      const cls = "legend-item" + (hidden ? " legend-item-hidden" : "");
      const title = hidden ? "Show " + kind + " edges" : "Hide " + kind + " edges";
      return '<button type="button" class="' + cls + '" data-kind="' + esc(kind) + '" title="' + esc(title) + '" aria-pressed="' + (hidden ? "false" : "true") + '">'
        + '<span class="legend-arrow" style="background:' + color + ';color:' + color + ';box-shadow:0 0 6px ' + color + '99;"></span>'
        + '<span class="legend-label">' + esc(kind) + "</span>"
        + '<span class="legend-count">' + n + "</span>"
        + "</button>";
    }).join("");

  const clusterCounts = new Map<number, number>();
  for (const n of payload.nodes) {
    const cid = nodeToCluster.get(normalizePath(n.id));
    if (cid !== undefined) {
      clusterCounts.set(cid, (clusterCounts.get(cid) || 0) + 1);
    }
  }
  const clusterSwatches = Array.from(clusterCounts.entries())
    .sort((a, b) => b[1] - a[1] || a[0] - b[0])
    .map(([cid, n]) => {
      const color = clusterColors.get(cid) || tokens.nodeAccent;
      const cinfo = clusterInfo.get(cid);
      const name = cinfo ? cinfo.name : "Cluster " + cid;
      return '<span class="legend-item legend-item-static" title="' + esc(name) + '">'
        + '<span class="legend-dot" style="background:' + color + ';box-shadow:0 0 6px ' + color + '99;"></span>'
        + '<span class="legend-label">' + esc(name) + "</span>"
        + '<span class="legend-count">' + n + "</span>"
        + "</span>";
    }).join("");

  let cycleSwatch = "";
  if (cycleEdges.size > 0) {
    cycleSwatch = '<span class="legend-item legend-item-static" title="Dependency Cycle">'
      + '<span class="legend-arrow" style="background:' + tokens.cycleEdge + ';color:' + tokens.cycleEdge + ';box-shadow:0 0 8px ' + tokens.cycleEdge + ';"></span>'
      + '<span class="legend-label">Cycle</span>'
      + '<span class="legend-count">' + cycleEdges.size + "</span>"
      + "</span>";
  }

  legend.innerHTML = [
    '<span class="legend-section legend-counts">',
    '<span class="pill">Nodes ' + visibleNodeCount + " / " + payload.nodes.length + "</span>",
    '<span class="pill">Edges ' + visibleEdgeCount + " / " + payload.edges.length + "</span>",
    "</span>",
    '<span class="legend-section legend-folders">' + clusterSwatches + cycleSwatch + "</span>",
    '<span class="legend-section">' + swatches + "</span>",
    '<span class="legend-section legend-hint">Node colour = cluster. Click a kind to toggle its edges; click a node to mute it. Drag to rotate, scroll to zoom.</span>',
  ].join("");
}

/**
 * @brief Toggle visibility for an edge kind and refresh the graph + legend.
 *        Re-renders the legend to update the dimmed state and visible-edge
 *        count, and nudges 3d-force-graph to re-evaluate `linkVisibility`.
 */
function toggleKind(kind: string): void {
  if (hiddenKinds.has(kind)) hiddenKinds.delete(kind);
  else hiddenKinds.add(kind);
  if (lastGraphPayload) renderLegend(lastGraphPayload);
  if (graphInstance) {
    graphInstance.linkVisibility(isLinkVisible);
    graphInstance.refresh();
  }
}

/**
 * @brief Toggle visibility of a single node and all its incident edges.
 *        Hidden nodes render in a muted grey; their links are hidden via the
 *        shared `isLinkVisible` predicate.
 */
function toggleNode(nodeId: string): void {
  if (hiddenNodes.has(nodeId)) hiddenNodes.delete(nodeId);
  else hiddenNodes.add(nodeId);
  if (lastGraphPayload) renderLegend(lastGraphPayload);
  if (graphInstance) {
    graphInstance.nodeColor(colorForNode);
    graphInstance.linkVisibility(isLinkVisible);
    graphInstance.refresh();
  }
}

legend.addEventListener("click", (ev) => {
  const target = ev.target as HTMLElement | null;
  const item = target?.closest(".legend-item") as HTMLElement | null;
  if (!item) return;
  const kind = item.getAttribute("data-kind");
  if (kind) toggleKind(kind);
});

/** @brief Convert API edges to 3d-force-graph link objects. */
function toLinks(payload: GraphResponse): ForceLink[] {
  const ids = new Set(payload.nodes.map((n) => n.id));
  return payload.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, kind: e.kind, weight: e.weight }));
}

/** @brief Convert API nodes to 3d-force-graph node objects. */
function toNodes(payload: GraphResponse): ForceNode[] {
  return payload.nodes.map((n) => ({ id: n.id, degree: n.degree }));
}

/**
 * @brief Build (or refresh) the 3D force-directed graph with the given payload.
 *        Uses the existing instance if one is mounted to avoid recreating the
 *        WebGL context.
 */
function renderGraph(payload: GraphResponse): void {
  buildFolderColors(payload);
  const maxDegree = payload.nodes.reduce((acc, n) => Math.max(acc, n.degree), 1);
  const nodeRadius = (n: ForceNode) => 1.2 + Math.min(8, (n.degree / maxDegree) * 7);
  const linkWidth = (l: ForceLink) => {
    const base = Math.max(0.4, Math.min(3.2, 0.4 + Math.log2(1 + l.weight)));
    return isCycleEdge(l) ? base * 2.5 : base;
  };

  const data = { nodes: toNodes(payload), links: toLinks(payload) };

  if (!graphInstance) {
    graphInstance = ForceGraph3D()(graphContainer)
      .backgroundColor(tokens.background)
      .showNavInfo(false)
      .nodeRelSize(4)
      .nodeOpacity(0.95)
      .linkOpacity(0.7)
      .linkDirectionalArrowLength(3.5)
      .linkDirectionalArrowRelPos(0.92)
      .linkCurvature(0.18)
      .nodeColor(colorForNode)
      .nodeLabel((n: ForceNode) => `<span style="background:#fff;color:#0d1320;padding:2px 6px;border-radius:6px;border:1px solid #dbe1f1;font:600 11px Inter,sans-serif;">${esc(shortLabel(n.id))}</span>`)
      .nodeVal(nodeRadius)
      .linkColor(colorForEdge)
      .linkDirectionalArrowColor(colorForEdge)
      .linkWidth(linkWidth)
      .linkVisibility(isLinkVisible)
      .onNodeClick((n: ForceNode) => toggleNode(n.id));
  } else {
    graphInstance
      .nodeVal(nodeRadius)
      .linkWidth(linkWidth);
  }

  graphInstance.graphData(data);
  // Resize once on (re)mount so the renderer matches the container after
  // the layout has settled.
  requestAnimationFrame(() => {
    if (graphInstance) {
      graphInstance.width(graphContainer.clientWidth);
      graphInstance.height(graphContainer.clientHeight);
    }
  });
}

/** @brief Load all data for a repo: stats, graph, modules. */
async function loadRepo(repo: string): Promise<void> {
  updateStatus("Loading stats and graph for " + repo + "...");
  renderIndexMgmt(repo);
  const encodedRepo = encodeURIComponent(repo);
  const [stats, graph, clustersResp, cyclesResp] = await Promise.all([
    getJson<RepositoryStats>("/ui/api/repos/" + encodedRepo + "/stats"),
    getJson<GraphResponse>("/ui/api/repos/" + encodedRepo + "/graph?limit=350"),
    getJson<{ clusters: Cluster[] }>("/ui/api/repos/" + encodedRepo + "/clusters?granularity=file").catch(() => ({ clusters: [] })),
    getJson<{ cycles: Cycle[] }>("/ui/api/repos/" + encodedRepo + "/cycles").catch(() => ({ cycles: [] })),
  ]);

  const clusters = clustersResp.clusters || [];
  clusterInfo.clear();
  clusterColors.clear();
  nodeToCluster.clear();
  cycleEdges.clear();

  for (let i = 0; i < clusters.length; i++) {
    const c = clusters[i];
    clusterInfo.set(c.id, c);
    clusterColors.set(c.id, tokens.folderPalette[i % tokens.folderPalette.length]);
  }

  const memberPromises = clusters.map(c => 
    getJson<{ members: ClusterMember[] }>("/ui/api/repos/" + encodedRepo + "/clusters/" + c.id + "/members?limit=1000").catch(() => ({ members: [] }))
  );
  const membersResults = await Promise.all(memberPromises);

  for (let i = 0; i < clusters.length; i++) {
    const c = clusters[i];
    const members = membersResults[i].members || [];
    for (const m of members) {
      if (m.file_path) nodeToCluster.set(normalizePath(m.file_path), c.id);
    }
  }

  const cycles = cyclesResp.cycles || [];
  for (const cycle of cycles) {
    const paths = new Set(cycle.member_paths || []);
    // Note: This intersection highlights all edges whose endpoints both sit in the cycle's member paths.
    // This heuristically includes non-cycle edges (like type references) between cycle members.
    for (const e of graph.edges) {
      if (paths.has(e.source) && paths.has(e.target)) {
        cycleEdges.add(e.source + "|" + e.target);
      }
    }
  }

  renderStats(stats);
  hiddenKinds.clear();
  hiddenNodes.clear();
  lastGraphPayload = graph;
  renderGraph(graph);
  renderLegend(graph);
  updateStatus("Showing " + repo + ".");
  void loadModules(repo);
}

/** @brief Reload the repo dropdown after deletion. */
async function reloadRepoList(): Promise<void> {
  try {
    const reposData = await getJson<RepoListResponse>("/ui/api/repos");
    const repos = reposData.repositories || [];
    repoSelect.innerHTML = repos.length
      ? repos.map((r) => '<option value="' + esc(r.repo) + '">' + esc(r.repo) + " (" + Number(r.total_files).toLocaleString() + " files)</option>").join("")
      : '<option value="">No repositories</option>';
    if (repos.length) {
      await loadRepo(repoSelect.value);
    } else {
      statsBody.innerHTML = '<p class="warn">No indexed repositories.</p>';
      indexMgmtBody.innerHTML = '<p class="warn">No indexed repositories.</p>';
    }
  } catch (e: any) {
    updateStatus("Reload failed: " + (e?.message || String(e)));
  }
}

/** @brief First-run boot: panels, polling, and initial repo load. */
async function boot(): Promise<void> {
  await refreshToolCalls();
  toolCallPollId = window.setInterval(() => { void refreshToolCalls(); }, 1500);

  try {
    const data = await getJson<RepoListResponse>("/ui/api/repos");
    const repos = data.repositories || [];
    if (!repos.length) {
      updateStatus("No indexed repositories found. Run ingestion first.");
      repoSelect.innerHTML = '<option value="">No repositories</option>';
      return;
    }
    repoSelect.innerHTML = repos.map((r) => (
      '<option value="' + esc(r.repo) + '">' + esc(r.repo) + " (" + Number(r.total_files).toLocaleString() + " files)</option>"
    )).join("");
    await loadRepo(repoSelect.value);
  } catch (error: any) {
    updateStatus("Failed to load repositories: " + (error?.message || String(error)));
  }
}

window.addEventListener("beforeunload", () => {
  if (toolCallPollId !== null) window.clearInterval(toolCallPollId);
});

window.addEventListener("resize", () => {
  if (graphInstance) {
    graphInstance.width(graphContainer.clientWidth);
    graphInstance.height(graphContainer.clientHeight);
  }
});

repoSelect.addEventListener("change", async () => {
  if (!repoSelect.value) return;
  try {
    await loadRepo(repoSelect.value);
  } catch (error: any) {
    updateStatus("Failed to load repo data: " + (error?.message || String(error)));
  }
});

void boot();
