/**
 * @file src/web/assets/app.ts
 * @brief Browser entrypoint for the CodeBrain web UI. Owns operational panel
 *        rendering (Repository Stats, Index Management, MCP Tool Calls) and
 *        the inline raw index-table browser workspace.
 */

interface RepoListItem { repo: string; total_files: number }
interface RepoListResponse { repositories: RepoListItem[] }

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
interface BrowseTableFilterOption { key: string; label: string; values: string[] }
interface BrowseTablePage {
  name: string;
  label: string;
  columns: BrowseColumn[];
  filter_options: BrowseTableFilterOption[];
  active_filters: Record<string, string[]>;
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
}

interface ToolCallSnapshot {
  total_calls: number;
  tool_calls: { name: string; count: number }[];
}

interface IndexJobSnapshot {
  id: string;
  repo: string;
  repo_path: string;
  status: "running" | "completed" | "failed" | "cancelled";
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  logs: string[];
}

interface IndexJobResponse {
  job: IndexJobSnapshot;
}

const repoSelect = document.getElementById("repoSelect") as HTMLSelectElement;
const statsBody = document.getElementById("statsBody") as HTMLElement;
const statusEl = document.getElementById("status") as HTMLElement;
const toolCallBody = document.getElementById("toolCallBody") as HTMLElement;
const indexMgmtBody = document.getElementById("indexMgmtBody") as HTMLElement;
const indexBrowserTitle = document.getElementById("indexBrowserTitle") as HTMLElement;
const indexBrowserTabs = document.getElementById("indexBrowserTabs") as HTMLElement;
const indexBrowserPane = document.getElementById("indexBrowserPane") as HTMLElement;

let toolCallPollId: number | null = null;
let indexJobPollId: number | null = null;
let activeRepo = "";
let activeIndexTable = "";
const tableFiltersByTable = new Map<string, Record<string, string>>();
const UI_BASE_PATH = "/ui";

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

/** @brief Return the final folder name from a local path string. */
function basenameFromPath(path: string): string {
  const trimmed = path.trim().replace(/[\\/]+$/, "");
  const parts = trimmed.split(/[\\/]+/);
  return parts[parts.length - 1] || "";
}

/** @brief Clear active polling for an index job dialog. */
function clearIndexJobPolling(): void {
  if (indexJobPollId !== null) {
    window.clearInterval(indexJobPollId);
    indexJobPollId = null;
  }
}

/**
 * @brief Returns the repository encoded in the current `/ui/:repo` path.
 * @param pathname Absolute browser path.
 * @returns Decoded repository name, or empty when absent/invalid.
 */
function getRequestedRepoFromPath(pathname: string): string {
  const prefix = UI_BASE_PATH + "/";
  if (!pathname.startsWith(prefix)) return "";
  const encodedRepo = pathname.slice(prefix.length).split("/")[0];
  if (!encodedRepo) return "";
  try {
    return decodeURIComponent(encodedRepo);
  } catch {
    return "";
  }
}

/**
 * @brief Build the UI path for a repository selection.
 * @param repo Repository name.
 * @returns URL path under `/ui`.
 */
function getUiPathForRepo(repo: string): string {
  return repo ? UI_BASE_PATH + "/" + encodeURIComponent(repo) : UI_BASE_PATH;
}

/**
 * @brief Synchronize browser URL with the current repository selection.
 * @param repo Repository name.
 * @param mode History update strategy.
 */
function syncUrlForRepo(repo: string, mode: "push" | "replace"): void {
  const nextPath = getUiPathForRepo(repo);
  if (window.location.pathname === nextPath) return;
  if (mode === "push") {
    window.history.pushState(null, "", nextPath);
    return;
  }
  window.history.replaceState(null, "", nextPath);
}

/**
 * @brief Populate the repository dropdown options.
 * @param repos Repository list response payload.
 */
