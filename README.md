# code-skills

Personal collection of [Agent Skills](https://github.com/anthropics/skills)
for Claude Code and Codex. Every skill here is **eval-certified**: it exists because it
won a pre-registered, blind-judged A/B on a real codebase — and anything that
lost such an eval was deleted, not kept.

## Skills

| Skill | Task it serves | Certified record |
|---|---|---|
| [code-discipline](skills/code-discipline/SKILL.md) | Any coding work—writing, fixing, refactoring, reviewing—plus an on-request executable repository quality loop | 6–1 across 7 blind-judged execution cells, two worker models; every doctrine rule earned its place by flipping a lost cell |

Renamed 2026-08-22 for task clarity — evaluated as *modern-zen*, the name the
archive still uses.

Each skill folder contains a `SKILL.md` with its instructions and triggering
description.

## Requirements

- Python 3.10 or newer to run the quality gate.
- Git to clone/update the skill and, when available, select tracked and
  unignored repository files.
- The target repository's own runtime and package manager. For example, a
  JavaScript repository still needs Node.js and npm/pnpm/yarn; a Go repository
  needs Go.

The quality runner can install missing analysis adapters. It does not install a
repository's runtime, production dependencies, or test suite.

## Install the skill

Choose one clone location and reuse it in the commands below:

```bash
code_skills_dir="$HOME/code-skills"
git clone https://github.com/benrben/code-skills.git "$code_skills_dir"
```

### User-wide Claude Code and Codex install

Symlink the same `code-discipline` skill into Claude Code, Codex, or both:

```bash
code_skills_dir="$HOME/code-skills"
mkdir -p "$HOME/.claude/skills" "$HOME/.agents/skills"
ln -s "$code_skills_dir/skills/code-discipline" "$HOME/.claude/skills/code-discipline"
ln -s "$code_skills_dir/skills/code-discipline" "$HOME/.agents/skills/code-discipline"
```

Use only the relevant `ln -s` line when installing for one agent. The skill
auto-triggers from its frontmatter description. Ordinary coding uses the
discipline rules; the expensive full-repository loop runs only when requested.
[Codex officially scans user and repository `.agents/skills` directories and
supports symlinked skill folders](https://developers.openai.com/codex/build-skills).
Restart Codex if a newly installed skill does not appear.

### Project-local install

Use this when a repository should carry its own agent-visible skill link:

```bash
code_skills_dir="$HOME/code-skills"
target_repo="/absolute/path/to/repository"
mkdir -p "$target_repo/.claude/skills" "$target_repo/.agents/skills"
ln -s "$code_skills_dir/skills/code-discipline" "$target_repo/.claude/skills/code-discipline"
ln -s "$code_skills_dir/skills/code-discipline" "$target_repo/.agents/skills/code-discipline"
```

### Local plugin session

The repository includes `.claude-plugin/plugin.json` and
`.codex-plugin/plugin.json`. Claude Code can load the checkout directly for one
session:

```bash
claude --plugin-dir "$HOME/code-skills"
```

### Verify, update, and uninstall

```bash
# Verify the links and both runner versions.
readlink "$HOME/.claude/skills/code-discipline"
readlink "$HOME/.agents/skills/code-discipline"
python3 "$HOME/code-skills/repo_quality_gate.py" --version
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" --version

# Update the shared checkout without rewriting local history.
git -C "$HOME/code-skills" pull --ff-only

# Uninstall only links; the shared checkout and reports remain untouched.
test -L "$HOME/.claude/skills/code-discipline" && unlink "$HOME/.claude/skills/code-discipline"
test -L "$HOME/.agents/skills/code-discipline" && unlink "$HOME/.agents/skills/code-discipline"
```

## Repository quality gate

[`repo_quality_gate.py`](repo_quality_gate.py) is the canonical, single-file
engine. [`quality_loop.py`](skills/code-discipline/scripts/quality_loop.py) is
the smaller agent-facing wrapper that adds repository locking, cached artifact
locations, machine-readable state, and the combined repair prompt.

Both runners check:

1. formatter and lint commands
2. static type checking
3. contract and schema validation
4. tests, 100% per-function coverage, and CRAAP ≤ 6
5. dead-code detection
6. repeated-suite flaky-test detection
7. zero surviving operator mutants
8. zero module dependency violations

Configured commands take precedence over detection. An unsupported optional
check is `NOT APPLICABLE`; set its `required` value to `true` in
`.quality-gate.json` when missing tooling must fail.

### Recommended workflow

Run fast mode while repairing ordinary failures, then run one full certification:

```bash
quality_loop="$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py"
target_repo="/absolute/path/to/repository"

# Quick diagnostic: static gates plus one tests/coverage/CRAAP pass.
python3 "$quality_loop" \
  --root "$target_repo" \
  --html quality-gate-report.html \
  --fast

# Full certification: also repeats the suite and executes mutation testing.
python3 "$quality_loop" \
  --root "$target_repo" \
  --html quality-gate-report.html \
  --mutation-workers auto
```

Fast mode always exits nonzero because it defers flaky-test and mutation gates.
When its executed checks are clean, `quality-gate-state.json` contains
`"ready_for_full": true` and a copy-paste `full_rerun_command`.

To run from inside the target repository:

```bash
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" \
  --root . \
  --html quality-gate-report.html \
  --mutation-workers auto
```

### Agent-loop command reference

The general form is:

```bash
python3 skills/code-discipline/scripts/quality_loop.py [OPTIONS]
```

| Option | Meaning |
|---|---|
| `-h`, `--help` | Print every supported option. |
| `--root PATH` | Repository to inspect; defaults to the current directory. |
| `--config PATH` | Use a specific JSON configuration; relative paths resolve from `--root`. |
| `--artifact-dir PATH` | Put HTML and JSON artifacts in this directory. Cannot be combined with `--html`. |
| `--html PATH` | Write the HTML here and `quality-gate-state.json` beside it. Cannot be combined with `--artifact-dir`. |
| `--gate-script PATH` | Load an alternate `repo_quality_gate.py`; intended for engine development. |
| `--max-mutants N` | Execute at most `N` mutants for diagnosis. A capped run can never pass. |
| `--mutation-workers auto` | Use up to four native/portable mutation workers. This is also the default. |
| `--mutation-workers N` | Use exactly `N` positive mutation workers. More workers can increase memory use. |
| `--fast` | Defer flaky repetitions and mutation testing. Never certifies. |
| `--no-install` | Forbid automatic analysis-tool installation; useful offline or in read-only setups. |
| `--print-prompt` | Print the one combined repair prompt after a failed run. |
| `--version` | Print the wrapper version. |

Common command combinations:

```bash
quality_loop="$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py"

# Default full run; artifacts go to the per-repository user cache.
python3 "$quality_loop" --root .

# Fast repair run with visible local artifacts.
python3 "$quality_loop" --root . --html quality-gate-report.html --fast

# Full run with a dedicated output directory.
python3 "$quality_loop" --root . --artifact-dir .quality-artifacts

# Full run with a non-default configuration and two mutation workers.
python3 "$quality_loop" \
  --root . \
  --config config/quality-gate.json \
  --mutation-workers 2

# Small mutation sample for debugging only; this cannot certify.
python3 "$quality_loop" --root . --max-mutants 20 --mutation-workers auto

# Offline run using only tools that are already available.
python3 "$quality_loop" --root . --no-install

# Print the combined prompt that tells an agent how to fix every failure.
python3 "$quality_loop" --root . --print-prompt

# CLI discovery.
python3 "$quality_loop" --help
python3 "$quality_loop" --version
```

### Standalone core command reference

Copy only the canonical engine when the skill checkout is unavailable:

```bash
cp "$HOME/code-skills/repo_quality_gate.py" /absolute/path/to/repository/
cd /absolute/path/to/repository
python3 repo_quality_gate.py --init --root .
```

`--init` writes a detected `.quality-gate.json` and, when absent, an example
`.quality-dependencies.json`. Review both before certification.

The standalone options are:

| Option | Meaning |
|---|---|
| `-h`, `--help` | Print every supported option. |
| `--root PATH` | Repository to inspect; defaults to the current directory. |
| `--config PATH` | JSON configuration; defaults to `ROOT/.quality-gate.json` when present. |
| `--html PATH` | HTML path; defaults to `quality-gate-report.html`. |
| `--init` | Generate detected configuration and exit without running gates. |
| `--max-mutants N` | Diagnostic mutant cap; a capped run can never pass. |
| `--mutation-workers auto` | Automatically use up to four workers. |
| `--mutation-workers N` | Use exactly `N` positive workers. |
| `--fast` | Run one diagnostic pass and defer flake/mutation work. Never certifies. |
| `--no-install` | Do not install missing analysis tools. |
| `--version` | Print the engine version. |

```bash
# Generate configuration.
python3 repo_quality_gate.py --root . --init

# Full certification.
python3 repo_quality_gate.py \
  --root . \
  --html quality-gate-report.html \
  --mutation-workers auto

# Fast diagnostic.
python3 repo_quality_gate.py --root . --html quality-gate-report.html --fast

# Custom configuration, diagnostic mutant cap, or offline mode.
python3 repo_quality_gate.py --root . --config config/quality.json
python3 repo_quality_gate.py --root . --max-mutants 20
python3 repo_quality_gate.py --root . --no-install

# CLI discovery.
python3 repo_quality_gate.py --help
python3 repo_quality_gate.py --version
```

## Tool installation and detection

Automatic installation is enabled by default and is intentionally conservative.
The runner installs analysis adapters only when repository files show that the
tool is relevant.

| Repository signal | Automatically installed tool | Install location |
|---|---|---|
| Supported source needs complexity analysis | `lizard` | Isolated Python gate cache |
| Python tests need coverage measurement | `coverage` | Isolated Python gate cache |
| Ruff configuration exists | `ruff` | Isolated Python gate cache |
| mypy configuration exists | `mypy` | Isolated Python gate cache |
| Vulture configuration exists | `vulture` | Isolated Python gate cache |
| `*.schema.json` is discovered | `jsonschema` | Isolated Python gate cache |
| OpenAPI JSON/YAML is discovered | `openapi-spec-validator` | Isolated Python gate cache |
| Vitest exists without its coverage provider | matching `@vitest/coverage-v8` | Project `node_modules`, without changing package or lock files |
| Vitest JavaScript/TypeScript mutation is enabled | `@stryker-mutator/core@9.6.1` and `@stryker-mutator/vitest-runner@9.6.1` | Project `node_modules`, without changing package or lock files |
| Rust needs inferred coverage | `cargo-llvm-cov` | Gate-owned Cargo cache prefix |

The portable mutation engine and module-dependency checker are built in. The
runner does not guess and install a project's formatter, compiler, test runner,
or dead-code policy. Install and configure those as normal development
dependencies when the repository does not already have them.

### Optional manual project installs

Install only tools appropriate to the repository. These examples make the tools
persistent project dependencies; the gate's automatic installs do not edit
manifests or lockfiles.

```bash
# JavaScript/TypeScript examples.
npm install
npm install --save-dev eslint prettier typescript vitest knip
npm install --save-dev @vitest/coverage-v8@VERSION
npm install --save-dev @stryker-mutator/core@9.6.1 @stryker-mutator/vitest-runner@9.6.1

# Python example in an activated virtual environment.
python3 -m pip install pytest coverage lizard ruff mypy vulture \
  jsonschema openapi-spec-validator

# Rust formatter and coverage tooling.
rustup component add rustfmt
cargo install cargo-llvm-cov --locked

# Restore the dependency sets used by other detected test stacks.
go mod download
bundle install
composer install
./gradlew dependencies
mvn dependency:go-offline
dotnet restore
```

For Vitest, `@vitest/coverage-v8` should match the installed Vitest version.
The automatic installer handles that version match. pnpm/yarn repositories can
use their equivalent add/install commands; package scripts themselves are run
with the package manager identified by the lockfile.

### Automatically detected commands

| Gate | Detection |
|---|---|
| Formatter/lint | package scripts `lint:check`, `check:lint`, `lint`, `format:check`, `check:format`, `format-check`, `fmt:check`; configured ESLint/Prettier; configured Ruff; `gofmt`; `cargo fmt` |
| Static types | package scripts `typecheck`, `type-check`, `check:types`, `types:check`; `tsc`; configured mypy; `go vet`; `cargo check`; `.NET build` |
| Contracts | package script names containing `contract`, schema/OpenAPI check scripts, discovered OpenAPI documents, and `*.schema.json` |
| Tests | npm `test`, pytest, `go test`, `cargo test`, RSpec/Rake, PHPUnit, Maven, Gradle, and `dotnet test` |
| Coverage | Coverage.py JSON, Istanbul JSON, LCOV, Cobertura/JaCoCo XML, Go cover profiles; inferred coverage runs for pytest, Go, Jest, Vitest, and Rust |
| Dead code | package scripts containing `dead-code`, `deadcode`, or `unused`; Knip; ts-prune; configured Vulture |
| Flakes | the complete configured/inferred suite, three runs by default |
| Mutation | native Stryker for Vitest JavaScript/TypeScript; portable operator mutation otherwise |
| Dependencies | built-in import parsing for supported syntax or a normalized custom edges adapter |

Repository-specific commands are argument arrays, not shell strings:

```json
{
  "test": {
    "command": ["npm", "test"],
    "timeout_seconds": 600
  },
  "format_lint": {
    "required": true,
    "commands": [
      ["npm", "run", "lint"],
      ["npm", "run", "format:check"]
    ]
  },
  "types": {
    "required": true,
    "commands": [["npm", "run", "typecheck"]]
  },
  "contracts": {
    "required": true,
    "commands": [["npm", "run", "contracts:check"]]
  },
  "dead_code": {
    "required": true,
    "commands": [["npx", "--no-install", "knip"]]
  },
  "flaky_tests": {
    "enabled": true,
    "runs": 3,
    "timeout_seconds": 600
  },
  "mutation": {
    "enabled": true,
    "engine": "auto",
    "incremental": true,
    "workers": "auto",
    "max_mutants": 0,
    "timeout_seconds": 600
  },
  "tools": {
    "auto_install": true,
    "cache_dir": null
  }
}
```

Use `["bash", "-lc", "..."]` only when shell syntax is genuinely required.
`--init` generates the complete configuration, including source patterns,
coverage thresholds, adapter paths, operator maps, excludes, and timeouts.

## Metrics and architecture adapters

The core reads standard coverage reports and uses Python AST or Lizard for
function complexity. An unsupported language can supply normalized metrics by
setting `metrics.command` and `metrics.report`:

```json
{
  "functions": [
    {
      "path": "src/file.ext",
      "name": "functionName",
      "start_line": 1,
      "end_line": 10,
      "complexity": 2,
      "covered_lines": 5,
      "total_lines": 5,
      "coverage_percent": 100
    }
  ]
}
```

The runner does not invent architectural intent. Declare it in
`.quality-dependencies.json`:

```json
{
  "modules": [
    {"name": "domain", "paths": ["src/domain/**"]},
    {"name": "application", "paths": ["src/application/**"]},
    {"name": "infrastructure", "paths": ["src/infrastructure/**"]}
  ],
  "allow": {
    "domain": [],
    "application": ["domain"],
    "infrastructure": ["application", "domain"]
  },
  "deny": [{"from": "domain", "to": "infrastructure"}]
}
```

Every production file must belong to exactly one declared module. Unsupported
import syntax can use `dependencies.command` plus
`dependencies.edges_report`; the report shape is:

```json
{
  "edges": [
    {"from": "src/a/file.ext", "to": "src/b/file.ext", "line": 12}
  ]
}
```

## Mutation performance

`--mutation-workers auto` uses up to four workers. An explicit value such as
`--mutation-workers 2` can reduce memory pressure; a larger value can help only
when the machine and test runner have spare CPU and memory.

Vitest repositories use Stryker semantic mutation, per-test selection,
bail-first execution, and persistent incremental results inside one disposable
copy-on-write snapshot. An exact content-addressed proof skips mutation entirely
when production source, tests, quality configuration, and dependency manifests
have not changed. A relevant edit reruns affected mutants. The first cold full
certification must still build the complete proof and is expected to be much
slower than one unit-test run.

Other stacks use the portable snapshot-per-worker engine. Neither engine edits
the active worktree. `--max-mutants N` is useful to debug the adapter quickly,
but a capped run cannot pass or certify the repository.

`Survived`, `NoCoverage`, `Timeout`, and runner-error mutants all fail because
an assertion did not kill them. Deliberately excluded mutation kinds are not
failures. Do not run two quality loops against the same repository at once; the
wrapper rejects overlap before shared coverage output can race.

## Reports, caches, statuses, and exits

Without an explicit output path, `quality_loop.py` writes HTML and JSON under:

```text
${XDG_CACHE_HOME:-~/.cache}/repo-quality-loop/<repository-name>-<path-hash>/
```

Analysis tools and mutation proof data use:

```text
${XDG_CACHE_HOME:-~/.cache}/repo-quality-gate/
```

Set the standard `XDG_CACHE_HOME` before running to relocate both caches. With
`--html`, the state file is written beside the HTML. With `--artifact-dir`, both
go into that directory. Every wrapper run prints the exact `STATE=` and `HTML=`
paths.

| Outcome | Meaning |
|---|---|
| `PASS` | The gate ran and met its threshold. |
| `FAIL` | The gate ran or was required, and actionable failures remain. |
| `NOT APPLICABLE` | No configured/supported command was detected for an optional gate; it does not block certification. Set `required: true` if it must block. |
| `DEFERRED` | Fast mode intentionally skipped the expensive full-only gate. The run is diagnostic, not certified. |

| Exit | Meaning |
|---|---|
| `0` | Full mode completed and every required gate passed. |
| `1` | Actionable failures remain, or the run was fast/capped and cannot certify. |
| `2` | Configuration, locking, or tooling prevented a complete measurement. |

Tests/coverage/CRAAP are attempted diagnostically even when the baseline suite
fails, but cannot certify until the suite is green. Mutation testing requires a
green unmodified baseline because killed/survived results are invalid otherwise.
Open the HTML for the human-readable findings and copy-paste repair prompt; read
`quality-gate-state.json` for automation.

## Method

Nothing ships on vibes. A candidate skill or gate gets a pre-registered
experiment — metric, arms, and falsifier fixed before any code — with blind
judges scoring executable behavior on a real codebase. Winners ship with their
record in `references/evidence.md`; losers are deleted the same day (two gates
died this way: a refactor-preservation mode that guarded a discipline agents
never violate, and a mutation gate whose operator set went vacuous). Raw
evidence for every round — preregistrations, harnesses, scores, all runner and
judge transcripts — is archived outside the repo at
`~/code-skills-evals-archive/`.

Doctrine the evals earned: **gate the violations, not the defaults** (measure
the compliance gap before building a check), and **when a gate guarding a
measured gap loses, autopsy the tool before abandoning the idea**.

## Change policy

A skill here only changes when a blind-judged loss names the missing or broken
rule. No speculative additions — extra text measurably dilutes attention on
the load-bearing rules — and no speculative deletions. Propose a change by
pairing it with the eval evidence that demands it. A skill that loses is
deleted, not kept as a warning.
