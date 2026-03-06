# Plan: Module-Level Intent Synthesis

## Context
The database has chunk-level `intent_detail` and file-level `summary`/`role`, but no concept of a subsystem or logical module. This adds a `module_intents` table and a standalone `synthesize_modules.py` script (manual post-ingestion step) that discovers modules two ways:
- **directory** — any folder containing ≥ 3 source files (based on `files.path`)
- **logical** — communities detected from the file-to-file dependency/reference graph (networkx community detection)

Both kinds are stored in the same table (distinguished by `kind` column). A new MCP tool `get_module_map` surfaces the data.

---

## Files to Modify / Create

| File | Change |
|---|---|
| `schema.sql` | Add `module_intents` DDL |
| `ingest.py` | Add `module_intents` CREATE to `SCHEMA_PATCHES` |
| `src/db.ts` | Add `module_intents` CREATE to `SCHEMA_PATCHES` |
| `synthesize_modules.py` | **New file** — CLI script (both modes) |
| `src/repositories/store.ts` | Add `getModuleIntents()` |
| `src/mcp/tools.ts` | Add `get_module_map` tool |

---

## 1. Schema — `module_intents` table

```sql
CREATE TABLE IF NOT EXISTS module_intents (
  repo            TEXT NOT NULL,
  module_path     TEXT NOT NULL,   -- dir path OR "_logical/<slug>"
  kind            TEXT NOT NULL DEFAULT 'directory',  -- 'directory' | 'logical'
  module_name     TEXT,            -- human-readable name (LLM-generated for logical)
  summary         TEXT,
  role            TEXT,
  dominant_intent TEXT,
  file_count      INTEGER NOT NULL DEFAULT 0,
  chunk_count     INTEGER NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (repo, module_path)
);
CREATE INDEX IF NOT EXISTS idx_module_intents_repo ON module_intents(repo);
CREATE INDEX IF NOT EXISTS idx_module_intents_kind ON module_intents(repo, kind);
```

Add this to:
- `schema.sql` (end of file)
- `SCHEMA_PATCHES` list in `ingest.py`
- `SCHEMA_PATCHES` array in `src/db.ts`

---

## 2. `synthesize_modules.py` — New Standalone Script

### CLI
```
python synthesize_modules.py --repo <name> [--mode directory|logical|all] [--min-files 3] [--config codebrain.toml]
```
Default `--mode all` (runs both passes).

### Shared setup
- `load_config()` — import directly from `ingest.py`
- `IntentClassifier` — import from `classifier.py`
- DB connection via `psycopg2.connect(config["database"]["url"])`

---

### Pass 1: Directory modules

1. Discover directories with ≥ N files:
   ```sql
   SELECT regexp_replace(path, '/[^/]+$', '') AS dir, COUNT(*) AS file_count
   FROM files WHERE repo = %s
   GROUP BY dir HAVING COUNT(*) >= %s
   ORDER BY dir
   ```
2. For each directory, fetch file summaries + dominant intent + chunk counts:
   ```sql
   SELECT f.path, f.summary, f.role,
     (SELECT mode() WITHIN GROUP (ORDER BY cc.intent)
      FROM code_chunks cc WHERE cc.file_id = f.id) AS dom_intent,
     (SELECT COUNT(*) FROM code_chunks cc WHERE cc.file_id = f.id) AS chunk_count
   FROM files f
   WHERE f.repo = %s AND regexp_replace(f.path, '/[^/]+$', '') = %s
   ```
3. Build `DIRECTORY_MODULE_PROMPT`:
   ```
   Analyze this source directory and synthesize its architectural role.

   Module path: {module_path}
   Files ({file_count}):
   {file_list_with_summaries}

   Intent distribution: {intent_counts}

   Respond with ONLY:
   {"summary": "<2-3 sentences>", "role": "<architectural role>", "dominant_intent": "<category>"}
   ```
4. Call `classifier._generate(prompt, max_tokens=200)` + `classifier._parse_json()`
5. Upsert with `kind='directory'`, `module_name=<last path component>`

---

### Pass 2: Logical modules (dependency community detection)

**Requires:** `pip install networkx`

1. Load all file-to-file edges for the repo:
   ```sql
   SELECT DISTINCT sf.path AS source, tf.path AS target
   FROM dependencies d
   JOIN files sf ON sf.id = d.source_file_id AND sf.repo = %s
   JOIN files tf ON tf.id = d.target_file_id AND tf.repo = %s
   WHERE sf.path <> tf.path
   UNION
   SELECT DISTINCT sf.path AS source, tf.path AS target
   FROM symbol_references sr
   JOIN files sf ON sf.id = sr.source_file_id AND sf.repo = %s
   JOIN symbols s ON lower(s.name) = lower(sr.target_name)
   JOIN files tf ON tf.id = s.file_id AND tf.repo = %s
   WHERE sf.path <> tf.path
   ```
