---
name: code-discipline
description: Engineering discipline for coding, review, refactoring, debugging, and tests. Design for first-pass green; repair until the complete requested behavior, Gherkin acceptance scenarios, and full ship report pass.
---

# Code discipline

Treat the configured gates as acceptance criteria before writing code. Design
the implementation and tests together to pass on the first run. Measure early;
first-pass green is a design target, never a reason to delay checks. Own the
complete result until every required gate passes. Prefer code a competent
stranger understands quickly.

## Repository quality loop

When `.quality/quality-gate.json` exists, read it and the active thresholds
before coding: know the commands, scope, coverage, complexity, and timing requirements.
Use `scripts/quality` and the
[repair procedure](references/quality-loop.md#results-and-the-repair-loop).
Read additional reference sections when configuring or diagnosing their checks.
The runner may install quality tools, never project dependencies. Missing
prerequisites remain explicit blockers; do not treat missing evidence as a pass.

1. **Bootstrap before features.** Map requested behavior to acceptance scenarios
   and gate evidence. New projects need a manifest, start command, real passing
   test, lint/type checks, and a working branch-coverage adapter. Run `--init`,
   configure required checks, then make the skeleton's `--fast` run green.
   Verify analyzer support now; repair missing tooling or adapters before adding
   features. Use whole-repository scope for new projects.
2. **Specify behavior in YAML.** Write readable `.feature.yaml` files with
   ordered `given`/`when`/`then` steps for requested workflows and failures. Bind steps
   to real public behavior and observable assertions. The required
   [Gherkin gate](references/quality-loop.md#gherkin-acceptance) compiles the YAML and runs every
   scenario and Examples row; missing, skipped, undefined, or failed steps fail.
   Feature text, mocked wiring, and an empty runner are insufficient.
3. **Build complete connections.** Connect input, logic, dependencies, and visible
   results. For web apps, drive the browser, capture actual page errors, and
   require zero. Prove a real write and read-back after reload when state changes.
   Exercise failure recovery. Measure application and test timings against
   configured budgets; without a budget report measurements, not a speed claim.
4. **Repair until green.** Read the terminal report and `quality_items.py --next`.
   Fix the cause, run focused tests, then rerun the gate. Failures, missing
   coverage, broken adapters, and unfinished features are work to complete,
   never a successful stopping point. Do not hand back a repair list while you
   can act. No fixed retry count. Never lower thresholds, disable required
   gates, weaken assertions, hide production files, or substitute no-ops.
5. **Certify the final code.** Once fast checks pass, run the
   [full ship report](references/quality-loop.md#the-ship-report) without check
   flags. Use `--local-changes` for an existing change, whole-repository scope
   for a new project or audit, or `--commit [REF]` for a requested commit.
   Repair and repeat until it exits `0` with `QUALITY_LOOP=PASS`.
   Run in the foreground and capture that command's actual exit code; a pipe
   to `tee` can hide failure. Any subsequent edit invalidates the handoff check.
6. **Stop only on evidence.** Deliver the complete requested result plus the
   fresh full-run status and scope. Fast/selected passes do not certify.
   A supervising agent must independently rerun certification and return
   failures for repair. Honor cancellation; if an external prerequisite truly
   prevents progress, report BLOCKED, evidence, and the exact dependency needed.
   Do not describe incomplete work as done.
7. **Keep optional checks honest.** Mutation and flaky checks run only when
   requested. N/A is valid only for an optional, inapplicable check; required
   tooling and Gherkin acceptance cannot become N/A to obtain green.
8. **Commit green features, not micro-edits.** Keep incremental runs focused on
   new work. Empty scope proves nothing. Never push, amend, or rewrite history
   unless the user asks.
9. **Fan out by ownership; measure once.** For more than 12 items across more
   than 3 files, or independent directories, use at most four sub-agents, each
   owning one production file and its tests. The parent owns shared contracts,
   configuration, measurement, and commits. Sub-agents never run `--init`, edit
   `.quality/`, or commit. Wait for all, then run one parent fast gate.

For first use, follow [setup](references/quality-loop.md#first-use-setup): run
`scripts/quality --root . --init --non-interactive`, then ask only pending
product, runtime, or risk questions, at most three at a time. See
[repository setup](references/repository-setup.md) for installation and adapters.
In existing repositories without a gate, use focused checks; install only when
asked. Audit or certification alone is read-only.

## Readable design and architecture

- Use honest names and established idioms. Comments explain why. Functions do
  one thing at one abstraction level; prefer guard clauses and a flat happy
  path. Prefer few arguments, group related values, and name operations instead
  of using boolean modes.
- Separate commands from queries. Make I/O, mutation, and complex conditions
  explicit. Keep shared mutable state rare, owned, guarded, and isolated.
- Share abstractions only for genuinely shared domain rules; the third
  occurrence is the default signal. Avoid coupling unrelated ideas.
- Give each module one reason to change. Point dependencies toward stable
  policy; keep frameworks, storage, networks, and external code behind small,
  tested interfaces. Avoid object chains; add layers for a second caller or
  implementation.

## Explicit failures

- Separate recovery from the happy path. Catch only what can be handled,
  preserve causes, and never silently swallow errors without a documented
  reason. Represent absence explicitly; documented not-found results are valid.
- Define and test partial failure for every new I/O path. Secondary features
  degrade without breaking the primary workflow; background failures are visible.
- Surface disconnection; reconnect or explain that recovery is unavailable.
  Queue offline edits or visibly refuse them; never silently lose data.
- Name any deferral and its reason. Never defer error handling for money,
  authorization, data integrity, or the riskiest tests.

## Tests and safe change

- Observe the regression test fail before fixing a bug. Match the reported
  symptom, magnitude, and realistic inputs; a nearby defect is not proof of cause.
- Test public behavior and the boundary beyond the fix. Use production-like
  values and fast, independent, deterministic tests wired into CI.
- Refactor structure only: preserve signatures, defaults, argument flow,
  statement order, and failure behavior. Ship behavior changes separately.
- Deliver the smallest complete fix. In reviews, report defects, then design,
  then style; state clearly when the code is sound.
