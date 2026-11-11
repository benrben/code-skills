# Repository quality environment setup

Installing code-discipline for a project includes this setup in the same chat.
Read [guided setup](project-setup-chat.md) before installing. After installation,
start its first question directly; do not ask whether to begin setup or hand off
after copying files. Preserve current policy as the starting recommendation.

Use this guide when the user asks to prepare, bootstrap, or harden a repository
for the complete quality loop. Preserve the repository's existing runtime,
package manager, lockfile, commands, and CI conventions. Quality tools belong
to the skill cache. The gate never adds project development dependencies,
restores dependency trees, rewrites manifests or lockfiles, or runs an install
script. An existing native project tool may be used read-only when already
available.

## Bootstrap

1. Inspect manifests, lockfiles, existing scripts, formatter/linter/type/test
   configuration, and coverage output. Record existing worktree changes before
   editing.
2. Inspect whether the repository's runtime and dependencies are already
   available. Do not restore them. A missing runtime, incomplete offline cache,
   or absent dependency produces `BLOCKED` evidence with the missing
   prerequisite. Ask the repository owner to prepare dependencies outside the
   quality run if they want those project-native checks to execute.
3. For first-time agent setup, use the [guided chat workflow](project-setup-chat.md).
   Explain gates and limits, collect choices, and create configuration without
   executing checks:

   ```bash
   python3 <skill-directory>/scripts/quality_loop.py --root . --init
   ```

   `<skill-directory>/scripts/quality --root . --init` does the same through the
   launcher. `--init` writes `.quality/quality-gate.json` for gate choices,
   commands, and adapters and `.quality/quality-thresholds.json` for every
   numeric limit, creates a git repository when none exists, and exits. It runs
   no checks, asks no questions, and never overwrites an existing file.

   Save each gate's ON/OFF choice in `quality-gate.json` under `gates` and numeric
   limits in `quality-thresholds.json`. Preserve these project choices on later
   runs. Do not edit bundled defaults or other projects. An enabled gate needs
   real evidence; missing tooling must not silently disable it.

   A thresholds file from an older skill version (`schema_version` 2) is
   migrated: `metrics.coverage_limit` and `metrics.branch_coverage_limit`
   become the `coverage` limits, `metrics.complexity_limit`,
   `metrics.crap_limit`, `portable_analysis.max_cognitive_complexity`, and
   `portable_analysis.max_nesting_depth` become the `complexity` limits,
   `file_loc.max_lines` is kept, and `test_execution.min_executed` and
   `max_skipped` move to `tests`. Other old limits are dropped. The runner
   migrates in memory and writes the file back only in flows that already
   write thresholds, such as `--init`.
4. Configure non-mutating, non-installing check commands as JSON argument arrays. Use
   `["bash", "-lc", "..."]` only when a check truly requires pipes, globbing,
   command substitution, or another shell feature.
5. Run `--fast` until every executed check is green, then run without `--fast`.
   `--fast` defers only the vulnerability check, which needs network access, so
   a fast run never certifies. Put that exact full command in CI. Never make CI
   and local development use different thresholds.

## How the gates judge a change

The ten gates are generic: they work for any language without project setup,
and they use only what the repository already has or pinned tools the skill
installs into its own cache. With `--local-changes` or `--commit REF` they judge
the agent's change: the lines it added or edited, the functions containing
them, the files it grew, and the dependency versions it introduced. Without
those flags every file counts. They run in this order: no shortcuts, secrets,
file size, project checks, tests, coverage, complexity, duplication, dead code,
and vulnerable dependencies.

A gate that cannot measure reports `BLOCKED` with the missing prerequisite,
never PASS. Only project checks (no declared commands) and vulnerable
dependencies (no dependency manifest) may be `NOT APPLICABLE`; every other
gate must measure.

## Configure each gate

### 1. No shortcuts

This gate catches ways an agent can make the other checks pass without doing
the work. It needs no tools. It fails on:

- New suppression, skip, or focus markers on added lines of any changed text
  file, including tests and configuration. Examples are `# type: ignore`,
  `# noqa`, `@ts-ignore`, `eslint-disable`, `//nolint`, `#[allow(`,
  `@SuppressWarnings`, `rubocop:disable`, `pragma: no cover`,
  `istanbul ignore`, `@pytest.mark.skip`, `.skip(`, `.only(`, `xit(`,
  `t.Skip(`, `#[ignore]`, `@Disabled`, and `XCTSkip`.
- New error handlers in production files that silently swallow a failure: a
  Python `except` whose body is only `pass`, `...`, `continue`, or `return`; an
  empty `catch` or one that only returns `null`, `nil`, or `undefined`; a
  JavaScript `.catch(() => {})`; an empty Go `if err != nil {}` or `_ = err`;
  and a Ruby `rescue` followed directly by `end` or `nil`.
- Production files hidden from the checks by source settings.
- A change to `.quality/quality-gate.json` or `.quality/quality-thresholds.json`.
  Policy belongs to the user. After the user confirms the change, rerun with
  `--allow-policy-change`. Generated `.quality/` files are never findings.

