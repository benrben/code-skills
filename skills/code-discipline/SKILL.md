---
name: code-discipline
description: Install and set up code-discipline for a project in Codex and Claude Code, including local checkout installs and a short conversation about checks and limits. Apply the saved policy during coding, review, refactoring, debugging, and tests.
---

# Code discipline

## Installation includes setup

A request to install code-discipline for a project includes first-time setup
in the same conversation. Read [guided project setup](references/project-setup-chat.md)
before installing. Install and verify the requested copy, then start the first
setup question directly; do not end with an installation summary or ask whether
to do setup next. A restart for future skill discovery does not prevent the
current agent from reading the installed instructions and continuing now.

Use the supported local command when a checkout is provided; do not download
GitHub instead or improvise with installer internals. Complete the two user
choices, save the policy, and verify the enabled checks before handoff. If the
project has settings but no `setup.completed: true`, preserve those settings as
the starting recommendation and finish the conversation. If that marker is
already true, reuse the completed policy. Pending user choices remain pending;
installation alone is not "ready". Report real verification blockers separately
and continue the setup choices when they do not depend on the blocker.

## Engineering discipline

Treat the configured gates as acceptance criteria before writing code. Design
the implementation and tests together to pass on the first run. Measure early;
first-pass green is a design target, never a reason to delay checks. Own the
complete result until every required gate passes. Prefer code a competent
stranger understands quickly.

## The ten gates

The gates run when you finish a piece of work and judge your change. With
`--local-changes` or `--commit`, only the lines and functions you added or
touched count; whole-repository scope judges everything. They need no project
setup for common languages: they use the commands the repository already
declares, plus pinned tools the skill installs into its own cache (lizard,
Pygments, coverage.py, c8, cargo-llvm-cov, JaCoCo). They never install project
dependencies. They run in this order:

1. **No shortcuts** (`--shortcuts`). New suppression, ignore, skip, or focus
   markers on added lines of any file (`# type: ignore`, `eslint-disable`,
   `pragma: no cover`, `it.skip`, `.only`, `#[ignore]`, and similar); new
   silent error handlers in production code; production files hidden by
   `source.exclude`; and edits to `.quality/` policy files. These are the
   quickest ways to turn a red report green without fixing anything. Policy
   edits pass only with `--allow-policy-change` after the user approves.
   Changed lint, type, or test configuration is listed for review, not failed.
   Needs no tools; it matches text, so it cannot judge whether a marker is
   justified.
2. **Secrets** (`--secrets`). Private keys and well-known token formats (AWS,
   GitHub, GitLab, Slack, Stripe, Anthropic, OpenAI, Google, npm, PyPI, and
   others) in every text file of the change, including `.env`, config, tests,
   and docs. A touched file with an old secret still fails. Needs no tools;
   it finds known formats only, not arbitrary passwords.
3. **File size** (`--loc`). A changed production file that crosses the line
   limit, or that was already over and grew. Stops one file from absorbing
   every new feature. Needs no tools.
4. **Project checks** (`--checks`). The repository's own lint, type, format,
   and build commands: `package.json` scripts, configured ruff and mypy,
   `go vet` and `gofmt`, `cargo check` and `cargo fmt`, `dotnet build`,
   `swift build`, or Makefile targets. Each must exit 0. A command that fixes
   files or cannot fail (`--fix`, `--write`, `|| true`) is rejected. Uses only
   what the repository declares; with nothing declared the gate is N/A.
5. **Tests** (`--tests`). Runs the test suite once and reads the runner's
   summary. Fails on failed, skipped, or filtered-out tests, on zero executed
   tests, and when the test count drops below the last full run. Removing
   tests needs user approval and `--allow-test-removal`. Uses the repository's
   own test command; output with no recognizable summary is BLOCKED.
6. **Coverage** (`--coverage`). Line and branch coverage of the changed
   executable lines, measured from that same test run. A changed file the
   tests never load counts as uncovered. Proves new code is exercised, not
   merely compiled. The skill wraps the test command for Python, JavaScript
   and TypeScript, Go, Rust, JVM, Ruby, Swift, and C/C++ (CMake or Meson,
   built with `--coverage` and read with gcov); .NET needs the
   `coverlet.collector` package in its test projects and PHP needs pcov or
   Xdebug. Until coverage can be measured the gate reports BLOCKED, never
   PASS.
7. **Complexity** (`--complexity`). Cyclomatic complexity, cognitive
   complexity, nesting depth, and CRAP of each touched function, so new logic
   stays readable and tested in proportion to its branching. Uses lizard;
   `lizard forgive` comments cannot hide a function. CRAP needs coverage data.
8. **Duplication** (`--duplication`). Copied code that touches your change,
   compared against every production file, including copies with renamed
   variables or inserted comments. Uses Pygments tokens; import blocks are
   ignored.
9. **Dead code** (`--dead-code`). Functions you added that nothing calls, and
   functions whose callers you removed. Uses lizard's function list and a
   name search across production files (a call from a test does not count); exported, decorated, and method names
   are reported for review rather than failed, because they may be called
   from outside or registered dynamically.