function setRepoOptions(repos: RepoListItem[]): void {
  repoSelect.innerHTML = repos.length
    ? repos.map((r) => (
      '<option value="' + esc(r.repo) + '">' + esc(r.repo) + " (" + Number(r.total_files).toLocaleString() + " files)</option>"
    )).join("")
    : '<option value="">No repositories</option>';
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

/**
 * @brief Render terminal output for an indexing job inside the modal.
 * @param job Index job snapshot from the server.
 */
function renderIndexTerminal(job: IndexJobSnapshot): void {
  const terminal = document.getElementById("indexTerminal") as HTMLElement | null;
  const status = document.getElementById("indexDialogStatus") as HTMLElement | null;
  if (!terminal || !status) return;
  const lines = [
    "Status: " + job.status,
    "Repo: " + job.repo,
    "Path: " + job.repo_path,
    job.exit_code === null ? "" : "Exit code: " + job.exit_code,
    "",
    ...job.logs,
  ].filter((line) => line !== "");
  terminal.textContent = lines.join("\n");
  terminal.scrollTop = terminal.scrollHeight;
  status.textContent = job.status === "running" ? "Indexing..." : "Index " + job.status + ".";
}

/**
 * @brief Fetch and render the latest state for an index job.
 * @param jobId Index job identifier.
 * @returns Promise resolved after the dialog is updated.
 */
async function refreshIndexJob(jobId: string): Promise<void> {
  try {
    const data = await getJson<IndexJobResponse>("/ui/api/index-jobs/" + encodeURIComponent(jobId));
    renderIndexTerminal(data.job);
    if (data.job.status !== "running") {
      clearIndexJobPolling();
      if (data.job.status === "completed" && data.job.repo === activeRepo) {
        await loadRepo(activeRepo, "replace");
      }
    }
  } catch (error: any) {
    clearIndexJobPolling();
    const terminal = document.getElementById("indexTerminal") as HTMLElement | null;
    if (terminal) {
      terminal.textContent = "Failed to load index job: " + (error?.message || String(error));
    }
  }
}

/**
 * @brief Start polling a running index job.
 * @param jobId Index job identifier.
 */
function pollIndexJob(jobId: string): void {
  clearIndexJobPolling();
  void refreshIndexJob(jobId);
  indexJobPollId = window.setInterval(() => { void refreshIndexJob(jobId); }, 1000);
}

/**
 * @brief Close and remove the index dialog.
 */
function closeIndexDialog(): void {
  clearIndexJobPolling();
  document.getElementById("indexDialogRoot")?.remove();
}

/**
 * @brief Return the chosen folder name from a directory input when available.
 * @param input Directory input element.
 * @returns Selected top-level folder name, or an empty string.
 */
function getSelectedDirectoryName(input: HTMLInputElement): string {
  const firstFile = input.files?.item(0);
  if (!firstFile) return "";
  const relativePath = String((firstFile as any).webkitRelativePath || "");
  if (relativePath) {
    return relativePath.split("/")[0] || "";
  }
  return firstFile.name || "";
}

/**
 * @brief Start an index job from the modal form.
 * @param repo Repository name selected in the UI.
 */
async function startIndexFromDialog(repo: string): Promise<void> {
  const pathInput = document.getElementById("indexPathInput") as HTMLInputElement | null;
  const folderInput = document.getElementById("indexFolderInput") as HTMLInputElement | null;
  const startBtn = document.getElementById("indexStartBtn") as HTMLButtonElement | null;
  const terminal = document.getElementById("indexTerminal") as HTMLElement | null;
  const repoPath = String(pathInput?.value || "").trim();
  const selectedFolderName = folderInput ? getSelectedDirectoryName(folderInput) : "";

  if (selectedFolderName && selectedFolderName !== repo) {
    if (terminal) terminal.textContent = 'Selected folder "' + selectedFolderName + '" does not match repository "' + repo + '".';
    return;
  }
  const pathFolderName = basenameFromPath(repoPath);
  if (pathFolderName && pathFolderName !== repo) {
    if (terminal) terminal.textContent = 'Repository path folder "' + pathFolderName + '" does not match repository "' + repo + '".';
    return;
  }
  if (startBtn) startBtn.disabled = true;
  if (terminal) terminal.textContent = "Starting index job...";

  try {
    const response = await fetch("/ui/api/repos/" + encodeURIComponent(repo) + "/index-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_path: repoPath, workers: 2 }),
    });
    const data: any = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || ("HTTP " + response.status));
    }
    const root = document.getElementById("indexDialogRoot") as HTMLElement | null;
    if (root) root.dataset.jobId = data.job.id;
    pollIndexJob(data.job.id);
  } catch (error: any) {
    if (startBtn) startBtn.disabled = false;
    if (terminal) terminal.textContent = "Start failed: " + (error?.message || String(error));
  }
}

