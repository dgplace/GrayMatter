---
name: codebrain-tools
description: Best practices and workflows for using CodeBrain MCP tools to navigate, search, and analyze codebases. Use this when performing repository discovery, tracking dependencies, understanding architecture, or planning refactoring.
---

# CodeBrain Tools Usage Guide

This skill provides recommended workflows and best practices for agents using CodeBrain MCP tools.

## Self-Discovery Policy

When working within a CodeBrain-indexed repository, use CodeBrain MCP tools as the primary discovery path.
- **Always start with** `list_repositories` to find the correct `repo` string. All other tools require this repository scope.
- **Use MCP tools first** for semantic, symbol, reference, and dependency analysis.
- **Use text search (`grep_search`)** as a precision and verification complement, or when index coverage might be stale.

## Recommended Workflows

### 1. Initial Discovery & Concept Search
When you need to find where a concept or feature is implemented but don't know exact names:
- Use `semantic_search` with a 2-8 word technical phrase. You can filter by `intent` (e.g., `business-logic`, `data-model`) or `language`.
- If you know a partial identifier, use `find_symbol` instead, as it is faster and more precise.

### 2. Exact Lookups & References
When you have a specific symbol name:
- Use `exact_symbol_search` for precision lookups to find definitions.
- Use `find_references` to see where a symbol is used. It resolves lexical and call references. *Tip: It defaults to high confidence matches. Pass `include_unresolved: true` if you suspect the heuristics missed something.*

### 3. Understanding Control Flow & Impact
When assessing the impact of a change or understanding how a component works:
- Use `find_call_graph` to traverse callers (reverse) or callees (forward) of a function or method.
- Use `find_impact` to find the blast radius of changing a specific file or symbol.
- Use `trace_dependencies` for a broader dependency walk (inbound or outbound) across file/module boundaries.

### 4. Type Hierarchies & Interfaces
When working with OOP or polymorphic code:
- Use `find_supertypes` to walk up the inheritance/implements chain.
- Use `find_subtypes` and `find_implementations` to see what inherits from or implements a class/interface.
- Use `find_instantiations` to see where a specific class is actually instantiated.

### 5. Architectural & Subsystem Analysis
When exploring larger boundaries:
- Use `get_file_map` to get a fast architectural map of a directory, including file roles, summaries, and exported symbols.
- Use `get_intent` to read a summary of a single file and the intentions behind its distinct code blocks.
- Use `codebase_stats` to understand repository size, languages, and intent distributions.
- Use `get_module_map` to visualize higher-level boundaries.

### 6. Refactoring & Modularization
When asked to analyze coupling or split up a module:
- Use `analyze_coupling` to measure afferent/efferent coupling of a directory (e.g., `src/payments/`).
- Use `extract_module_interface` to see the "public surface" of a directory (what external code consumes from it).
- Use `find_modularization_seams` to identify natural split points inside a tightly coupled directory.
- Use `find_cycles` to detect circular dependency chains.
- Use `find_external_dependencies` to see what 3rd-party packages a repository relies on.

## Anti-Patterns
- **Do not loop `semantic_search` excessively.** If semantic search doesn't find it, switch to `find_symbol`, or use standard `grep_search`.
- **Do not guess the `repo` parameter.** Call `list_repositories` first.
- **Do not bypass stale-index problems with ad-hoc heuristics.** If the index is stale, run local re-ingestion first.
