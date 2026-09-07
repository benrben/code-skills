# Repository quality loop

Read this reference before running or updating the bundled quality loop.

## Entrypoint and configuration

Run from the target repository root, in the foreground, and read its exit code:

```bash
<skill-directory>/scripts/quality --root . --local-changes --fast
```

The POSIX launcher supports macOS and Linux on x86_64 and arm64. It uses an
existing Python 3.10+ or downloads pinned, checksum-verified `uv` and a managed
Python into the user cache. This bootstrap owns only quality tools. The gate
never installs or restores project dependencies. It applies offline/no-sync
settings to project commands, adds native no-restore flags to inferred Cargo,
Maven, Gradle, .NET, `npx`, and `uv` commands, and fingerprints manifests,
lockfiles, and common dependency restore markers around the baseline test.
Missing project dependencies therefore produce `BLOCKED` evidence; they do not
trigger a restore.

## First-use setup

`quality --root . --init` now performs setup in this order:

1. Inspect files and manifests without running project code or changing files.
2. Write the gate files and `.quality/project-profile.json`.
3. Run and print the first baseline report.
4. Ask at most three questions that cannot be answered from the repository.

Use `--non-interactive` in CI or with an agent. Pending questions print after
the baseline and can be answered without a terminal dialog:

```bash
quality --root . --setup --non-interactive
quality --root . --answer 'critical_user_stories=["publish a report"]' \
  --answer 'security_level="Internal"' --non-interactive
```

`--setup product`, `--setup runtime`, and `--setup risk` resume one question
group. Confirmed answers survive later scans and are not asked again.
`--show-profile` previews detection without writing anything. Language,
framework, test, API, database, infrastructure, CI, deployment, and
observability detection never becomes a question when the repository already
provides clear evidence.

The HTML report keeps repository certification separate from product,
security, deployment, operations, language-semantics, and full-profile
certification. It does not compute one combined score. Each metric says
whether its priority is `REQUIRED`, `RECOMMENDED`, or `OPTIONAL`, separately
from its availability: `MEASURED`, `CONFIRMED`, `NOT CONFIGURED`,
`UNSUPPORTED`, or `NEEDS CONTEXT`.

## Project-wide measurement catalog

The baseline includes these generic dimensions. Runtime and organization
metrics use confirmed values or configured adapters; the gate never invents
production evidence.

| Dimension | Metrics shown | What they do and why they are needed |
|---|---|---|
| Product behavior | core behaviors, acceptance pass rate, requirements coverage, functional coverage, escaped defect rate | Connects test evidence to what users were promised and shows defects that reached them. |
| Integration and compatibility | contract pass rate, endpoint coverage, external interaction success, compatibility matrix | Finds broken APIs, schemas, platforms, and external-system boundaries. |
| Performance | suite time, P50/P95/P99 latency, throughput, CPU/memory, startup time | Measures feedback speed, user delay, capacity, and cost pressure. It starts in measure-only mode until budgets exist. |
| Reliability and recovery | error rate, availability/SLO, MTTR, recovery success, flaky-test status | Shows whether the real system stays available and can recover predictably. |
| UI and accessibility | accessibility violations, task completion, user error rate, cross-platform UI pass rate | Finds barriers and workflows that users cannot complete safely. |
| Application security | secret findings, known vulnerabilities, risk level, SAST/DAST findings, remediation age | Selects risk-based controls and prevents known security defects from shipping. |
| Supply chain | lockfile coverage, license issues, SBOM/provenance, pinned CI tools | Makes dependencies, licenses, and build origin reproducible and reviewable. |
| Infrastructure | IaC validation, misconfigurations, container findings, drift | Finds invalid or unsafe deployment configuration before and after release. |
| Data safety | forward migration, rollback, backup restore, schema compatibility | Proves releases and recovery do not corrupt or strand data. |
| Observability | log/metric/trace coverage, health/readiness, alert tests | Makes failures visible, diagnosable, and actionable. |
| Delivery and operations | deployment frequency, lead time, change-fail rate, deployment rework, runbook coverage | Uses the five current DORA-style delivery signals plus response readiness. |
| Governance and ownership | ownership coverage, bus factor, public API docs, security/contribution/release policy | Finds unclear responsibility, concentrated knowledge, and missing maintenance rules. |
| Code and language semantics | test/branch/mutation results, cyclomatic/cognitive complexity, CRAP, duplication, cycles, fan-in/out, hotspots, maintainability index, NPath, cohesion, smell density | Combines behavior, structure, test strength, and change history. Unsupported semantic metrics stay visible until a language adapter exists. |

## Measurements and why they exist

