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
| `--fast` | lint, types, contracts, tests, line/branch coverage, slow tests, extensibility, error handling, test integrity, CRAAP, file size, dead code; defers flaky and mutation | first look after a change |
| `--lint` · `--types` · `--contracts` | that one check | after an edit, a signature change, a schema change |
| `--tests` | the complete test suite only | after writing a test |
| `--coverage` | tests plus per-function coverage; lists every uncovered function in scope | deciding which test to write next |
| `--branches` | tests plus per-function branch coverage | after adding conditionals or exception branches |
| `--slow-tests` | complete-suite duration plus individual timings when the runner emits them | when feedback is becoming slow |
| `--extension-contracts` · `--extension-deps` | configured extension scenarios; forbidden core-to-extension imports | after adding a plug-in point or extension |
| `--failure-paths` · `--silent-errors` | error-handler coverage; empty or `pass` handlers | after changing recovery or fallback behavior |
| `--test-integrity` | same-package production mocks and null render surfaces in composed-root tests | after changing an application/root composition test |
| `--complexity` | static complexity per function, no tests | after a refactor |
| `--craap` | tests, coverage and complexity, CRAAP per function | before a commit |
| `--loc` · `--dead-code` · `--deps` | file size, unused code, module boundaries | before handoff, after adding imports |
| `--smoke` | runs the outside-process core story and parses every probe (`smoke.story`), or runs a CLI/library entry point | after changing the core workflow or startup |
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
4. `Extended metrics` in the HTML shows branch coverage, the slowest test,
   extension-contract results, core-to-extension dependencies, failure-path
   coverage, and silent handlers. Gate detail tables also show every structured
   smoke probe, catch evidence type, and test-integrity violation. The
   responsible function, test, scenario, dependency, handler, or probe remains
   visible.
5. `Since last run: fixed n · remaining m · new k` — compared with the
   previous state file, so a cycle's effect is visible without reading twice.
5. `To fix:` up to twelve items (functions, oversized files, surviving
   mutants, dependency violations, then other failing gates). More than
   twelve: the list is grouped by file with counts, most items first,
   repository-level items last, followed by `Next file:` — the
   `quality_items.py --next` command that prints the first file's items.
6. The next step: the exact command to rerun; when the fast run is green,
   "commit this step, then continue" plus the ship-report command; or — when
   the ship report is green — `HAND OFF NOW`: the next message is the
   hand-off; nothing is added after green.
7. `QUALITY_LOOP=PASS|FAIL|READY_FOR_FULL|ERROR`, `ITEMS_TO_FIX=n`, `STATE=`,
   `HTML=`.

Gates marked OFF (mutation, flaky) are off in the configuration and run only
when requested with `--mutation` / `--flaky`, or when the configuration enables
them. Nothing is skipped silently: skipped, deferred, and off gates are listed.

## Results and the repair loop

- Exit `0`: every executed check passed (a partial run's selected checks, or
  the full ship report).
- Exit `1`: read the `To fix` list, run `quality_items.py --next` for the
  items of the first file, fix that file, and rerun the same command. Never
  open the state file yourself; the items script reads it. Verify a fix with
  that file's own test only; the suite, coverage, types, and lint all run in
  the next fast run — one turn instead of four. In fast mode exit 1 may
  instead mean READY_FOR_FULL: commit the step (the report prints the exact
  one-line command) and continue. A fast run whose local-changes scope is
  empty (everything committed) measures nothing and says so; it never prints
  READY_FOR_FULL.
- Exit `2`: configuration, an adapter, or the runner failed; repair that
  blocker first — it is an item to fix, not a reason to stop.

Run the loop in the foreground and read the exit code in the same command.
Never run it in the background or wait for a notification: a session that ends
while a run is in flight has produced nothing. Coverage and CRAAP measurements
are diagnostic until the unmodified baseline suite passes; check
`metrics.certified` before reporting function measurements as certified.

Before the first run, preserve existing worktree changes. Only one loop may run
per repository; coverage and mutation tools often share temporary paths.

## quality_items.py: what to fix next

Reads the state file the loop wrote and prints only what the next cycle needs:

| Command | Prints |
|---|---|
| `quality_items.py --root . --next` | the first file with open items: every item with line, metric, and a hint |
| `quality_items.py --root . --file PATH` | the same for one file (full path or file name) |
| `quality_items.py --root . --summary` | files and counts only |
| `quality_items.py --root . --briefs N` | one ready-to-send sub-agent brief per file for the N files with the most items |

`--state PATH` selects another state file. Exit 2 means the loop has not run
yet.

## The per-step cycle

New project: skeleton (manifest, test runner, one passing test of one real
function, start command) → `--init` → `--local-changes --fast` green with 0
items → commit. Then for every feature: write its test and code →
`--local-changes --fast` → `quality_items.py --next` → fix that file → rerun →
… → READY_FOR_FULL → `git commit`. Because each step is committed, every fast
run measures only the current step. Before hand-off: the whole-repository
ship report (`quality_loop.py --root .`, no scope flag), which includes
Runs (smoke) and Gate scope.

## Sub-agent briefs

When items sit in several files, the parent fans out: `quality_items.py
--briefs 4` prints one brief per file. Each brief names the file the
sub-agent owns (plus its test file), lists its items with hints, and carries
the rules: edit only those files; no `--init`, no `.quality/`, no commit;
verify with that file's test; stop and report when a fix needs another file.
The parent edits shared modules first, waits for every sub-agent, then reruns
the fast run once — that report is the evidence. The same pattern builds a new
project in parallel: after the skeleton is green, the parent writes the shared
contracts and their tests, then one sub-agent per directory.

## Runs (smoke): the core user story must work

The ship report has a **Runs (smoke)** row. For an interactive application,
configure `smoke.story` in `.quality/quality-gate.json`. Its command drives the
real application from outside the process and writes a fresh JSON report. The
gate checks the command exit status, minimum probe count, every probe's `ok`
value, and browser page errors. A command that exits 0 still fails if any probe
fails. Unit tests or mocked network calls cannot stand in for this check.

```json
"smoke": {
  "enabled": true,
  "story": {
    "name": "whiteboard core user story",
    "command": ["python3", "ui_check.py", "http://127.0.0.1:3000", "{report_dir}"],
    "report": ".quality/ui-check/report.json",
    "format": "steps-json",
    "minimum_probes": 10,
    "fail_on_page_errors": true
  }
}
```

`{root}`, `{report}`, and `{report_dir}` are replaced with absolute paths.
The report has this generic shape:

```json
{"steps": [{"step": "draw rectangle", "ok": true}], "page_errors": []}
```

The probe command owns application startup and cleanup when the app is not
already running. Install the browser driver required by that script, such as
Playwright plus Chromium. Use the bundled `smoke_check.py` only for a simpler
load/readiness story, or `smoke.commands` for a CLI or library entry point:

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
override with `--fail-on-text`). **Exercise one write path**: `--click
SELECTOR` (repeatable) picks a tool, `--drag SELECTOR` press-drag-releases on
the working surface (both need Python Playwright) — a drag with the default
select tool usually writes nothing, so click a drawing tool first — and each
`--probe 'METHOD /path [json]'` issues a request that must not 5xx; after the drag and probes the server must still answer — an
application that crashes on its first save fails the smoke. It always stops
the process. For a CLI or
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
