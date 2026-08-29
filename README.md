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

## Install

Clone, then symlink the same skill into Claude Code, Codex, or both:

```bash
git clone https://github.com/benrben/code-skills.git
mkdir -p ~/.claude/skills ~/.agents/skills
ln -s "$(pwd)/code-skills/skills/code-discipline" ~/.claude/skills/code-discipline
ln -s "$(pwd)/code-skills/skills/code-discipline" ~/.agents/skills/code-discipline
```

The repository is also a validated plugin bundle: `.claude-plugin/plugin.json`
and `.codex-plugin/plugin.json` expose the same `code-discipline` skill without
creating a second skill. For a local Claude Code plugin session:

```bash
claude --plugin-dir "$(pwd)/code-skills"
```

Skills auto-trigger from their frontmatter `description`. The strict repository
loop runs only when requested; ordinary coding still uses the discipline without
launching a full-repository gate.

## Portable repository quality gate

[`repo_quality_gate.py`](repo_quality_gate.py) is a single-file bootstrapper and
runner for any repository. It writes a clear, self-contained HTML report with
one copy-paste prompt to fix every failure plus focused prompts for developers
who prefer to repair issues individually. The organized loop runs these checks:

- formatter and lint commands
- static type checking
- contract and schema validation
- 100% per-function coverage and CRAAP ≤ 6
- dead-code detection
- repeated-suite flaky-test detection
- zero surviving operator mutants
- zero module dependency violations

Repository-configured commands take precedence. Common package scripts and
tool configurations are detected automatically; checks unsupported by a
repository are reported explicitly as not applicable instead of silently
passing. Set a check's `required` field to `true` when missing tooling must fail.

Copy it into a repository, let it detect the available toolchain, and run it:

```bash
python3 repo_quality_gate.py --init
# Review .quality-gate.json and declare .quality-dependencies.json.
python3 repo_quality_gate.py --root . --html quality-gate-report.html
open quality-gate-report.html  # macOS; use your browser elsewhere
```

Missing detected analysis tools install automatically. Ruff, mypy, Vulture,
JSON Schema and OpenAPI validators, Python coverage, and the multi-language
Lizard complexity analyzer go into an isolated gate cache; Vitest's matching
V8 coverage provider installs into `node_modules` without changing
`package.json` or the lockfile; Rust coverage installs into a gate-owned Cargo
prefix. Mutation and dependency analysis are built into the script. Use
`--no-install` for an offline/read-only tool setup run.

The core is language-neutral. It reads standard coverage formats (Coverage.py
JSON, Istanbul JSON, LCOV, Cobertura/JaCoCo XML, and Go cover profiles), uses
Python AST or Lizard function analysis, and discovers common test commands.
Languages needing a custom semantic analyzer plug in through normalized JSON:

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

Set `metrics.command` to the language-specific analyzer and `metrics.report`
to that JSON file. Dependency adapters use the same idea with an `edges` array;
the generated config documents both contracts. Commands are argument arrays,
so shell syntax is opt-in via `["bash", "-lc", "..."]`.

The runner does not invent architecture intent. If a repository lacks a module
dependency specification or a semantic analyzer for an unknown language, that
gate fails with a prompt that an agent can use to create the missing evidence.
`--max-mutants N` is available for quick diagnostics, but a capped mutation run
can never produce a passing gate.

Mutation runs can safely use parallel workers:

```bash
python3 skills/code-discipline/scripts/quality_loop.py \
  --root /path/to/repository \
  --mutation-workers auto
```

`auto` adds workers as the mutant set grows, up to four; an explicit positive
integer such as `--mutation-workers 2` is also accepted. Every worker receives
an isolated repository snapshot (copy-on-write when supported) and separate
temporary/cache paths, so it never mutates the active worktree or another
worker's files. Repositories whose tests share fixed ports, databases,
accounts, or other resources should either isolate those resources using
`QUALITY_GATE_MUTATION_WORKER` or select one worker. The default remains one
worker for compatibility.

Agents loop on the smaller core attached to `code-discipline`:

```bash
python3 skills/code-discipline/scripts/quality_loop.py --root /path/to/repository
```

Each invocation performs one complete measurement and writes an HTML report and
machine-readable JSON state under a repository-specific user cache. Exit `0`
means all gates pass, exit `1` means the JSON contains actionable failures and a
single combined repair prompt, and exit `2` means configuration or tooling must
be repaired first. The agent fixes a coherent batch and invokes the same command
again until it exits `0`. The loop and standalone CLI share one canonical gate
engine at the plugin root, so fixes and new checks cannot drift between copies.

For faster repair iterations, use diagnostic mode:

```bash
python3 skills/code-discipline/scripts/quality_loop.py \
  --root /path/to/repository \
  --html quality-gate-report.html \
  --fast
```

Fast mode runs every static gate plus one complete tests/coverage/CRAAP pass,
but defers repeated flaky-test runs and mutation testing. It always exits
nonzero and can never certify a repository. Its JSON state reports
`ready_for_full: true` and provides `full_rerun_command` when the executed
checks are clean; run that command without `--fast` for the required final
certification. `--html` writes the JSON state beside the requested HTML file;
use `--artifact-dir` instead when both artifacts should live in a dedicated
directory.

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
