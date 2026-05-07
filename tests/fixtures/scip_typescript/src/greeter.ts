/**
 * @file greeter.ts
 * @brief Fixture declarations for TypeScript SCIP resolver tests.
 */

/** @brief Simple greeter used by the resolver fixture. */
export class Greeter {
  /** @brief Return a deterministic greeting. */
  greet(): string {
    return "hi";
  }
}
