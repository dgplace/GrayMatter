# Spike: Incremental update strategy for resolved edges (CODEBRAIN-13)

This spike answers `CODEBRAIN-13` with repository-specific analysis plus primary-source web research.

## Scope

- Item: `CODEBRAIN-13`
- Question: in watch mode, can CodeBrain update only the changed file's outgoing edges and the small incoming-edge set that resolves to changed symbols?
- Constraint: optimize for repositories under `1,000,000` LOC.

## Current baseline in CodeBrain

From the current ingestion path:

- Watch mode already reprocesses only changed files (`ingest.py` -> `ReindexHandler._handle_change` -> `process_file`).
- For an updated file, CodeBrain rewrites:
  - `code_chunks` for that file
  - `symbols` for that file
  - `dependencies` where `source_file_id = changed_file`
  - `symbol_references` where `source_file_id = changed_file`
- Incoming lexical references from other files are not recomputed, because `symbol_references` currently stores `target_name` but not resolved `target_symbol_id`.

Repository calibration snapshot (indexed `CodeBrain` repo):

- `11,303` LOC, `376` symbols, `8,707` symbol references, `191` dependencies
- Density: `33.27` symbols / KLOC, `770.33` refs / KLOC
- Linear projection at `1,000,000` LOC: ~`33k` symbols, ~`770k` refs

Implication: under 1M LOC, selective incoming-edge refresh is tractable if we keep candidate-set queries indexed and avoid whole-repo rescans.

## Web evidence (primary sources)

1. Tree-sitter supports true incremental parsing: edit old tree, parse with old tree, inspect changed ranges (`tree.edit`, `Parser.parse(new_src, old_tree)`, `Tree.changed_ranges`) [py-tree-sitter README](https://github.com/tree-sitter/py-tree-sitter).
2. Incremental systems should track dependency graphs and cut off recomputation when results are unchanged (Salsa early cutoff) [rust-analyzer durable incrementality](https://rust-analyzer.github.io/blog/2023/07/24/durable-incrementality.html).
3. clangd separates dynamic (actively edited) and background indexes, and models references as edges keyed by stable symbol IDs (`Ref` looked up by `SymbolID`) [clangd index design](https://clangd.llvm.org/design/indexing).
4. TypeScript persists project-graph state (`.tsbuildinfo`) and uses smart incremental build orchestration (`tsc --build`) [TS `incremental`](https://www.typescriptlang.org/tsconfig/incremental.html), [Project References](https://www.typescriptlang.org/docs/handbook/project-references).
5. Bazel documents two key lessons:
   - dependency tracking enables precise reverse-closure invalidation
   - change pruning/early resurrection matters when recomputed values are unchanged
   - all-or-nothing invalidation can erase gains if not decomposed
   [Skyframe](https://bazel.build/reference/skyframe).
6. Static-analysis literature reports update time proportional to change size is achievable, but only with careful dependency tracking and invalidation discipline [PLDI 2021](https://doi.org/10.1145/3453483.3454026), [FSE 2023 / CodeQL](https://arxiv.org/abs/2308.09660).

## Edge dependency model for CodeBrain

### File-local (safe to recompute only from changed file)

- `code_chunks`
- symbols declared in that file
- outgoing lexical references extracted from that file
- outgoing dependency edges extracted from that file

### Cross-file (must be selectively invalidated/re-resolved)

- resolved target binding for outgoing references (`target_symbol_id`, future schema)
- incoming references from other files that currently point to symbols changed/removed in edited file
- transitive symbol-resolution confidence affected by changed declarations

## Required schema/index additions

Recommended to align with planned `CODEBRAIN-15` columns:

- Add to `symbol_references`:
  - `target_symbol_id INTEGER NULL REFERENCES symbols(id) ON DELETE SET NULL`
  - `resolution_confidence REAL NULL`
  - `resolution_method TEXT NULL` (e.g. `scip`, `heuristic_name`, `unresolved`)

Indexes required for incremental incoming refresh:

```sql
CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_symbol
ON symbol_references(target_symbol_id)
WHERE target_symbol_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_symbol_refs_target_name_kind
ON symbol_references(target_name, reference_kind);

CREATE INDEX IF NOT EXISTS idx_symbols_file_primary_name
ON symbols(file_id, is_primary_declaration, name);
```

These keep three hot paths cheap: incoming-by-symbol lookup, fallback candidate lookup by name/kind, and changed-symbol enumeration per file.

## Recommended incremental algorithm (<1M LOC default)

1. Detect changed file hash (existing behavior).
2. Recompute file-local artifacts for that file (existing behavior).
3. Resolve changed file's outgoing refs to `target_symbol_id` + confidence (new resolver stage).
4. Build changed-symbol delta:
   - old symbol IDs in file (before rewrite)
   - new symbol IDs in file (after rewrite)
   - compute removed/changed declaration keys.
5. Invalidate inbound resolved edges only where `target_symbol_id` is in removed/changed symbol set.
6. Re-resolve only affected source files:
   - fetch distinct `source_file_id` from invalidated refs
   - rerun resolver for those files' existing lexical refs (do not re-chunk unless file also changed)
7. Apply change pruning:
   - if newly resolved `(source_ref_id -> target_symbol_id, confidence)` equals previous value, keep existing row metadata and skip cascading work.
8. Emit metrics per event:
   - changed file parse/chunk time
   - invalidated incoming ref count
   - re-resolved incoming ref count
   - fallback/unresolved count

## Guardrails for <1M LOC repos

Default strategy:

- Always do selective incoming refresh first.
- Fall back to repo-wide re-resolution only when either trigger trips:
  - invalidated incoming refs > `50,000`, or
  - affected source files > `10%` of repo files.

Why these guardrails:

- At projected ~`770k` refs for 1M LOC, these limits cap a single watch event to a bounded fraction of graph churn.
- They preserve interactive watch responsiveness while keeping correctness exact.

## Phased rollout path

1. **Phase A (schema + read compatibility)**  
   Add `symbol_references.target_symbol_id` and confidence/method columns + indexes.
2. **Phase B (write path for changed file only)**  
   Populate resolved targets for outgoing refs in `process_file`.
3. **Phase C (selective incoming refresh)**  
   Invalidate/re-resolve inbound refs for changed symbols only.
4. **Phase D (incremental parser cache, optional)**  
   Keep per-file tree cache and use `tree.edit` + `parse(old_tree)` where watcher can provide edit ranges.
5. **Phase E (observability + auto fallback)**  
   Promote guardrails to config and expose counters in ingestion summary/UI.

## Concrete recommendation to unblock

Proceed with `CODEBRAIN-15` + `CODEBRAIN-16` implementation using selective incoming refresh as the default algorithm for repos under 1M LOC, with the guardrails above and whole-repo re-resolution fallback when thresholds trip.
