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
settings to project commands, uses offline or no-restore modes for inferred
Cargo, Maven, Gradle, and .NET commands, and fingerprints manifests, lockfiles,
and common dependency restore markers around the test run. Missing project
dependencies therefore produce `BLOCKED` evidence; they do not trigger a
restore.

In a Claude Code plugin, the skill directory is
`${CLAUDE_PLUGIN_ROOT}/skills/code-discipline`; in Codex, use the directory of
the loaded `SKILL.md`.

`.quality/quality-gate.json` owns project gate choices, commands, and adapters.
Its `gates` object maps the ten gate keys (`shortcuts`, `secrets`, `file_loc`,
`project_checks`, `tests`, `coverage`, `complexity`, `duplication`,
`dead_code`, `vulnerabilities`) to booleans; see the
[setup guide](project-setup-chat.md#choose-gates-one-topic-at-a-time).
Explicit `true` enables a gate and requires its evidence; `false` prevents its
execution and reports OFF, even if a check flag selects it. Omitted keys retain
existing behavior. This policy does not turn a full run into a selected run.
All gates OFF cannot certify.

Older configurations keep working. Legacy keys map to the new gates:
`quality` → `tests`, `coverage`, and `complexity`; `test_execution` → `tests`;
`format_lint` and `types` → `project_checks`; `scope` → `shortcuts`;
`error_handling` → `shortcuts` and `coverage`; `cognitive` → `complexity`.
A new key set explicitly wins over a legacy key. Keys of removed gates are
ignored, and the report says so (`ignored removed gate X`).

The same file holds the optional adapters:

- `test.command`: the complete test command, when inference picks the wrong one.
- `test.junit`: JUnit XML globs to read when the runner prints no summary.
- `project_checks.commands`: the lint, type, format, or build commands; they
  replace inference.
- `coverage.command`, `coverage.report`, `coverage.format`: a coverage adapter
  that always wins over the built-in recipes.

`.quality/quality-thresholds.json` owns every numeric goal and overrides the
bundled defaults in `../quality-thresholds.json` (schema version 3), including
`file_loc.max_lines: 600`. Never copy thresholds into
`.quality/quality-gate.json`. A schema version 2 file is migrated in memory:
coverage, complexity, CRAP, cognitive, nesting, file-size, and test-count goals
move to their new sections and everything else is dropped. Only `--init` and
the update flow write the migrated file back.

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

## First-use setup

An installation request includes the guided setup in this conversation; do not
stop after installation to ask permission to start it. Read the installed guide
directly if automatic skill discovery needs a restart.

For agent-led first-time setup, follow [guided project setup](project-setup-chat.md).
Discuss gates and limits in chat, then initialize configuration without running
checks using `python3 <skill-directory>/scripts/quality_loop.py --root . --init`.
`--init` asks no questions. It writes `.quality/quality-gate.json` and
`.quality/quality-thresholds.json` from what it detects and refuses to
overwrite existing files. Save accepted choices before running the first
baseline. All settings belong to this project's `.quality/` files; reuse them
on later tasks.

## The ten gates

Every gate is generic: it works for any language without project setup and
uses only the repository's own commands or pinned tools the skill installs into
its own cache. In `--local-changes` or `--commit` scope each gate judges the
agent's change (changed lines, functions, and files); in whole-repository scope
it judges everything. Gates run, and appear in the report, in this order.

| Gate | What it checks in the change | Why it matters | Tools | Limits |
|---|---|---|---|---|
| No shortcuts (`shortcuts`) | New suppression, ignore, skip, or focus markers on added lines of any text file (`# noqa`, `# type: ignore`, `@ts-ignore`, `eslint-disable`, `pragma: no cover`, `@pytest.mark.skip`, `it.only`, `t.Skip`, `#[ignore]`, `XCTSkip`, and similar); new silent error handlers in production code (`except: pass`, empty `catch {}`, `.catch(() => {})`, empty `if err != nil {}`, `rescue … end`); production files hidden by `source.exclude`; edits to `.quality/quality-gate.json` or `.quality/quality-thresholds.json`. | An agent under pressure can make a red check green by silencing it, skipping the test, swallowing the error, hiding the file, or loosening the policy. This gate makes those moves visible failures. | None. | A fixed marker list and common handler shapes, not a parser. Changed check settings (`tsconfig*.json`, ESLint, Ruff, mypy, `pyproject.toml`, `package.json`, and similar) are listed for review but never fail. |
| Secrets (`secrets`) | Known private-key and provider-token formats (AWS, GitHub, GitLab, Slack, Stripe, Anthropic, OpenAI, Google, npm, PyPI, Hugging Face, SendGrid, Twilio) in every text file, including `.env`, YAML, JSON, tests, and docs. In incremental scope every line of a changed file is scanned. | A committed credential needs rotation the moment it lands. | None. | Only high-confidence formats; generic passwords are not guessed. Matched values are never stored in terminal, JSON, or HTML output. |
| File size (`file_loc`) | Physical lines per production file. Incremental scope fails a file that crosses the limit, a new file over it, or a file already over it that grew. | Agents tend to append to one file until it mixes responsibilities and becomes hard to review. | None. | Counts lines, not responsibilities. Files already over the limit pass until they grow. |
| Project checks (`project_checks`) | Every lint, type, format, and build command the repository declares exits cleanly: `package.json` scripts such as `lint`, `typecheck`, `format:check`, and `build` (through the repo's package manager), local `tsc`, Ruff and mypy when configured and installed in the project environment, `go vet` and `gofmt -l`, `cargo check` and `cargo fmt --check`, `dotnet build`, `swift build`, and Makefile `lint`/`typecheck`/`check` targets. | Keeps the repository's own rules binding on the agent's change. | The repository's own. | Never installs a checker. N/A when nothing is declared. A missing executable is BLOCKED. A command that cannot fail or that rewrites files (`--fix`, `--write`, `\|\| true`) is reported as a finding. |
| Tests (`tests`) | The complete test command exits zero, and its output proves how many tests ran, passed, failed, and were skipped or filtered. Fails on any failure, skip, deselection, or `todo`, on fewer than the minimum executed, and when the executed count drops below the last full run. | Stops an empty, miswired, or quietly trimmed suite from passing by doing nothing. | The repository's own runner. | Reads pytest, unittest, Jest, Vitest, Mocha, node:test, Go, Cargo, RSpec, Minitest, PHPUnit, Maven, dotnet, Swift, Dart, Elixir, CTest, Meson, and TAP summaries, or JUnit XML. A zero exit without a recognizable summary is BLOCKED. A test count drop passes only with `--allow-test-removal` after the user approves. |
| Coverage (`coverage`) | Line and branch coverage of the changed executable lines, from the same single test run. Uncovered lines are grouped by function; a changed production file the tests never load counts as fully uncovered. | An agent can add code, including error and fallback paths, that no test ever runs. | The test runner plus pinned coverage tools in the skill cache (see below). | Branch data depends on the runtime; when it is missing the report says so. Without a recipe or adapter the gate is BLOCKED, never PASS. |
| Complexity (`complexity`) | Cyclomatic complexity, cognitive complexity, nesting depth, and CRAP (`complexity² × (1 − coverage)³ + complexity`) for each function the change touches. | Long chains of conditions are hard to test and easy to break; CRAP ranks complex code that is also untested. | Pinned Lizard 1.26.0. | Covers the languages Lizard reads; other files are listed as not analyzed. `lizard forgive` comments are ignored. Nesting is the deepest level of the cognitive-complexity counter: control structures, `catch`/`except`, ternaries, and lambdas add a level; a `try` body and a `with` block do not. For Ruby, Lua, Erlang, Fortran, PL/SQL, and Structured Text, nesting and its cognitive increments come from the indentation of lines that start with a control keyword. CRAP needs coverage for the function. |
| Duplication (`duplication`) | Copies of at least 50 tokens and 5 lines where one side touches an added line, searched against all production code. Renamed variables, changed literals, and inserted blank lines or comments still match. | Agents often copy an existing function instead of reusing it, and copies drift apart. | Pinned Pygments 2.21.0 lexers. | Finds copy-and-rename clones, not rewritten logic with the same behavior. Import and include blocks are ignored. Files without a lexer are skipped. |
| Dead code (`dead_code`) | Functions the change adds that nothing references, and functions whose last caller the change removed. | Agents leave helpers behind after a refactor or write code that is never wired in. | Pinned Lizard 1.26.0 (the function list complexity already built). | A name-based search over production files; a call from a test does not count, so a function only tests call is reported as "only tests call it". Functions with decorators or attributes, methods, and exported or public names are listed for review instead of failing; a new private or new top-level Python or Ruby function with zero references fails. |
| Vulnerable dependencies (`vulnerabilities`) | Locked package versions have zero OSV advisories. Incremental scope runs only when a manifest or lockfile changed and checks only new or changed packages. | A newly added or upgraded dependency can carry a known attack path. | None installed; queries OSV over the network. | A manifest with dependencies but no lockfile fails ("add a lockfile"). Unsupported lockfile formats, ecosystems without a readable lockfile (such as Maven), and offline or network failures are BLOCKED. N/A only when the repository has no dependency manifest. Deferred in `--fast`. |

### Coverage recipes

The tests gate runs the test command once, wrapped by the coverage recipe for
its language, so tests and coverage come from the same execution:

| Language | How coverage is collected | Needs |
|---|---|---|
| Python | `coverage run --branch` around the project's own command | Pinned coverage.py from the skill cache |
| JavaScript / TypeScript | Jest `--coverage`, Vitest coverage when its provider is installed, otherwise Node's built-in V8 coverage converted by c8 | Pinned c8 10.1.3 in the skill cache for the V8 path |
| Go | `-coverprofile` added to `go test` | Nothing extra |
| Rust | `cargo llvm-cov` replaces `cargo test` | cargo-llvm-cov in the skill cache |
| Java / Kotlin | JaCoCo agent attached to every JVM the build starts | Pinned, checksum-verified JaCoCo 0.8.12 jars in the skill cache |
| .NET | `--collect:"XPlat Code Coverage"` | `coverlet.collector` in the test projects |
| Ruby | Ruby's built-in `Coverage` module, loaded through `RUBYOPT` | Nothing extra |
| Swift | `--enable-code-coverage`, read with llvm-cov | Nothing extra |
| PHP | PHPUnit `--coverage-cobertura` | The pcov or Xdebug PHP extension; otherwise BLOCKED |
| C / C++ (CMake, Meson) | `--coverage` build in a build directory of the run's own, read with gcov or `llvm-cov gcov` | CMake or Meson, a C/C++ compiler, and its gcov; otherwise BLOCKED |
| Others | No built-in recipe | Configure `coverage.command`, `coverage.report`, and `coverage.format`; until then BLOCKED |

A configured adapter always wins. Supported report formats are coverage.py
JSON, LCOV, Cobertura, JaCoCo, Go cover profiles, llvm-cov JSON, Ruby
coverage JSON, and gcov text.

### Statuses

Every gate reports `PASS`, `FAIL`, `BLOCKED`, `N/A`, `OFF`, `SKIPPED`, or
`DEFERRED`. Only PASS contributes to certification. `N/A` counts as passing
only for project checks (nothing declared) and vulnerable dependencies (no
manifest). Any other gate that cannot measure is BLOCKED and names the missing
prerequisite. The explicit states are the portable answer when a machine lacks
a runtime, a dependency cache, a tool, or a network connection.

## Scope

- `--local-changes` selects staged, unstaged, and untracked files.
- `--commit [REF]` selects a committed diff; omitted `REF` means `HEAD`.
- Omitting both selects the whole repository.

Incremental scope limits every gate to the change: added lines for shortcuts,
coverage, and duplication; touched functions for complexity; added or orphaned
functions for dead code; changed files for secrets and file size; changed
manifests and lockfiles for vulnerabilities. The test suite and project checks
still run the repository's complete commands. Never describe an incremental
pass as whole-repository certification.

## Choosing a check

| Flags | Runs | Use it when |
|---|---|---|
| `--fast` | every gate except vulnerable dependencies, which needs the network and is deferred | first look after a change |
| `--shortcuts` (`--scope`, `--silent-errors`) | suppression and skip markers, silent handlers, hidden files, policy edits | after silencing a warning or changing error handling |
| `--checks` (`--lint`, `--types`) | the repository's declared lint, type, format, and build commands | after an edit or a signature change |
| `--tests` (`--test-count`) | the complete test suite plus its execution counts | after writing a test |
| `--coverage` (`--branches`, `--failure-paths`) | tests plus line and branch coverage of the changed lines | deciding which test to write next |
| `--complexity` (`--crap`, `--cognitive`) | tests, coverage, and per-function complexity, nesting, and CRAP | after a refactor, before a commit |
| `--loc` · `--duplication` · `--dead-code` | file size; copied code; unused functions | before handoff, after moving code |
| `--secrets` · `--vulnerabilities` | credential formats; OSV advisories | after touching config or dependencies |
| *(no check flags)* | **the ship report**: every enabled gate | before handoff, until it is green |

Check flags combine (`--coverage --checks`). Selecting coverage or complexity
also runs the tests. A run with check flags is a *partial* run: it exits 0 when
every selected check passes, and it never certifies. Runs with no check flags
are full runs; `--fast` is a full run that defers vulnerable dependencies and
therefore never exits 0.

Two flags record the user's explicit approval and are never chosen by the agent
alone:

- `--allow-policy-change`: the user approved this change to
  `.quality/quality-gate.json` or `.quality/quality-thresholds.json`.
- `--allow-test-removal`: the user approved removing tests, so a lower test
  count passes.

## The report

Every mode prints the same report to the terminal (the HTML file is for
people):

1. `QUALITY REPORT · mode · scope` — one line.
2. One line per gate, in gate order:
   `[PASS|FAIL|BLOCKED|N/A|OFF|SKIPPED|DEFERRED] title: summary`, and for a
   failing gate up to eight lines naming the exact items (`path:line name:
   metric value, limit`). `[N/A]` means the gate has nothing to measure in
   this repository; the line says so and it is not a to-do item.
3. `Coverage today:` — printed in every mode; when a run measures nothing it
   repeats the last measurement with its timestamp. Use it to plan tests, not
   to discover gaps at the end.
4. The HTML report shows each gate with its items and measurements: test
   counts, line and branch coverage, the riskiest functions by complexity and
   CRAP with P50/P95/maximum values against the configured limits, clone
   locations, secret kinds and locations, and OSV findings. Missing
   measurements are explicit. The responsible file, function, or package
   remains visible.
5. `Since last run: fixed n · remaining m · new k` — compared with the
   previous state file, so a cycle's effect is visible without reading twice.
6. `To fix:` up to twelve items from failing gates. More than twelve: the list
   is grouped by file with counts, most items first, repository-level items
   last, followed by `Next file:` — the `quality_items.py --next` command that
   prints the first file's items.
7. The next step: the exact command to rerun; when the fast run is green,
   "commit this step, then continue" plus the ship-report command; or — when
   the ship report is green — `HAND OFF NOW`: the next message is the
   hand-off; nothing is added after green.
8. `QUALITY_LOOP=PASS|FAIL|READY_FOR_FULL|ERROR`, `ITEMS_TO_FIX=n`, `STATE=`,
   `HTML=`.

Any gate can be saved as OFF in the project's `gates` object; re-enable it
there before selecting it with a check flag. Nothing is skipped silently:
skipped, deferred, and off gates are listed.

## Results and the repair loop

- Exit `0` with `QUALITY_LOOP=PASS`: the full ship report passed.
  A successful partial run exits `0` with `QUALITY_LOOP=SELECTED_PASS`;
  it cannot authorize handoff.
- Exit `1`: read the `To fix` list, run `quality_items.py --next` for the
  items of the first file, fix that file, and rerun the same command. Never
  open the state file yourself; the items script reads it. Verify a fix with
  that file's own test only; the suite, coverage, and project checks all run
  in the next fast run — one turn instead of four. In fast mode exit 1 may
  instead mean READY_FOR_FULL: commit the step (the report prints the exact
  one-line command) and continue. A fast run whose local-changes scope is
  empty (everything committed) measures nothing and says so; it never prints
  READY_FOR_FULL.
- Exit `2`: configuration, an adapter, or the runner failed; repair that
  blocker first — it is an item to fix, not a reason to stop.

Fix a shortcuts finding at its cause: remove the marker and fix the warning or
test; restore the source settings; raise the error or handle it visibly. A
policy edit or a test removal needs the user's explicit approval first; then
rerun with `--allow-policy-change` or `--allow-test-removal`.

Run the loop in the foreground and read the exit code in the same command.
Never run it in the background or wait for a notification: a session that ends
while a run is in flight has produced nothing. Coverage and CRAP numbers from a
failing test run are diagnostic only.

Before the first run, preserve existing worktree changes. Only one loop may run
per repository; coverage tools often share temporary paths.

## quality_items.py: what to fix next

Reads the state file the loop wrote and prints only what the next cycle needs:

| Command | Prints |
|---|---|
| `quality_items.py --root . --next` | the first file with open items: every item with line, metric, value, and limit |
| `quality_items.py --root . --file PATH` | the same for one file (full path or file name) |
| `quality_items.py --root . --summary` | files and counts only |
| `quality_items.py --root . --briefs N` | one ready-to-send sub-agent brief per file for the N files with the most items |

`--state PATH` selects another state file. Exit 2 means the loop has not run
yet.

## The per-step cycle

New project: skeleton (manifest, test runner, one passing test of one real
function) → guided setup and configuration-only initialization
→ saved project policy → `--local-changes --fast` green with 0
items → commit. Then for every feature: write its test and code →
`--local-changes --fast` → `quality_items.py --next` → fix that file → rerun →
… → READY_FOR_FULL → `git commit`. Because each step is committed, every fast
run measures only the current step. Before hand-off: the whole-repository
ship report (`quality_loop.py --root .`, no scope flag).

## Sub-agent briefs

When items sit in several files, the parent fans out: `quality_items.py
--briefs 4` prints one brief per file. Each brief names the file the
sub-agent owns (plus its test file), lists its items, and carries
the rules: edit only those files; no `--init`, no `.quality/`, no commit;
verify with that file's test; stop and report when a fix needs another file.
The parent edits shared modules first, waits for every sub-agent, then reruns
the fast run once — that report is the evidence. The same pattern builds a new
project in parallel: after the skeleton is green, the parent writes the shared
contracts and their tests, then one sub-agent per directory.

## The ship report

Before handoff use whole-repository scope for a new project or audit,
`--local-changes` for existing work, or `--commit REF` for a requested commit.
Omit all check flags. A green ship report means every enabled, applicable
gate passed: no shortcuts, no secrets, file size, project checks, tests,
coverage, complexity, duplication, dead code, and vulnerable dependencies. Red
means not finished: fix every listed item and rerun until it exits `0`. Never
lower thresholds or disable a gate merely to obtain green. The user can
explicitly revise project policy; save their choice, rerun with
`--allow-policy-change`, and make the reduced evidence clear. Never skip
tests, weaken assertions, add suppressions, broaden allow-lists, exclude
production files, or replace checks with no-ops. If a gate cannot measure valid
code, repair the adapter or configuration — never rewrite correct code merely
to manufacture a measurable target. When the report prints `HAND OFF NOW`, the
next message is the hand-off: the deliverables list and the report's status
line. Nothing is added after green — not a linter, not a formatter, not another
check. Report the disabled gates with the status; PASS covers only the chosen
project policy.