2. Build undirected `networkx.Graph` from edges
3. Run `nx.community.greedy_modularity_communities(G)` (no external deps beyond networkx)
4. Filter: keep communities with ≥ `--min-files` nodes
5. Skip communities where all files share the same directory (already covered by directory pass)
6. For each community, fetch file summaries from DB and build `LOGICAL_MODULE_PROMPT`:
   ```
   Identify the functional purpose of this group of source files that form a cohesive logical module.

   Repository: {repo}
   Files ({count}):
   {file_list_with_summaries}

   These files are grouped because they have strong call/reference relationships.

   Respond with ONLY:
   {"name": "<short-hyphenated-slug>", "summary": "<2-3 sentences on the shared purpose>",
    "role": "<architectural role>", "dominant_intent": "<category>"}
   ```
7. Upsert with:
   - `module_path = "_logical/" + name_slug`
   - `kind = 'logical'`
   - `module_name` = LLM-returned name

---

## 3. `src/repositories/store.ts` — `getModuleIntents()`

```typescript
export type ModuleIntent = {
  module_path: string;
  kind: string;
  module_name: string | null;
  summary: string | null;
  role: string | null;
  dominant_intent: string | null;
  file_count: number;
  chunk_count: number;
  updated_at: string;
};

export async function getModuleIntents(
  repo: string,
  pathPrefix = "",
  kind?: string,
): Promise<ModuleIntent[]> {
  const result = await query(
    `SELECT module_path, kind, module_name, summary, role, dominant_intent, file_count, chunk_count, updated_at
     FROM module_intents
     WHERE repo = $1
       AND ($2 = '' OR module_path LIKE $2 || '%')
       AND ($3::text IS NULL OR kind = $3)
     ORDER BY kind, module_path`,
    [repo, pathPrefix, kind ?? null],
  );
  return result.rows as ModuleIntent[];
}
```

---

## 4. `src/mcp/tools.ts` — `get_module_map` tool

Add after `get_file_map`, import `getModuleIntents` from `store.ts`:

```typescript
server.tool(
  "get_module_map",
  "Returns synthesized module-level intents for a repository — both folder-based and logical (cross-folder) modules. Useful to understand subsystem purposes before diving into files. Run synthesize_modules.py first. Repository scope is required.",
  {
    repo: z.string().min(1).describe("Repository name. Required."),
    path_prefix: z.string().optional().default("").describe("Filter to a path prefix (directory modules only)."),
    kind: z.enum(["directory", "logical", "all"]).optional().default("all").describe("Filter by module kind."),
  },
  async ({ repo, path_prefix, kind }) => {
    logToolInvocation("get_module_map", { repo, path_prefix, kind });
    const repoCheck = await requireRepository(repo);
    if (repoCheck) return repoCheck;

    const kindFilter = kind === "all" ? undefined : kind;
    const modules = await getModuleIntents(repo, path_prefix, kindFilter);

    if (modules.length === 0) {
      return { content: [{ type: "text", text: `No module intents found for \`${repo}\`. Run synthesize_modules.py first.` }] };
    }

    const dirModules = modules.filter(m => m.kind === "directory");
    const logicalModules = modules.filter(m => m.kind === "logical");

    const formatModule = (m: ModuleIntent) =>
      `**${m.module_name || m.module_path}** (${m.file_count} files | ${m.chunk_count} chunks)\n` +
      `  Role: ${m.role || "unknown"} | Intent: ${m.dominant_intent || "unknown"}\n` +
      (m.summary ? `  ${m.summary}` : "");

    const parts: string[] = [`# Module Map: ${repo}\n`];
    if (dirModules.length > 0) {
      parts.push(`## Directory Modules\n\n${dirModules.map(formatModule).join("\n\n")}`);
    }
    if (logicalModules.length > 0) {
      parts.push(`## Logical Modules (Cross-Folder)\n\n${logicalModules.map(formatModule).join("\n\n")}`);
    }

    return { content: [{ type: "text", text: parts.join("\n\n") }] };
  },
);
```

---

## Verification

1. **Schema**: Run `python ingest.py --repo <name>` or start the TS server — both apply `SCHEMA_PATCHES`, which creates `module_intents`
2. **Directory modules**: `python synthesize_modules.py --repo <name> --mode directory`
   - Check: `SELECT * FROM module_intents WHERE repo='<name>' AND kind='directory';`
3. **Logical modules**: `python synthesize_modules.py --repo <name> --mode logical`
   - Check: `SELECT * FROM module_intents WHERE repo='<name>' AND kind='logical';`
4. **MCP tool**: Call `get_module_map` with `repo` + `kind="all"` — should return formatted sections
5. **TypeScript**: `node node_modules/typescript/lib/tsc.js --noEmit` — clean
