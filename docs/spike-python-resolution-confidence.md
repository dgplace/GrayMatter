# Spike: Type resolution confidence for dynamic Python (CODEBRAIN-9)

This spike benchmarks `scip-python` on a real Python repository and proposes default confidence thresholds for future `find_references` and `impact_of` behavior.

## Scope

- Item: `CODEBRAIN-9`
- Repository benchmarked: `pallets/flask`
- Commit benchmarked: `7374c85ddefc3f4b177a698ab9f0cbb6a5c0b392` (2026-05-02)
- Indexer: `@sourcegraph/scip-python` `0.6.6`

## Method

1. Generate SCIP:
   - `npx -y @sourcegraph/scip-python index --cwd <flask-repo> --project-name flask --output <flask-repo>/index.scip`
2. Expand to JSON:
   - `docker compose --profile indexer run --rm indexer scip print --json /workspace/.bench/flask/index.scip > .bench/flask/index.print.json`
3. Compute metrics using the prototype script in this branch:
   - `python3 scripts/benchmark_scip_python_confidence.py --index-json .bench/flask/index.print.json --project-prefix 'scip-python python flask ' --source-root .bench/flask`

## Results

From `benchmark_scip_python_confidence.py`:

- Documents indexed: `83`
- Total occurrences: `20,292`
- Definitions: `5,025`
- References: `15,267`

Reference categories:
- Local symbols: `5,841`
- Internal project symbols: `6,214`
- Python stdlib symbols: `3,212`
- External package symbols: `0`

Resolution and quality signals:
- Internal reference resolution rate: `90.20%` (`5,605 / 6,214`)
- Internal references unresolved in-index: `609` (`9.80%`)
- References with explicit `[unable to resolve ...]` marker: `2,313` (`15.15%` of all refs)
- Name-based fallback ambiguity risk (same simple name appears in multiple defs): `74.59%` of internal refs (`4,635 / 6,214`)
- Token/symbol alignment proxy on internal refs: `99.78%` match (`6,034` checked; `13` mismatches, mostly inheritance/meta patterns)

## Interpretation

- SCIP gives materially better disambiguation than name-only linking.
- The major risk for wrong impact chains is **fallback name matching**, not SCIP symbol linking.
- Dynamic/untyped paths are visible through unresolved markers and non-resolved internal edges; these should remain queryable, but clearly labeled lower-confidence.

## Confidence recommendation

Recommended `resolution_confidence` defaults when persisting resolved edges:

- `1.00`: symbol resolved to an internal project definition (`target_symbol_id` present and definition exists in index)
- `0.85`: resolved to stdlib or known external package symbol
- `0.60`: local-only references (usable as weak signal, not strong impact evidence)
- `0.35`: indexer marks unresolved (`[unable to resolve ...]`) or resolver cannot bind symbol

Using these bands on this benchmark:

- `>= 0.75` keeps `57.75%` of references (high-confidence set)
- `>= 0.55` keeps `80.86%` of references (includes weaker-but-useful local signals)

## MCP behavior recommendation

`find_references`:
- Default threshold: `0.55`
- Show grouped output:
  - High confidence (`>= 0.75`)
  - Lower confidence (`0.55-0.74`)
  - Hidden by default (`< 0.55`, opt-in with a flag)

`impact_of`:
- Default threshold: `0.75` for "likely impact"
- Optionally include `0.55-0.74` as "possible impact" with explicit confidence labels
- Exclude `< 0.55` from default traversal to reduce noisy blast-radius results

## Unblock recommendation

Proceed with KG Phase work blocked by confidence policy by implementing:

1. `resolution_confidence` persistence on resolved edges.
2. Threshold-aware filtering in `find_references` and `impact_of`.
3. Response formatting that surfaces confidence bands instead of a flat list.
