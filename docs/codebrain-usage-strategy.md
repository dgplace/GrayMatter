# Codebrain MCP — Usage Strategy

Guidance for agents querying a Codebrain-indexed repository.

## Principle

Codebrain is an index, not a search engine. Each tool answers a *different* question. Picking the right tool is more important than picking the right keywords. Reach for `semantic_search` only when no more precise tool fits.

## The loop

1. **Orient** with `list_repositories`, then `get_file_map` or `get_module_map` on the target subsystem. Know roughly where the relevant code lives before searching.
2. **Pick the tool that matches the question** (table below).
3. **Read the result.** Once a plausible file or symbol surfaces, open it. Do not keep searching for a better hit.
4. **Harvest vocabulary.** The first read reveals the codebase's own names (classes, functions, identifiers). Use those in any follow-up query.
5. **Stop rule.** After 2 unproductive queries, change *tool*, not keywords. Reordering or rephrasing the same words against the same tool wastes calls — embeddings treat them as the same query.

## Tool selection

| Question | Tool |
|---|---|
| What repos are indexed? | `list_repositories` |
| What's in this directory/subsystem? | `get_file_map`, `get_module_map` |
| Where is symbol `X` defined? | `exact_symbol_search`, `find_symbol` |
| Where is concept X discussed (name unknown)? | `semantic_search` |
| Who calls / references this symbol? | `find_references` |
| What does this file/symbol/cluster do? | `describe_node` |
| What's the public API of this module? | `extract_module_interface` |
| What does this symbol depend on? | `find_call_graph`, `trace_dependencies` |
| What breaks if I change this? | `find_impact` |
| Are there circular dependencies? | `find_cycles` |
| What implements this interface / extends this type? | `find_implementations`, `find_subtypes`, `find_supertypes` |
| Where is this type instantiated? | `find_instantiations` |
| How does data flow through here? | `find_flows` |
| What external libraries does this touch? | `find_external_dependencies` |
| Where could I split this module? | `find_modularization_seams` |
| How tightly coupled are these? | `analyze_coupling` |
| What's the intent/role of this code? | `get_intent` |
| What semantic clusters exist? | `clusters`, `cluster_members` |
| How big is the index? | `codebase_stats`, `get_index_size` |

## Anti-patterns

- **Permuting keywords against `semantic_search`.** Word-order shuffles and synonym swaps produce near-identical embedding vectors. If one query misses, switch tools — don't rephrase.
- **Sticking with the user's vocabulary.** User terms rarely match codebase identifiers. After the first read, prefer the codebase's own names.
- **Searching instead of reading.** Once a result is plausible, open the file. Reading beats more searching.
- **Skipping orientation.** Searching before knowing which module owns the concern wastes calls and gives diffuse results.
- **Using `semantic_search` for exact identifiers.** If the name is known, `exact_symbol_search` is faster and unambiguous.

## Query design for `semantic_search`

- One concept per query. Stacking 4–5 terms averages the embedding into noise.
- 2–8 words, technical phrasing, framework/API/domain names.
- Use `path_prefix` and `intent` filters when you already know the subsystem or role.
- Lower `threshold` only when the codebase's vocabulary is sparse or unusual.
