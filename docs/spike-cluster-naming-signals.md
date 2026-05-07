# Spike: Cluster naming quality evaluation (CODEBRAIN-12)

This spike evaluates whether logical-module naming should use only baseline file summaries or add an `embedding + co-change` signal pack.

## Scope

- Item: `CODEBRAIN-12`
- Question: is first-file context enough, or does `embedding + co-change` improve naming quality?
- Repo benchmarked: `CodeBrain`
- Community detection mode: existing logical-module synthesis defaults (class-level graph with fallback)

## Method

1. Added a reproducible benchmark harness:
   - `scripts/benchmark_cluster_naming_signals.py`
2. Extracted eligible logical communities from the indexed graph (same Louvain/splitting path used by synthesis).
3. Created human-labeled ground truth for 4 known community domains in this repo.
4. Ran two prompt variants (2 trials each per community):
   - `baseline`: first file paths + summaries
   - `embedding-cochange`: baseline + embedding centroid anchors + git co-change summaries
5. Scored output against ground-truth keywords using precision/recall/F1 and exact-hit proxy.

Command used:

```bash
./.venv/bin/python3 scripts/benchmark_cluster_naming_signals.py \
  --repo CodeBrain \
  --trials 2 \
  --output-json .bench/codebrain12_cluster_naming.json
```

## Results

- Communities scored: `4`
- Samples per variant: `8`

Baseline:
- Avg precision: `0.0766`
- Avg recall: `0.3500`
- Avg F1: `0.1253`
- Avg exact-hit: `0.0000`

Embedding + co-change:
- Avg precision: `0.1064`
- Avg recall: `0.5000`
- Avg F1: `0.1746`
- Avg exact-hit: `0.0000`

Delta (`embedding-cochange - baseline`):
- F1: `+0.0493`
- Exact-hit: `+0.0000`

## Interpretation

- `embedding + co-change` produced a clear positive gain on keyword-based naming quality for this repository.
- The gain came mostly from higher recall (more domain-relevant terms present in generated names/summaries/intents).
- Exact full-keyword coverage did not change in this small sample, but average semantic overlap improved.

## Recommendation

Proceed with `embedding-cochange` as the default logical-module naming context, while retaining `baseline` as a fallback variant for A/B checks and future tuning.

## Prototype / implementation references

- Synthesis update: `synthesize_modules.py` now supports:
  - `--context-variant baseline|embedding-cochange`
  - default `embedding-cochange` for logical-module naming
- Benchmark harness: `scripts/benchmark_cluster_naming_signals.py`
- Raw benchmark output can be regenerated with `--output-json <path>` when needed.