/**
 * @brief Cancel the currently displayed index job.
 */
async function cancelDisplayedIndexJob(): Promise<void> {
  const jobId = String(document.getElementById("indexDialogRoot")?.dataset.jobId || "");
  if (!jobId) {
    closeIndexDialog();
    return;
  }
  try {
    await fetch("/ui/api/index-jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
  } finally {
    await refreshIndexJob(jobId);
  }
}

/**
 * @brief Open the centered index dialog for a repository.
 * @param repo Repository name selected in the UI.
 */
function showIndexDialog(repo: string): void {
  closeIndexDialog();
  document.body.insertAdjacentHTML("beforeend", [
    '<div id="indexDialogRoot" class="index-dialog-root">',
    '  <div class="index-dialog-backdrop"></div>',
    '  <section class="index-dialog" role="dialog" aria-modal="true" aria-labelledby="indexDialogTitle">',
    '    <div class="index-dialog-header">',
    '      <h2 id="indexDialogTitle">Index ' + esc(repo) + "</h2>",
    '      <button id="indexCloseBtn" class="btn-secondary" type="button">Close</button>',
    "    </div>",
    '    <div class="index-dialog-body">',
    '      <div class="index-path-controls">',
    '        <input id="indexPathInput" type="text" placeholder="/absolute/path/to/' + esc(repo) + '" autocomplete="off">',
    '        <button id="indexChooseBtn" class="btn-secondary" type="button">Choose Folder</button>',
    '        <input id="indexFolderInput" type="file" webkitdirectory directory multiple hidden>',
    "      </div>",
    '      <p id="indexDialogStatus" class="size-note">Ready.</p>',
    '      <pre id="indexTerminal" class="index-terminal">Waiting for repository path...</pre>',
    "    </div>",
    '    <div class="index-dialog-footer">',
    '      <button id="indexCancelBtn" class="btn-secondary" type="button">Cancel</button>',
    '      <button id="indexStartBtn" type="button">Index</button>',
    "    </div>",
    "  </section>",
    "</div>",
  ].join(""));

  const chooseBtn = document.getElementById("indexChooseBtn") as HTMLButtonElement;
  const folderInput = document.getElementById("indexFolderInput") as HTMLInputElement;
  const closeBtn = document.getElementById("indexCloseBtn") as HTMLButtonElement;
  const startBtn = document.getElementById("indexStartBtn") as HTMLButtonElement;
  const cancelBtn = document.getElementById("indexCancelBtn") as HTMLButtonElement;
  const status = document.getElementById("indexDialogStatus") as HTMLElement;

  chooseBtn.addEventListener("click", () => folderInput.click());
  folderInput.addEventListener("change", () => {
    const selectedFolderName = getSelectedDirectoryName(folderInput);
    status.textContent = selectedFolderName
      ? "Selected folder: " + selectedFolderName
      : "Folder selected.";
  });
  closeBtn.addEventListener("click", closeIndexDialog);
  startBtn.addEventListener("click", () => { void startIndexFromDialog(repo); });
  cancelBtn.addEventListener("click", () => { void cancelDisplayedIndexJob(); });
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
 * @brief Render the Index Management panel with destructive operations only.
 * @param repo Repository name.
 */
function renderIndexMgmt(repo: string): void {
  indexMgmtBody.innerHTML = [
    '<div class="index-actions">',
    '<button id="indexRepoBtn" type="button">Index</button>',
    '<button class="btn-danger" id="deleteIndexBtn" type="button">Delete Index</button>',
    "</div>",
  ].join("");

  const indexBtn = document.getElementById("indexRepoBtn") as HTMLButtonElement;
  const deleteBtn = document.getElementById("deleteIndexBtn") as HTMLButtonElement;
  indexBtn.addEventListener("click", () => {
    if (!repo) return;
    showIndexDialog(repo);
  });
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

/** @brief Format any cell value into a compact HTML-safe string. */
function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '<span class="cell-null">null</span>';
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return esc("[" + value.map((v) => String(v)).join(", ") + "]");
  if (typeof value === "object") return esc(JSON.stringify(value));
  const str = String(value);
  if (str.length > 240) return esc(str.slice(0, 240)) + '<span class="cell-trunc">...</span>';
  return esc(str);
}

/**
 * @brief Return persisted filters for a table key.
 * @param table Table key.
 * @returns Mutable key->value filter map for that table.
 */
function getTableFilters(table: string): Record<string, string> {
  return tableFiltersByTable.get(table) || {};
}

/**
 * @brief Persist active table filters from server-normalized payload.
 * @param table Table key.
 * @param activeFilters Active filters keyed by column.
 */
function setTableFiltersFromActive(table: string, activeFilters?: Record<string, string[]>): void {
  const next: Record<string, string> = {};
  for (const [key, values] of Object.entries(activeFilters || {})) {
    if (!values || values.length === 0) continue;
    next[key] = String(values[0]);
  }
  tableFiltersByTable.set(table, next);
}

/**
 * @brief Build URL query suffix for a table's current filters.
 * @param table Table key.
 * @returns Serialized query-string segment, including leading `&` when needed.
 */
function buildTableFilterQuery(table: string): string {
  const filters = getTableFilters(table);
  const parts: string[] = [];
  for (const key of Object.keys(filters).sort()) {
    const value = String(filters[key] || "").trim();
    if (!value) continue;
    parts.push("filter_" + encodeURIComponent(key) + "=" + encodeURIComponent(value));
  }
  if (!parts.length) return "";
  return "&" + parts.join("&");
}

/** @brief Render the categorical-filter controls for a table page. */
function renderTableFiltersHtml(page: BrowseTablePage): string {
  const options = page.filter_options || [];
  if (!options.length) return "";
  const active = getTableFilters(page.name);
  const hasActiveFilters = Object.keys(active).some((key) => String(active[key] || "").trim().length > 0);
  return [
    '<div class="table-filters">',
    ...options.map((option) => (
      '<label class="table-filter" for="filter-' + esc(option.key) + '">'
      + '<span>' + esc(option.label) + "</span>"
      + '<select id="filter-' + esc(option.key) + '" data-filter-key="' + esc(option.key) + '">'
      + '<option value="">All</option>'
      + option.values.map((value) => (
        '<option value="' + esc(value) + '"' + (active[option.key] === value ? " selected" : "") + ">"
        + esc(value)
        + "</option>"
      )).join("")
      + "</select>"
      + "</label>"
    )),
    '<button type="button" class="table-filter-clear" data-action="clear-filters"' + (hasActiveFilters ? "" : " disabled") + '>Clear</button>',
    "</div>",
  ].join("");
}

/** @brief Build the HTML for a single table page (header bar + table + pager). */
function renderTablePageHtml(page: BrowseTablePage): string {
  const start = page.total === 0 ? 0 : page.offset + 1;
  const end = Math.min(page.total, page.offset + page.rows.length);
  const headerCells = page.columns.map((c) => '<th>' + esc(c.label) + "</th>").join("");
  const bodyRows = page.rows.length
    ? page.rows.map((row) => (
      "<tr>" + page.columns.map((c) => "<td>" + formatCell(row[c.key]) + "</td>").join("") + "</tr>"
    )).join("")
    : '<tr><td class="cell-empty" colspan="' + page.columns.length + '">No rows.</td></tr>';
  const prevDisabled = page.offset <= 0 ? " disabled" : "";
  const nextDisabled = end >= page.total ? " disabled" : "";
  return [
    renderTableFiltersHtml(page),
    '<div class="modal-pager">',
    '  <span class="modal-pager-info">',
    esc(page.label) + " &middot; " + start.toLocaleString() + "-" + end.toLocaleString()
    + " of " + Number(page.total).toLocaleString(),
    "  </span>",
    '  <span class="modal-pager-buttons">',
    '    <button type="button" data-page="prev"' + prevDisabled + ">Prev</button>",
    '    <button type="button" data-page="next"' + nextDisabled + ">Next</button>",
    "  </span>",
    "</div>",
    '<div class="modal-table-wrap">',
    '  <table class="modal-table"><thead><tr>' + headerCells + "</tr></thead>",
    "  <tbody>" + bodyRows + "</tbody></table>",
    "</div>",
  ].join("");
}

/**
 * @brief Fetch one page of a table and render it in the inline browser pane.
 * @param repo Repository name.
 * @param table Table key.
 * @param offset Page offset.
 */
async function loadIndexTablePage(repo: string, table: string, offset: number): Promise<void> {
  if (!table) return;
  activeIndexTable = table;
  indexBrowserPane.innerHTML = '<p class="warn">Loading ' + esc(table) + "...</p>";
  const limit = 100;
  try {
    const filterQuery = buildTableFilterQuery(table);
    const url = "/ui/api/repos/" + encodeURIComponent(repo)
      + "/tables/" + encodeURIComponent(table)
      + "?limit=" + limit + "&offset=" + Math.max(0, offset) + filterQuery;
    const page = await getJson<BrowseTablePage>(url);
    if (repo !== activeRepo) return;
    if (table !== activeIndexTable) return;
    setTableFiltersFromActive(table, page.active_filters || {});
    indexBrowserPane.innerHTML = renderTablePageHtml(page);
    wireTablePageControls(repo, page);
  } catch (e: any) {
    indexBrowserPane.innerHTML = '<p class="warn">Failed to load ' + esc(table) + ": " + esc(e?.message || String(e)) + "</p>";
  }
}

/**
 * @brief Wire up Prev/Next buttons to fetch adjacent pages of the same table.
 * @param repo Repository name.
 * @param page Current table page.
 */
function wireTablePageControls(repo: string, page: BrowseTablePage): void {
  const prev = indexBrowserPane.querySelector<HTMLButtonElement>('button[data-page="prev"]');
  const next = indexBrowserPane.querySelector<HTMLButtonElement>('button[data-page="next"]');
  const clearFilters = indexBrowserPane.querySelector<HTMLButtonElement>('button[data-action="clear-filters"]');
  const filterSelects = indexBrowserPane.querySelectorAll<HTMLSelectElement>("select[data-filter-key]");

  if (prev) prev.addEventListener("click", () => {
    void loadIndexTablePage(repo, page.name, Math.max(0, page.offset - page.limit));
  });
  if (next) next.addEventListener("click", () => {
    void loadIndexTablePage(repo, page.name, page.offset + page.limit);
  });
  filterSelects.forEach((select) => {
    select.addEventListener("change", () => {
      const key = String(select.dataset.filterKey || "").trim();
      if (!key) return;
      const value = String(select.value || "").trim();
      const filters = { ...getTableFilters(page.name) };
      if (value) {
        filters[key] = value;
      } else {
        delete filters[key];
      }
      tableFiltersByTable.set(page.name, filters);
      void loadIndexTablePage(repo, page.name, 0);
    });
  });
  if (clearFilters) clearFilters.addEventListener("click", () => {
    tableFiltersByTable.set(page.name, {});
    void loadIndexTablePage(repo, page.name, 0);
  });
}

/**
 * @brief Load table metadata and initialize the inline raw index browser tabs.
 * @param repo Repository name.
 */
async function loadIndexBrowser(repo: string): Promise<void> {
  indexBrowserTitle.textContent = "Raw Index - " + repo;
  indexBrowserTabs.innerHTML = "";
  indexBrowserPane.innerHTML = '<p class="warn">Loading tables...</p>';
  try {
    const data = await getJson<{ tables: BrowseTableInfo[] }>(
      "/ui/api/repos/" + encodeURIComponent(repo) + "/tables",
    );
    if (repo !== activeRepo) return;
    const tables = data.tables || [];
    if (!tables.length) {
      indexBrowserPane.innerHTML = '<p class="warn">No browseable tables for this repository.</p>';
      return;
    }
    indexBrowserTabs.innerHTML = tables
      .map((t) => (
        '<button class="modal-tab" type="button" data-table="' + esc(t.name) + '" title="' + esc(t.description) + '">'
        + esc(t.label) + ' <span class="modal-tab-count">' + Number(t.row_count).toLocaleString() + "</span>"
        + "</button>"
      ))
      .join("");
    indexBrowserTabs.querySelectorAll<HTMLButtonElement>("button.modal-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        indexBrowserTabs.querySelectorAll(".modal-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        void loadIndexTablePage(repo, btn.dataset.table || "", 0);
      });
    });
    const first = indexBrowserTabs.querySelector<HTMLButtonElement>("button.modal-tab");
    if (first) {
      first.classList.add("active");
      await loadIndexTablePage(repo, first.dataset.table || "", 0);
    }
  } catch (e: any) {
    indexBrowserPane.innerHTML = '<p class="warn">Failed to load tables: ' + esc(e?.message || String(e)) + "</p>";
  }
}

