# Superchat is standalone: no upstream, no fork

> **Update:** the multi-bot mode has since been retired and the `SUPERCHAT_PRODUCT_MODE` switch removed; Muse is the only behaviour, unconditionally. The paragraph below is left as the historical record of the decision at the time.

ADR 0002 still described this codebase as a fork that pulled upstream fixes selectively. In practice the two products have fully diverged: Muse, Goals, the Feed, and the Superchat-specific product surface are most of what the codebase now does, the remaining shared modules have been refactored freely, and no change has been pulled from upstream in a long time. Calling it a fork no longer describes reality and invites confusion about where updates come from.

Superchat is its own product, not a fork. There is no upstream remote, no merge or sync process, and no expectation that a file's history ties it to another project. The `SUPERCHAT_PRODUCT_MODE` switch (`muse` vs `superchat`) stays for now purely as an internal product-mode toggle between the single-Muse edition and the full multi-bot edition; it carries no fork or merge-compatibility meaning and will be simplified in a later cleanup once the multi-bot mode is retired.

The cost is that any fix made independently by the original project has to be noticed and reimplemented here by hand, if it is wanted at all; there is no tooling or process that surfaces it automatically. We accept that: this project now moves on its own roadmap.

## Considered options

- **Keep syncing selectively (status quo per ADR 0002).** Rejected: there is no active sync happening, and the docs, license notices, and CI still pointed at the upstream repository, which is misleading for a project distributed on its own.
- **Re-fork from upstream at a later point if useful.** Not ruled out in principle, but not a plan; it would be a deliberate one-time import, not an ongoing relationship.