A changed check configuration, such as `tsconfig.json`, an ESLint or Ruff
config, `pyproject.toml`, or `package.json`, is listed for review but does not
fail: confirm the change does not weaken checks. The fix for a finding is to
remove the marker and fix its cause, raise or visibly handle the error, or
restore the source settings.

Limits: markers are matched as text, so a disguised suppression is not caught,
and a marker that existed before the change is not reported in change scope.

### 2. Secrets

This gate stops a credential from reaching history. It needs no tools. It
scans every line of every changed text file, including `.env`, YAML, JSON,
TOML, tests, and docs, so a secret already present in a touched file still
fails. It recognizes private key blocks and the token formats of AWS, GitHub,
GitLab, Slack, Stripe, Anthropic, OpenAI, Google API keys, npm, PyPI, Hugging
Face, SendGrid, and Twilio. Reports name the kind and line, never the value.
Remove the secret, rotate it, and load it from the environment instead.

Limits: only these known formats are detected. Generic passwords and other
high-entropy strings are not.

### 3. File size

File size requires no external tool. The runner counts physical lines in every
selected production source file, including code, comments, and blank lines.
`file_loc.max_lines` in `.quality/quality-thresholds.json` defaults to 600. In
change scope a file fails when the change pushes it over the limit, a new file
starts over it, or a file that is already over it grows. In whole-repository
scope every production file over the limit fails. A failure is a design prompt
to split cohesive responsibilities while preserving behavior; never minify
code, remove useful documentation, or exclude a file to evade it.

### 4. Project checks

This gate runs the lint, type, format, and build commands the repository
already declares, so an agent cannot finish with code the project's own tools
reject. It uses only the repository's tools and never installs any:

- `package.json` scripts named `lint`, `typecheck`, `type-check`, `types`,
  `check-types`, `tsc`, `format:check`, `check-format`, `fmt:check`,
  `prettier:check`, `check`, or `build`, run through the repository's package
  manager. A TypeScript project without a type script also runs the local
  `node_modules/.bin/tsc --noEmit` when it exists.
- Python: `ruff check .` and `ruff format --check .`, and `mypy`, each only
  when configured and installed in the project environment.
- Go: `go vet ./...` and `gofmt -l .` (any listed file fails).
- Rust: `cargo check --all-targets --frozen --offline`, plus
  `cargo fmt --check` when rustfmt is available.
- .NET: `dotnet build --no-restore`. Swift: `swift build`.
- Makefile targets `lint`, `typecheck`, or `check`.

Commands under `project_checks.commands` in `.quality/quality-gate.json`
replace inference. Each command must exit 0. Use the strictest mode the
repository currently promises and include every production package; do not
narrow a check to the changed file, because unchanged callers must still pass.
A command that cannot fail or that rewrites files, such as one with `--fix`,
`--write`, or `|| true`, is rejected. A missing executable reports `BLOCKED`, a
timeout fails, and a repository without any such command is `NOT APPLICABLE`.

### 5. Tests

