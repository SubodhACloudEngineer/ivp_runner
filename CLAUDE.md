## What this is
A post-deployment IVP (installation verification plan) runner for Juniper Mist
sites. Checks are declared in YAML with explicit pass criteria; the runner
executes them, emits a verdict per test ID, renders evidence, and writes results
into an Excel MOP workbook.

## Hard rules
- READ-ONLY. Never call a Mist POST, PUT or DELETE endpoint. Never write to a
  device. If a task seems to need a write call, stop and ask.
- The API token comes from the `MIST_API_TOKEN` environment variable. Never
  hardcode it, never write it to a file, never log it, never put it in a test
  fixture.
- NEVER invent Mist API field names. Every field this code reads must be
  traceable to a real captured response in `samples/`. If you need a field that
  isn't in `samples/`, stop and say so rather than guessing a plausible name.
- Never modify the workbook at `reference/` in place. Always copy to `out/` first.
- Tests must not make network calls. Use captured fixtures.

## Conventions
- Every check module is addressable by a stable test ID (e.g. `AP-01`), which
  never changes even if the description does.
- Timestamps are recorded in UTC with an explicit site-local rendering alongside.
- Prompts live only in `src/ivp_runner/interactive.py` (guided mode: `python ivp.py`,
  or `ivp` at a terminal with arguments missing). `ivp run` with every argument
  given never prompts, so it stays scriptable.
- Portal screenshots: the engineer logs in to the Mist portal by hand. The tool
  never handles portal credentials, keeps nothing between runs, and after login
  blocks every browser request that is not GET, HEAD or OPTIONS.

## Out of scope for now — do not build these
Failover probe, SSH collectors, Prisma/firewall checks, Teams notifications,
parallel execution, credential vaulting, web UI, scheduling.
