# One Muse per person, behind a product mode

> **Status:** the one-Muse rule stands. The product-mode switch and the soft-fork approach are gone (ADR 0002, ADR 0003).

This fork turns the upstream multi-bot platform into a single personal agent: each person has exactly one Muse, and the only other agents are short-lived Helpers the Muse starts itself. Creating peer bots (the sidebar "+", onboarding bot setup, and the `spawn_bot` / `update_bot` / `archive_bot` / `message_bot` / `handoff_to_bot` tools) is locked, not merely hidden, so there is never a second persistent memory or host the person has to reason about.

The fork is our own product, never proposed upstream. Muse behaviour sits behind one product-mode setting. (Originally a soft fork that avoided editing upstream files; superseded by ADR 0002, which allows refactoring them.)

## Considered options

- **Keep multi-bot, make the Muse the default.** Rejected: two mental models in one product, and the Muse experience depends on a single relationship.
- **Let the Muse create hidden persistent helper bots.** Rejected: each one would carry its own memory and host the person cannot see; long-running specialist work becomes a goal the Muse works on instead.
- **Hard fork.** Rejected at first to keep upstream fixes merging; revisited in ADR 0002, which lets us refactor upstream code we own.
