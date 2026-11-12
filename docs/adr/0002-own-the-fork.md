# Own the fork: refactor upstream code when it helps

> **Status:** Superseded by [ADR 0003](0003-superchat-is-standalone.md), which stops tracking upstream entirely.

ADR 0001 kept the Muse edition a soft fork: Muse code in new files, only small additive edits to upstream files, so upstream updates would keep merging cheaply. We now own this branch as our product. When an upstream file gets in the way of clean, maintainable Muse code, we refactor it, starting with splitting `web/packages/adapters/src/executor.ts` (~5,700 lines) into focused modules before the Goal work (B5, B8) lands on it.

The cost is that pulling upstream changes into refactored files becomes a manual port instead of a merge. We accept that: upstream fixes are pulled selectively, not wholesale. The `SUPERCHAT_PRODUCT_MODE` switch stays, so upstream behaviour is still reachable and testable, but it is no longer a merge-compatibility guarantee.

## Considered options

- **Keep the soft fork with a thin seam** (all Muse logic in new modules, at most three marked call sites in `executor.ts`). Rejected: it keeps a 5,700-line file we would still have to read and change for every run-engine fix.
- **Contribute the split upstream first.** Not ruled out, but not a prerequisite; we don't wait on another project's review.