| Measurement | What the gate checks | Why it is needed | Portable evidence and boundary |
|---|---|---|---|
| Formatter and lint | Every detected or configured check-mode formatter/linter exits cleanly and emits no configured violation output. | Keeps syntax, style, and language idioms consistent. | Uses an existing project command or an isolated pinned Python tool; N/A when no supported command exists. |
| Static types | Configured or inferred type checks report zero errors. | Finds invalid contracts and caller/callee mismatches before runtime. | Never restores dependencies; missing compilers or packages are explicit failures or blockers. |
| Contracts and schemas | OpenAPI, JSON Schema, and configured compatibility commands validate. | Prevents producers and consumers from silently disagreeing. | Built-in discovery plus isolated validators; domain compatibility still needs a project command. |
| Complete test result | The repository’s complete test command exits zero. | Proves the currently installed project can satisfy its behavior checks. | Inferred commands use offline/no-restore modes; absent runtimes or dependencies are BLOCKED. |
| Test execution count | Native unittest, pytest, Vitest, Jest, Go JSON, Cargo, Minitest, or TAP output proves how many tests ran, passed, failed, skipped, xfailed, or retried. | Stops an empty or miswired test command from passing by doing nothing. | Inferred Go uses `go test -json`; a zero exit without a supported summary is BLOCKED. Default: at least 1 executed and 0 skipped. |
| Line coverage | Covered executable lines per function. | Shows behavior that the tests never execute. | Reads Coverage.py, Istanbul/LCOV, Cobertura/JaCoCo, Go, or normalized reports; missing evidence stays unmeasured. |
| Branch coverage | Covered outcomes per function. | A line can run while one decision outcome remains untested. | Uses native branch data or a normalized adapter. Default: 100%. |
| Cyclomatic complexity | Independent control-flow paths per function. | High path count raises testing and change risk. | Precise Python AST plus supported native analyzers; default maximum 6. |
| CRAP | `complexity² × uncovered_fraction³ + complexity` per function. | Ranks risky combinations of complex and uncovered code. | Recalculated by the gate from measured coverage and complexity; default maximum 6. |
| Cognitive complexity | Human reasoning cost from nested/continued control flow. | Two functions with equal path count can differ greatly in readability. | Precise Python AST today; other syntax is marked UNSUPPORTED rather than guessed. Default maximum 15. |
| Maximum nesting | Deepest control-flow nesting in each function. | Deep nesting hides the happy path and multiplies reasoning context. | Precise Python AST today; other syntax is explicit UNSUPPORTED. Default maximum 4. |
| Duplicate source | Exact normalized contiguous blocks of at least six code lines, with both locations and union percentage. | Repeated behavior drifts and multiplies repair work. | Pure source scan for every text language. Default maximum 5% duplicated code. |
| File LOC | Physical lines in every selected production file. | Oversized modules usually mix responsibilities and slow review. | Pure file read for all source extensions. Default maximum 600. |
| Slow tests | Complete-suite duration and individual test durations when available. | Keeps the feedback loop fast enough to run continuously. | Suite time always measured; per-test timing requires native or JUnit/normalized evidence. |
| Failure paths | Covered handler entries and high-confidence silent handlers. | Recovery code often fails exactly when it is needed. | Python, brace-style catch, and Go patterns; coverage kind is recorded. |
| Test integrity | Same-package mocks and null render surfaces in composed-root tests. | Prevents tests from replacing the subsystem they claim to prove. | Conservative structural rules; N/A without configured root-test patterns. |
| Smoke story | Real entry point or outside-process user-story probes all pass, with no page errors. | Unit tests cannot prove startup, wiring, persistence, or a real composed flow. | Requires a runnable installed project; missing runtime/dependencies are never restored. |
| Dead code | High-confidence configured detector reports zero findings. | Removes misleading paths and unused ownership. | Isolated Vulture for configured Python or an existing native project detector. |
| Flakiness | Repeated complete suites produce consistent results. | Nondeterminism makes every other metric unreliable. | OFF unless requested; project dependency restore remains disabled. |
| Mutation | Tests kill deliberately wrong operator changes. | Shows that tests would notice incorrect behavior, not only execute lines. | OFF unless requested; built-in fallback or an already installed native runner. |
| Dependency cycles | Resolved internal imports contain zero strongly connected components. | Cycles make change order, initialization, and ownership fragile. | Pure import resolution and iterative graph analysis. |
| Fan-in and fan-out | Incoming and outgoing internal edges per source module. | Highlights unstable hubs and modules with too many responsibilities. | Informational counts from resolved internal imports. |
| Module boundaries | Imports obey repository-owned allow/deny directions and every source has one owner. | Enforces intended architecture rather than current accidental structure. | NEEDS CONTEXT until generated rules are reviewed; custom edge adapters cover unsupported syntax. |
| High-confidence secrets | Known private-key and provider-token formats by kind and location. | Committed credentials create immediate security and rotation risk. | Pure source scan; matched values are never stored in terminal, JSON, or HTML. Default zero. |
| Known vulnerabilities | Locked package names and versions have zero OSV advisories. | Known vulnerable dependencies expose already documented attack paths. | Parses common lockfiles and queries OSV directly without installing packages; offline/network failure is BLOCKED and unsupported lockfiles are explicit. |
| Git hotspots | Commit frequency and churn combined with measured cognitive complexity. | Frequently changed complex files deserve earlier review and refactoring. | Read-only Git history; N/A without history and unsupported complexity stays visible. |
| Extensibility | Configured extension scenarios pass and stable core does not import replaceable extensions. | Keeps plug-in points replaceable without coupling policy to implementations. | NEEDS CONTEXT or N/A until the project declares its extension contract. |
| Gate scope | Production files are not hidden by custom excludes. | Prevents a green report from omitting difficult code. | Pure path analysis; only standard tests and narrow tooling files may be excluded. |

