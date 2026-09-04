---
name: code-discipline
description: Engineering discipline for any programming language. Use whenever code is written, reviewed, refactored, debugged, tested, or designed, including quick fixes and new projects. Enforces readable design, behavior-preserving changes, regression tests, explicit failures, and the configured quality loop. Bootstrap the gate before source in new projects. Work ends only when the requested result exists, the core interaction works, and the ship report is green.
---

# Code discipline

Optimize for the reader: code is read more than it is written. Prefer the
version a competent stranger understands faster; practicality breaks ties.

## Repository quality loop

When `.quality/quality-gate.json` exists, read
[references/quality-loop.md](references/quality-loop.md) completely and follow
it. These nine rules are binding:

1. **Deliverables first; gate before source.** List the requested code, tests,
   docs, runnable entry point, and decisions. A new project starts with a
   manifest, one real passing test, and a start command; run `--init` and make
   `--local-changes --fast` green before its first feature.
2. **Use the report as the interface.** Drive repairs from the terminal report
   and `quality_items.py --next`, not the state file or HTML. Fix one file per
   cycle. A focused file test is fine; let the next fast run own repository-wide
   tests, lint, types, coverage, and metrics. Combine targeted gate flags only
   when the report asks for them.
3. **Run in the foreground.** Read the exit code from the same command. Never
   background the loop, duplicate an active run, or end while it is running.
4. **Prove the core user story.** For a web app, `smoke.story` must drive the
   real app from outside the process; every fresh probe and page-error check
   passes. Use `smoke.commands` for a CLI or library, and make a changing
   entry point survive one real write. Unit mocks or one browser drag do not
   prove a composed workflow.
5. **Green ship report means finished.** Before handoff, run without check
   flags: `--local-changes` for an existing change, or whole-repository scope
   for a new project or requested audit. Fix every red row. Never lower a
   threshold, disable a gate, weaken a test, hide production with
   `source.exclude`, or replace a check with a no-op.
6. **Mutation and flaky checks run only when asked.** They otherwise remain OFF;
   N/A means an optional check is not configured, not a defect.
7. **Hand off immediately after green.** After `QUALITY_LOOP=PASS`, report the
   completed deliverables and status line; add nothing else.
8. **Commit green features, not micro-edits.** Keep each feature green so the
   next incremental run measures only new work. Empty incremental scope proves
   nothing. Never push, amend, or rewrite history unless the user asks.
9. **Fan out by ownership; measure once.** For more than 12 items across more
   than 3 files, or independent directories, use at most four sub-agents, one
   production file plus its test per owner. The parent owns shared contracts,
   configuration, measurement, and commits. Sub-agents do not run `--init`,
   edit `.quality/`, or commit. Wait for all, then run one parent fast gate.

For setup, adapters, and installation, follow
[references/repository-setup.md](references/repository-setup.md). In an existing
repository without a gate, use its focused checks and install the gate only
when asked. For a requested commit, use `--commit [REF]`. Audit or
certification by itself is read-only.

## Readable design

- Use honest names and the codebase's established idioms. Comments explain
  *why*, never what a clearer name or function could say.
- A function does one thing at one abstraction level. Prefer guard clauses and
  a flat happy path. None or one argument is ideal; many arguments need a
  named value object. Replace boolean mode arguments with named operations.
- A command changes state; a query returns information. Make mutation, global
  state, I/O, and multi-clause conditions explicit in names and structure.
- Extract a shared abstraction only when the domain rule is truly shared; the
  third occurrence is the default signal. Duplication is cheaper than coupling
  unrelated ideas.
- Do not reach through chains of objects. Wrap external code behind the
  smallest interface you own and test; authorship never creates trust.

## Explicit failures

- Keep failure paths separate from the happy path. Catch only what can be
  handled, preserve the cause when wrapping, and never silently swallow an
  error without a documented reason.
- Prefer typed absence or an honest empty value to ambiguous null. A documented
  not-found result is valid absence.
- Every new I/O path defines and tests partial failure. A secondary feature
  degrades without breaking the primary path; background failures become
  visible.
- A network client surfaces disconnection and reconnects or states that it
  cannot. Offline edits are queued or visibly refused, never silently lost.
- If speed requires a cut, name the deferral and its reason. Never defer error
  handling for money, authorization, data integrity, or the riskiest tests.

## Tests and safe change

- Start with a failing test and observe it fail on the unfixed code. Reproduce
  the reported symptom, magnitude, and realistic inputs; a nearby defect is
  not the cause until it explains them.
- Test public behavior with production-like values, not tuned internals. Keep
  tests fast, independent, deterministic, wired into CI, and focused on one
  promise.
- Refactoring changes structure only. Preserve signatures, defaults, argument
  flow, statement order, and failure behavior; ship improvements separately.
- Deliver the smallest complete fix. Cover the boundary just beyond it, and
  keep the suite green before and after.

## Architecture and output

- One actor and one reason to change per module. Dependencies point toward
  stable policy; frameworks, storage, and networks stay behind boundaries.
  Add layers only for a second caller or implementation.
- Keep shared mutable state rare, named, owned, and guarded; isolate
  concurrency.
- In code, let the discipline show through structure rather than comments.
  In reviews, report defects first, then design, then style; say clearly when
  the code is already sound.
