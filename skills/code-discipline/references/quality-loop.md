# Repository quality loop

Read this reference before running or updating the bundled quality loop.

## Entrypoint and configuration

Run from the target repository root, in the foreground, and read its exit code:

```bash
python3 <skill-directory>/scripts/quality_loop.py --root . --local-changes --fast
```

In a Claude Code plugin, the skill directory is
`${CLAUDE_PLUGIN_ROOT}/skills/code-discipline`; in Codex, use the directory of
the loaded `SKILL.md`.

`.quality/quality-gate.json` owns commands and adapters. `.quality/quality-thresholds.json`
owns every numeric goal and overrides the bundled defaults in
`../quality-thresholds.json`, including `file_loc.max_lines: 600`. Never copy
thresholds into `.quality/quality-gate.json`. `.quality/quality-dependencies.json`
owns module boundaries; `--init` writes a generated skeleton to review.

To update the complete installed skill:

```bash
python3 <skill-directory>/scripts/install.py --update-current [--ref REF]
```

## Scope

- `--local-changes` selects staged, unstaged, and untracked production files.
- `--commit [REF]` selects a committed diff; omitted `REF` means `HEAD`.
- Omitting both selects the whole repository.

Incremental scope limits file-aware metrics, mutation, dependency analysis, and
inferred formatter/linter commands. Complete tests and explicitly configured
commands retain repository scope. Never describe an incremental pass as
whole-repository certification.

## Choosing a check

| Flags | Runs | Use it when |
|---|---|---|
| `--fast` | lint, types, contracts, tests, coverage + CRAAP, file size, dead code; defers flaky and mutation | first look after a change |
| `--lint` · `--types` · `--contracts` | that one check | after an edit, a signature change, a schema change |
| `--tests` | the complete test suite only | after writing a test |
| `--coverage` | tests plus per-function coverage; lists every uncovered function in scope | deciding which test to write next |
| `--complexity` | static complexity per function, no tests | after a refactor |
| `--craap` | tests, coverage and complexity, CRAAP per function | before a commit |
| `--loc` · `--dead-code` · `--deps` | file size, unused code, module boundaries | before handoff, after adding imports |
| `--smoke` | starts the application once and loads it (`smoke.commands`) | after the app first starts, after changing how it starts |
| *(no check flags)* | **the ship report**: every enabled gate, including Runs (smoke) and Gate scope | before handoff, until it is green |
| `--flaky` · `--mutation` | 3× repeated suite, mutation testing | only when the user asks |

Check flags combine (`--coverage --lint`). A run with check flags is a *partial*
run: it exits 0 when every selected check passes, and it never certifies. Runs
with no check flags are full runs; `--fast` is a full run that defers the slow
gates and therefore never exits 0.

## The report

Every mode prints the same report to the terminal (the HTML file is for
people):

1. `QUALITY REPORT · mode · scope` — one line.
2. One line per gate: `[PASS|FAIL|OFF|SKIPPED|DEFERRED|N/A] title: summary`,
   and for a failing gate up to eight lines naming the exact items
   (`path:line name: coverage …, complexity …`). `[N/A]` means an optional
   check is not configured; the line says so and it is not a to-do item.
3. `Coverage today: N of M functions fully covered (x%) · k not covered — most in: …`
   — printed in every mode; when a run measures nothing it repeats the last
   measurement with its timestamp. Use it to plan tests, not to discover gaps
   at the end.
4. `To fix:` a numbered list: functions, oversized files, surviving mutants,
   dependency violations, then other failing gates.
5. The next step: the exact command to rerun, the full ship-report command
   when a partial or fast run is green, or — when the ship report is green —
   `HAND OFF NOW`: the next message is the hand-off; nothing is added after
   green.
6. `QUALITY_LOOP=PASS|FAIL|READY_FOR_FULL|ERROR`, `ITEMS_TO_FIX=n`, `STATE=`,
   `HTML=`.

Gates marked OFF (mutation, flaky) are off in the configuration and run only
when requested with `--mutation` / `--flaky`, or when the configuration enables
them. Nothing is skipped silently: skipped, deferred, and off gates are listed.

## Results and the repair loop

- Exit `0`: every executed check passed (a partial run's selected checks, or
  the full ship report).
- Exit `1`: read the `To fix` list (and `fix_prompt` / `failures` in the state
  file), repair one coherent batch, run the focused test, and rerun the same
  command. In fast mode exit 1 may instead mean READY_FOR_FULL.
- Exit `2`: configuration, an adapter, or the runner failed; repair that
  blocker first — it is an item to fix, not a reason to stop.

Run the loop in the foreground and read the exit code in the same command.
Never run it in the background or wait for a notification: a session that ends
while a run is in flight has produced nothing. Coverage and CRAAP measurements
are diagnostic until the unmodified baseline suite passes; check
`metrics.certified` before reporting function measurements as certified.

Before the first run, preserve existing worktree changes. Only one loop may run
per repository; coverage and mutation tools often share temporary paths.

## Runs (smoke): the application must start

The ship report has a **Runs (smoke)** row. It executes `smoke.commands` from
`.quality/quality-gate.json` and is red until a command is configured and
green — a test suite that mocks the network cannot stand in for it. `--init`
generates the command when it can (an npm `start` script, a Python package
import); otherwise add one:

```json
"smoke": {"commands": [["python3", "<skill-directory>/scripts/smoke_check.py",
  "--start", "npm start", "--browser", "--expect-selector", "canvas"]]}
```

`smoke_check.py` picks a free port, exports it as `PORT` (change with
`--port-env`), starts the command, waits for an HTTP answer, and with
`--browser` loads the page in headless Chromium (Python Playwright, or the
repository's `node_modules/playwright`) and fails on any page error, console
error, a missing `--expect-selector`, missing `--expect-text`, or page text
that looks like a failure (`error`, `exception`, `failed`, `could not`;
override with `--fail-on-text`). It always stops the process. For a CLI or
library use the entry point itself (`["python3", "-c", "import pkg"]` or
`--help`). In fast mode the row is deferred; `--smoke` runs it alone.

## Gate scope: no hidden files

The **Gate scope** row fails when `source.exclude` hides a production file
beyond the standard test patterns. Only tooling files (`*.config.*`, rc files,
`*.d.ts`, `conftest.py`, `setup.py`) may be excluded. An entry point that is
hard to unit-test gets a startup or smoke test, not an exclusion.

## The ship report

Before handoff run `--local-changes` with no check flags (or `--commit REF` for
a requested commit). A green ship report means every applicable formatter,
lint, type, contract, test, coverage, complexity, CRAAP, smoke, file-LOC,
dead-code, module-boundary, and gate-scope goal passed, plus flaky and
mutation goals when they are enabled or requested. Red means not finished: fix
every listed item and rerun until it exits `0`. Never lower thresholds, disable
a gate, cap the final mutation run, skip tests, weaken assertions, add
pass-only suppressions, broaden allow-lists, exclude production files, or
replace checks with no-ops. If a gate cannot measure valid code, repair the
adapter or configuration — never rewrite correct code merely to manufacture a
measurable target. When the report prints `HAND OFF NOW`, the next message is
the hand-off: the deliverables list and the report's status line. Nothing is
added after green — not a linter, not a formatter, not another check.

## Full-run invariants

Use `--mutation-workers auto` unless an explicit positive worker limit is
needed. Native Vitest/Stryker uses related-test selection and a content-addressed
proof cache; other stacks use the portable snapshot engine. Both treat
`Survived`, `NoCoverage`, `Timeout`, and runner errors as failures and leave the
active worktree unchanged.