Every row reports `PASS`, `FAIL`, `BLOCKED`, `UNSUPPORTED`, `NEEDS CONTEXT`,
`N/A`, `OFF`, `SKIPPED`, or `DEFERRED`. Only PASS contributes to
certification. The explicit states are the portable answer when a machine lacks
a runtime, a dependency cache, a parser, history, or repository-specific intent.

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

Updates also refresh a repository's `.quality/toolchain.json` code-skills pin
and its clean checkout when present. Existing HTML is rendered again from saved
measurements without changing their timestamp or pass/fail results. Reports made
before render snapshots were supported need one quality-loop run first. An active
run blocks the repository refresh; retry after it finishes. `--update-all` performs
this for the repositories containing the registered skill installations.

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
| `--fast` | lint, types, contracts, tests, line/branch coverage, slow tests, extensibility, error handling, test integrity, CRAP, file size, dead code; defers smoke, Gherkin, flaky and mutation | first look after a change |
| `--lint` · `--types` · `--contracts` | that one check | after an edit, a signature change, a schema change |
| `--tests` | the complete test suite plus a native execution-count summary | after writing a test |
| `--coverage` | tests plus per-function coverage; lists every uncovered function in scope | deciding which test to write next |
| `--branches` | tests plus per-function branch coverage | after adding conditionals or exception branches |
| `--slow-tests` | complete-suite duration plus individual timings when the runner emits them | when feedback is becoming slow |
| `--extension-contracts` · `--extension-deps` | configured extension scenarios; forbidden core-to-extension imports | after adding a plug-in point or extension |
| `--failure-paths` · `--silent-errors` | error-handler coverage; empty or `pass` handlers | after changing recovery or fallback behavior |
| `--test-integrity` | same-package production mocks and null render surfaces in composed-root tests | after changing an application/root composition test |
| `--complexity` | static complexity per function, no tests | after a refactor |
| `--crap` | tests, coverage and complexity, CRAP per function | before a commit |
| `--loc` · `--dead-code` · `--deps` | file size, unused code, module boundaries | before handoff, after adding imports |
| `--test-count` · `--cognitive` · `--duplication` | native test totals; cognitive/nesting; exact repeated blocks | after changing tests or control flow |
| `--cycles` · `--secrets` · `--vulnerabilities` · `--hotspots` | dependency graph health; credential formats; OSV advisories; Git change risk | for portable architecture and security review |
| `--smoke` | runs the outside-process core story and parses every probe (`smoke.story`), or runs a CLI/library entry point | after changing the core workflow or startup |
| `--gherkin` | every Gherkin scenario, Examples row, step and hook | after implementing an acceptance workflow |
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
2. One line per gate: `[PASS|FAIL|BLOCKED|UNSUPPORTED|NEEDS CONTEXT|OFF|SKIPPED|DEFERRED|N/A] title: summary`,
   and for a failing gate up to eight lines naming the exact items
   (`path:line name: coverage …, complexity …`). `[N/A]` means an optional
   check is not configured; the line says so and it is not a to-do item.
3. `Coverage today: N of M functions fully covered (x%) · k not covered — most in: …`
   — printed in every mode; when a run measures nothing it repeats the last
   measurement with its timestamp. Use it to plan tests, not to discover gaps
   at the end.
4. `Health overview` in the HTML shows the minimum line, branch, and failure-path
   coverage, plus aligned P50/P75/P95/maximum bullet charts for CRAP,
   complexity, function LOC, file LOC, and test duration. Each metric uses its
   own linear scale with a visible configured-limit marker. Percentiles use
   linear interpolation between sorted observations. The maximum row prevents
   an outlier above P95 from hiding behind a passing percentile. Missing
   measurements are explicit; function LOC is informational because it has no
   configured gate. The project evidence summary shows Required, Recommended,
   and Optional priority independently from whether each source is connected,
   needs setup, or is unavailable. It says when no additional setup is required
   to pass and keeps dimensions that do not apply in one group. `View run details` also shows
   branch coverage, the slowest test,
   extension-contract results, core-to-extension dependencies, failure-path
   coverage, and silent handlers. Gate detail tables also show every structured
   smoke probe, catch evidence type, test-integrity violation, native test
   total, cognitive/nesting measurement, duplicate block, dependency fan
   count/cycle, secret location, OSV finding, and Git hotspot. The
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

