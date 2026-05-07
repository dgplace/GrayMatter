# Spike: Symbol vs file granularity for clustering (CODEBRAIN-11)

This spike answers `CODEBRAIN-11` using literature and web evidence only (no local benchmark runs).

## Scope

- Item: `CODEBRAIN-11`
- Question: for repositories under ~1M LOC, should v1 default to symbol-level clustering?
- Method: literature review + scalability evidence from community detection papers

## Evidence summary

1. Software clustering literature explicitly treats granularity as a first-order design choice (file/class/function), not a fixed default for all systems.
   - Source: Sarhan et al., *Software Module Clustering: An In-Depth Literature Analysis* (IEEE TSE 2021 / arXiv 2020): https://arxiv.org/abs/2012.01057
2. The same review notes that clustering at higher abstraction levels is often preferred for comprehension, while very fine entity granularity can increase complexity/noise.
   - Source: paper text (ar5iv rendering): https://ar5iv.org/pdf/2012.01057
3. Architecture recovery work commonly uses class-level dependency graphs (finer than file-level), supporting symbol/class granularity for structural fidelity.
   - Source: Lundberg & Lowe, *Architecture Recovery by Semi-Automatic Component Identification* (2003): https://www.sciencedirect.com/science/article/pii/S1571066104807370
4. Leiden itself is practical at very large graph sizes, and modern implementations report strong scalability on million/billion-edge graphs.
   - Source: Traag et al., *From Louvain to Leiden* (Scientific Reports 2019): https://www.nature.com/articles/s41598-019-41695-z
   - Source: Sahu, *GVE-Leiden* (ICPP 2024 / arXiv): https://arxiv.org/abs/2312.13936

## Practical interpretation for CodeBrain

- LOC alone is a weak predictor; graph size/density drives cost.
- For this repository, indexed density is about `33.27 symbols / 1k LOC` (`376` symbols over `11,303` LOC), implying roughly `33k` symbols at `1M` LOC at similar extraction density.
- That projected node count is generally compatible with symbol-level Leiden on modern hardware, but edge growth and language mix can still make some repos expensive.

## Recommendation (v1 default)

- Yes: for repos under ~`1,000,000` LOC, default to **symbol-level clustering**.
- Add guardrails so LOC is not the only gate:
  - if symbol nodes exceed `250k`, or
  - if symbol-reference/dependency edges exceed `5M`,
  - then auto-fallback to file-level (or two-stage clustering: symbol->file/community rollup).
- Keep granularity configurable (`symbol`, `file`, `auto`) and record the chosen mode in cluster metadata.

## Rationale for this recommendation

- Symbol/class-level better matches architectural boundaries than file-only clustering in many multi-concern files.
- Sub-1M LOC repos are usually below the scale where symbol-level is infeasible.
- Guardrails prevent pathological cases where LOC underestimates graph complexity.

## Unblock decision

Proceed with KG Phase 6 using `auto` mode with symbol-level default under 1M LOC plus node/edge guardrails.
