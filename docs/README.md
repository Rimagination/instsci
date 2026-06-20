# InstSci Documentation

This folder records the operational rules behind InstSci's publisher PDF
workflows. The project should keep improving its own visible CloakBrowser
execution layer rather than replacing it with a separate browser runtime.

## Core Docs

- `architecture.md`: high-level architecture and non-negotiable design rules.
- `browser-execution-layer.md`: browser action model, human handoff state
  machine, publisher-skill checklist, session identity rules, and done
  criteria.
- `performance.md`: speed, broker persistence, long-lived sessions, health
  checks, and resume commands.
- `opencli-bridge.md`: optional OpenCLI Browser Bridge diagnostics and
  attach-only usage boundary.

## Practical Reading Order

1. Read `architecture.md` to understand what owns each part of the workflow.
2. Read `browser-execution-layer.md` before changing browser automation,
   human-assist behavior, publisher profiles, or attach-only control tools.
3. Read `performance.md` before tuning speed, login persistence, broker TTL,
   resume behavior, or large overnight batches.
4. Read `opencli-bridge.md` only when configuring or evaluating OpenCLI inside
   the existing visible CloakBrowser context.