This gate runs the test suite once and proves that tests really ran. Set
`test.command` to one deterministic command that runs the complete required
suite and exits nonzero on any failure; otherwise the runner infers the usual
command for Python, Node, Go, Rust, Maven, Gradle, .NET, Ruby, PHP, Swift,
Dart, Elixir, and C/C++ (CMake with CTest, or Meson, built in a directory of
the run's own). It reads the runner's own summary. When the console has no
summary, configure `test.junit` with the JUnit XML report globs.

The gate fails when the command fails or times out, when any test fails, when
any test is skipped, deselected, or filtered out, when fewer than
`tests.min_executed` tests run, or when the test count drops below the last
full run. If the user approved removing tests, rerun with
`--allow-test-removal`. Output without a recognizable summary reports
`BLOCKED`. Make the suite deterministic: isolated temporary state, controlled
clocks and randomness, unique ports, and cleanup that runs on failure.

### 6. Coverage

This gate checks that tests reach the code the change added or edited, so an
agent cannot add untested behavior. It reads the coverage report from the same
test run; tests never run twice. The measured lines are the changed lines that
the report marks as executable. A changed production file the tests never
loaded counts all lines inside its functions as uncovered. Line coverage must
reach `coverage.line_coverage_limit` and branch coverage
`coverage.branch_coverage_limit` (90% each by default). When the runtime
reports no branch data, branch coverage is shown as not reported. A change with
no executable lines, such as documentation only, passes. A missing or stale
report after a successful test run reports `BLOCKED`.

The runner wraps the test command with the language's coverage tool:

| Language | How coverage is measured | Tools |
| --- | --- | --- |
| Python | coverage.py with branch measurement | pinned coverage.py in the skill cache |
| JavaScript/TypeScript | Jest or Vitest built-in coverage; other Node test commands use V8 coverage converted by c8 | the project's Jest or Vitest coverage package; pinned c8 in the skill cache |
| Go | `go test -coverprofile` | Go toolchain |
| Rust | `cargo llvm-cov` | cargo-llvm-cov in the skill cache |
| Java/Kotlin (Maven, Gradle) | JaCoCo agent and report | pinned JaCoCo jars in the skill cache |
| .NET | `XPlat Code Coverage` collector | the `coverlet.collector` package in the test projects |
| Ruby | Ruby's built-in `Coverage` module | none |
| Swift | `swift test --enable-code-coverage` | Swift toolchain |
| PHP | PHPUnit Cobertura report | the pcov or Xdebug PHP extension |
| C/C++ (CMake, Meson) | `--coverage` build read with gcov or `llvm-cov gcov` | CMake or Meson, a C/C++ compiler, and its gcov |

PHP without pcov or Xdebug cannot produce a report. Coverage reports `BLOCKED`
for it until the project installs the PHP extension or configures an adapter. Set `coverage.command`, `coverage.report`, and
`coverage.format` in `.quality/quality-gate.json`; a configured adapter always
wins. Supported formats are `coverage-json`, `istanbul-json`, `lcov`,
`cobertura`, `jacoco`, `go-cover`, `llvm-cov-json`, `ruby-coverage`, and
`gcov`.
`{root}` and `{out_dir}` are substituted in configured values.

### 7. Complexity

This gate stops a change from leaving functions too hard to follow or test. It
uses lizard, a pinned analyzer the skill installs into its cache, which reads
most mainstream languages. For each function the change touches it checks:

- cyclomatic complexity (independent paths), at most `complexity.cyclomatic_limit` (10);
- cognitive complexity (how hard the control flow is to follow), at most
  `complexity.cognitive_limit` (15);
- nesting depth, at most `complexity.nesting_limit` (4); control structures,
  `catch`/`except`, ternaries, and lambdas add a level, a `try` body and a
  `with` block do not (for Ruby, Lua, Erlang, Fortran, PL/SQL, and Structured
  Text, nesting and its cognitive increments come from the indentation of
  lines that start with a control keyword);
- CRAP, at most `complexity.crap_limit` (30), using the function's line coverage:

  ```text
  CRAP = complexity^2 * (1 - coverage)^3 + complexity
  ```

A fully covered function scores its complexity. Without coverage data CRAP is
not computed, and the report says so. Lizard's own forgiveness comments are
ignored so they cannot hide a function. Files lizard cannot read are listed as
not analyzed. Limits: lizard parses quickly without compiling, so unusual
syntax can be measured approximately.

### 8. Duplication

This gate catches new copy-pasted code. It uses Pygments, a pinned tokenizer
in the skill cache. It compares code tokens rather than text, so renamed
variables, changed literals, inserted blank lines, and comments do not hide a
copy, and identical import blocks are ignored. The change is compared against
every production file, so copying existing code is caught. A repeated stretch
counts when it spans at least `duplication.min_tokens` tokens (50) and
`duplication.min_lines` lines (5) on each side. The gate fails on any copy that
touches a changed line. Limits: it finds copies with the same structure, not
code that does the same thing differently. Files without a Pygments lexer are
skipped.

### 9. Dead code

This gate catches functions the change leaves unused: new functions nothing
calls, and functions whose last caller the change removed. It reuses lizard's
function list and needs no other tool. A function is unused when no
production file mentions its name. A call from a test does not count: a
function that only tests call is reported as "only tests call it". Entry points
such as `main`, constructors, and tests are not reported. Decorated or
annotated functions, methods, and exported or public functions are listed for
review rather than failed, because frameworks or other packages may call them.
A private function with no references fails, and so does a new top-level
Python or Ruby function. Delete the function or call it from the code that
needs it. Limits: the search is by name, so a function called only through a
string or reflection can be reported, and an unrelated use of the same name
hides one.

### 10. Vulnerable dependencies

This gate stops a change from adding a dependency version with a known
vulnerability. It reads the repository's lockfiles, including
`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `uv.lock`,
`Pipfile.lock`, `Cargo.lock`, `go.mod`, `Gemfile.lock`, `composer.lock`, and
`packages.lock.json`, and queries the OSV advisory database, so it needs
network access. Other lockfiles, such as `bun.lock`, `mix.lock`, and
`Package.resolved`, report `BLOCKED`. In change scope it runs only when a
manifest or lockfile changed and checks only new or changed versions. A
manifest with dependencies but no lockfile fails, because its versions cannot
be checked; add a lockfile. Ecosystems without a readable lockfile, such as
Maven, Gradle without dependency locking, or .NET without
`packages.lock.json`, report `BLOCKED`, and a library crate without
`Cargo.lock` is only noted. A network failure reports `BLOCKED`. A repository
without any dependency manifest is `NOT APPLICABLE`. `--fast` defers this
gate.

## Verify the environment

Run these in order and retain the generated JSON state and HTML report:

```bash
<skill-directory>/scripts/quality --root . --fast
<skill-directory>/scripts/quality --root .
```

The first run installs any missing pinned tools into the skill cache. Add
`--no-install` to prove the cache and repository already contain every required
tool; a missing tool then reports `BLOCKED`. Confirm all configured commands
appear in command evidence, all applicable gates pass, thresholds in
`.quality/quality-gate-state.json` match `.quality/quality-thresholds.json`, and
the full command exits 0.