- Exit `0` with `QUALITY_LOOP=PASS`: the full ship report passed.
  A successful partial run exits `0` with `QUALITY_LOOP=SELECTED_PASS`;
  it cannot authorize handoff.
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
while a run is in flight has produced nothing. Coverage and CRAP measurements
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

## Gherkin acceptance

Author acceptance tests in human-readable **YAML**, using `.feature.yaml` or
`.feature.yml`. Keep steps ordered so a scenario can contain repeated actions
and assertions. For example, `features/todos.feature.yaml`:

```yaml
feature: Todo list
scenarios:
  - name: Add a task
    steps:
      - given: an empty todo list
      - when: I add "Buy milk"
      - then: I see "Buy milk" in the list
      - and: the active count is 1

  - name: Save different tasks
    steps:
      - given: an empty todo list
      - when: I add "<task>"
      - then: I see "<task>" after reloading
    examples:
      - task: Buy milk
      - task: Call Sam
```

Required keys are `feature` and a nonempty `scenarios` list. Each scenario has
`name` and nonempty `steps`; each step contains one of `given`, `when`, `then`,
`and`, or `but` with a text value. Feature-level `description`, `tags`, and
`background` (ordered steps) are optional; scenarios may add `tags` and
`examples`. Quote tags such as `"@smoke"` and numeric/boolean-looking example
values such as `"0"` or `"yes"`. Example rows use matching column names. Unknown
keys, duplicate keys, empty steps, and invalid values fail with an error.

The gate generates a sibling `.feature` file before invoking Behave/Cucumber,
then checks native results against the YAML specification. Edit and commit the
YAML; add its generated sibling to `.gitignore`. Generated files carry a source
marker. The compiler refuses to overwrite an authored `.feature` file; migrate
or rename that file first. Existing native `.feature` suites remain supported.
For a direct runner invocation, prepare files with
`scripts/gherkin_yaml.py --root . --feature features/todos.feature.yaml` first;
`scripts/quality --gherkin` does this automatically.

Bind every requested workflow and important failure to real behavior and
observable assertions. UI scenarios use a browser, capture page errors, and
fail on them. Include a successful write and read-back after reload. Review
assertions: a report cannot detect a step that does nothing.

Configure a native runner and its JSON formatter, for example:

```json
"gherkin": {
  "enabled": true,
  "command": [".venv/bin/python", "-m", "behave", "tests/features", "--format", "json", "--outfile", "{report}"],
  "report": ".quality/gherkin.json",
  "format": "behave-json",
  "timeout_seconds": 300
}
```

For Cucumber use `format: "cucumber-json"` and the runner's legacy JSON formatter
(e.g. `cucumber-js --format json:{report}`), not Cucumber Messages NDJSON.
Install the project's runner separately before checks. The quality-tool cache
can provision the pinned official Gherkin parser and PyYAML; it never installs project
dependencies. Custom runners need a native supported report, not invented totals.

The gate discovers YAML scenarios and native `.feature` suites outside vendor
folders, counts each YAML/generated pair once, and uses the official Gherkin
parser to enumerate scenarios and Examples rows. It validates the fresh native
JSON report against that inventory; JSON is the runner evidence format.
Require at least one scenario and step, runner exit `0`, complete matching
execution, and every step/hook passed. Missing, extra, duplicate, skipped,
undefined, pending, ambiguous, and failed results fail. Tag filters must not omit
required scenarios. Changed YAML, changed generated features, or an unchanged report fail.

Run `--gherkin` for focused feedback. Fast mode defers acceptance execution;
the full ship report requires it and cannot mark it OFF or N/A. Existing projects
must add acceptance configuration before their next full certification.

See the [Gherkin reference](https://cucumber.io/docs/gherkin/reference/) and
[Behave JSON formatter](https://behave.readthedocs.io/en/stable/appendix.formatters/).

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

Before handoff use whole-repository scope for a new project or audit,
`--local-changes` for existing work, or `--commit REF` for a requested commit.
Omit all check flags. A green ship report means every applicable formatter,
lint, type, contract, test, test-count, coverage, cyclomatic/cognitive
complexity, nesting, CRAP, duplication, smoke, Gherkin acceptance, file-LOC, dead-code,
dependency-cycle/coupling, module-boundary, secret, OSV, hotspot, and gate-scope
goal passed, plus flaky and mutation goals when they are enabled or requested. Red means not finished: fix
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