10. **Vulnerable dependencies** (`--vulnerabilities`). Known advisories (OSV)
    for packages your change adds or upgrades in a lockfile (every locked
    package in whole-repository scope). A manifest with
    dependencies but no lockfile fails. Needs network access; offline it is
    BLOCKED. `--fast` defers only this gate. N/A when the repository has no
    dependency manifest.

Read the [quality loop reference](references/quality-loop.md) for the exact
rules and [repository setup](references/repository-setup.md) for adapters.

## Repository quality loop

Quality settings belong only to the target project. Read its existing
`.quality/quality-gate.json` and `.quality/quality-thresholds.json` before coding.
Reuse saved choices without asking again. Never change the skill's bundled
defaults, global settings, or another project's policy to configure this project.
For first-time setup, read [guided project setup](references/project-setup-chat.md)
and offer a simple user flow: recommended checks or customization, then suggested
limits or adjustments. Explain the recommendations briefly; show individual
gate keys and detailed metrics only when requested. Discuss choices in chat
before initializing or running the baseline. Installation or invocation of the skill does not accept strict
defaults. Existing projects without a gate use focused checks unless the user
asks to set up project gates. Existing configuration is already a project policy;
offer the guided setup when the user asks to review it.

Once configured, know the commands, scope, coverage, and complexity requirements.
Use `scripts/quality` and the
[repair procedure](references/quality-loop.md#results-and-the-repair-loop).
Read additional reference sections when configuring or diagnosing their checks.
The runner may install quality tools, never project dependencies. Missing
prerequisites remain explicit blockers; do not treat missing evidence as a pass.

1. **Bootstrap before features.** Map requested behavior to tests and gate
   evidence. New projects need a manifest, a test command, and a real passing
   test before the first feature. Complete guided setup, initialize
   configuration without running checks, save the chosen policy, then make
   the skeleton's `--fast` run green. Verify coverage and analyzer support
   now; add a coverage adapter where the language needs one before adding
   features. Use whole-repository scope for new projects.
2. **Take no shortcuts.** Never add suppression, skip, or focus markers, empty
   error handlers, or `source.exclude` entries for production files to get
   past a gate. Fix the cause instead. Edit `.quality/` policy or delete tests
   only when the user explicitly approves, then rerun with
   `--allow-policy-change` or `--allow-test-removal`.
3. **Build complete connections.** Connect input, logic, dependencies, and visible
   results. For web apps, drive the browser and check that the page reports no
   errors. Prove a real write and read-back after reload when state changes.
   Exercise failure recovery.
4. **Repair until green.** Read the terminal report and `quality_items.py --next`.
   Fix the cause, run focused tests, then rerun the gate. Failures, missing
   coverage, broken adapters, and unfinished features are work to complete,
   never a successful stopping point. Do not hand back a repair list while you
   can act. No fixed retry count. Never lower thresholds or disable enabled
   gates merely to obtain green. The user can revise project policy explicitly;
   explain the changed evidence, save the choice, and rerun. Never weaken
   assertions, hide production files, or substitute no-ops.
5. **Certify the final code.** Once fast checks pass, run the
   [full ship report](references/quality-loop.md#the-ship-report) without check
   flags. Use `--local-changes` for an existing change, whole-repository scope
   for a new project or audit, or `--commit [REF]` for a requested commit.
   Repair and repeat until it exits `0` with `QUALITY_LOOP=PASS`.
   Run in the foreground and capture that command's actual exit code; a pipe
   to `tee` can hide failure. Any subsequent edit invalidates the handoff check.
6. **Stop only on evidence.** Deliver the complete requested result plus the
   fresh full-run status, scope, and disabled gates. A PASS certifies only the
   project's enabled checks. All gates OFF provides no certification.
   Fast/selected passes do not certify.
   A supervising agent must independently rerun certification and return
   failures for repair. Honor cancellation; if an external prerequisite truly
   prevents progress, report BLOCKED, evidence, and the exact dependency needed.
   Do not describe incomplete work as done.
7. **Keep gate choices honest.** Disabled checks remain OFF, never PASS or N/A.
   N/A counts only for Project checks with no declared commands and Vulnerable
   dependencies with no manifest. Any other gate that cannot measure is
   BLOCKED. Missing evidence for an enabled gate is a blocker.
8. **Commit green features, not micro-edits.** Keep incremental runs focused on
   new work. Empty scope proves nothing. Never push, amend, or rewrite history
   unless the user asks.
9. **Fan out by ownership; measure once.** For more than 12 items across more
   than 3 files, or independent directories, use at most four sub-agents, each
   owning one production file and its tests. The parent owns shared contracts,
   configuration, measurement, and commits. Sub-agents never run `--init`, edit
   `.quality/`, or commit. Wait for all, then run one parent fast gate.

For first use, follow [guided setup](references/project-setup-chat.md): explain
the recommended checks and limits in two short choices. Walk through individual
settings only when the user chooses customization. Then initialize
and apply them in the target project's `.quality/` files before running checks.
See [runner setup](references/quality-loop.md#first-use-setup) and
[repository setup](references/repository-setup.md) for installation and adapters.
Audit or certification alone is read-only.

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