/**
 * @brief Load all data for a repo: stats, management controls, and raw table browser.
 * @param repo Repository name.
 * @param urlMode Whether to sync browser history with the selection.
 */
async function loadRepo(repo: string, urlMode: "none" | "push" | "replace" = "none"): Promise<void> {
  activeRepo = repo;
  activeIndexTable = "";
  tableFiltersByTable.clear();
  if (urlMode !== "none") {
    syncUrlForRepo(repo, urlMode);
  }
  updateStatus("Loading repository data for " + repo + "...");
  indexBrowserTitle.textContent = "Raw Index - " + repo;
  indexBrowserTabs.innerHTML = "";
  indexBrowserPane.innerHTML = '<p class="warn">Loading repository tables...</p>';
  renderIndexMgmt(repo);

  const statsPromise = getJson<RepositoryStats>("/ui/api/repos/" + encodeURIComponent(repo) + "/stats");

  const stats = await statsPromise;
  if (repo !== activeRepo) return;
  renderStats(stats);
  await loadIndexBrowser(repo);
  updateStatus("Showing " + repo + ".");
}

/** @brief Reload the repo dropdown after deletion. */
async function reloadRepoList(): Promise<void> {
  try {
    const reposData = await getJson<RepoListResponse>("/ui/api/repos");
    const repos = reposData.repositories || [];
    setRepoOptions(repos);
    if (repos.length) {
      const requestedRepo = getRequestedRepoFromPath(window.location.pathname);
      const fallbackRepo = activeRepo || repos[0].repo;
      const targetRepo = repos.some((r) => r.repo === requestedRepo)
        ? requestedRepo
        : (repos.some((r) => r.repo === fallbackRepo) ? fallbackRepo : repos[0].repo);
      repoSelect.value = targetRepo;
      await loadRepo(targetRepo, "replace");
    } else {
      activeRepo = "";
      syncUrlForRepo("", "replace");
      statsBody.innerHTML = '<p class="warn">No indexed repositories.</p>';
      indexMgmtBody.innerHTML = '<p class="warn">No indexed repositories.</p>';
      indexBrowserTitle.textContent = "Raw Index";
      indexBrowserTabs.innerHTML = "";
      indexBrowserPane.innerHTML = '<p class="warn">No indexed repositories.</p>';
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
      setRepoOptions([]);
      syncUrlForRepo("", "replace");
      indexBrowserTitle.textContent = "Raw Index";
      indexBrowserTabs.innerHTML = "";
      indexBrowserPane.innerHTML = '<p class="warn">No indexed repositories.</p>';
      return;
    }
    setRepoOptions(repos);
    const requestedRepo = getRequestedRepoFromPath(window.location.pathname);
    const selectedRepo = repos.some((r) => r.repo === requestedRepo)
      ? requestedRepo
      : repos[0].repo;
    repoSelect.value = selectedRepo;
    await loadRepo(selectedRepo, "replace");
  } catch (error: any) {
    updateStatus("Failed to load repositories: " + (error?.message || String(error)));
  }
}

window.addEventListener("beforeunload", () => {
  if (toolCallPollId !== null) window.clearInterval(toolCallPollId);
  clearIndexJobPolling();
});

repoSelect.addEventListener("change", async () => {
  if (!repoSelect.value) return;
  try {
    await loadRepo(repoSelect.value, "push");
  } catch (error: any) {
    updateStatus("Failed to load repo data: " + (error?.message || String(error)));
  }
});

window.addEventListener("popstate", () => {
  if (!repoSelect.options.length) return;
  const requestedRepo = getRequestedRepoFromPath(window.location.pathname);
  const selectedRepo = requestedRepo && Array.from(repoSelect.options).some((option) => option.value === requestedRepo)
    ? requestedRepo
    : repoSelect.value;
  if (!selectedRepo || selectedRepo === activeRepo) return;
  repoSelect.value = selectedRepo;
  void loadRepo(selectedRepo);
});

void boot();
