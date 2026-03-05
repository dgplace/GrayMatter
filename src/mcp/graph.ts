/**
 * @file src/mcp/graph.ts
 * @brief Pure graph algorithms for dependency analysis.
 *
 * Operates on lightweight directed edge lists (file paths as nodes)
 * to keep memory usage manageable on large codebases.
 */

import type { GraphEdge } from "./types.js";

/**
 * @brief Finds all simple cycles up to a given length in a directed graph.
 *
 * Uses iterative DFS from each node, looking for paths back to the start.
 * Deduplicates rotational equivalents by only recording a cycle when the
 * start node is the lexicographically smallest in the cycle.
 *
 * @param edges Directed edges as {source, target} pairs.
 * @param maxLength Maximum cycle length to search for (inclusive).
 * @returns Array of cycles, each cycle being an array of node identifiers.
 */
export function findCycles(edges: GraphEdge[], maxLength: number): string[][] {
  const adj = new Map<string, Set<string>>();
  for (const { source, target } of edges) {
    if (source === target) continue;
    if (!adj.has(source)) adj.set(source, new Set());
    adj.get(source)!.add(target);
  }

  const allNodes = Array.from(adj.keys()).sort();
  const cycles: string[][] = [];
  const seen = new Set<string>();

  for (const startNode of allNodes) {
    const stack: Array<{ node: string; path: string[] }> = [
      { node: startNode, path: [startNode] },
    ];

    while (stack.length > 0) {
      const { node, path } = stack.pop()!;
      const neighbors = adj.get(node);
      if (!neighbors) continue;

      for (const next of neighbors) {
        if (next === startNode && path.length >= 2) {
          // Only record if startNode is lexicographically smallest (avoids rotational duplicates)
          const minNode = path.reduce((a, b) => (a < b ? a : b));
          if (startNode === minNode) {
            const key = [...path].sort().join("\0");
            if (!seen.has(key)) {
              seen.add(key);
              cycles.push([...path]);
            }
          }
        } else if (
          !path.includes(next) &&
          path.length < maxLength
        ) {
          stack.push({ node: next, path: [...path, next] });
        }
      }
    }
  }

  return cycles.sort((a, b) => a.length - b.length || a[0].localeCompare(b[0]));
}

/**
 * @brief Counts how many cycles each node participates in.
 * @param cycles Array of cycles from findCycles().
 * @returns Map of node identifier to cycle participation count, sorted descending.
 */
export function rankNodesByCycleParticipation(
  cycles: string[][],
): Array<{ node: string; cycleCount: number }> {
  const counts = new Map<string, number>();
  for (const cycle of cycles) {
    for (const node of cycle) {
      counts.set(node, (counts.get(node) || 0) + 1);
    }
  }

  return Array.from(counts.entries())
    .map(([node, cycleCount]) => ({ node, cycleCount }))
    .sort((a, b) => b.cycleCount - a.cycleCount);
}
