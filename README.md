# code-skills

Personal collection of [Agent Skills](https://github.com/anthropics/skills)
for Claude Code. Every skill here is **eval-certified**: it exists because it
won a pre-registered, blind-judged A/B on a real codebase — and anything that
lost such an eval was deleted, not kept.

## Skills

| Skill | Task it serves | Certified record |
|---|---|---|
| [code-discipline](skills/code-discipline/SKILL.md) | Any coding work — writing, fixing, refactoring, reviewing. Nine verses of why, each with the terse rules that enforce it | 6–1 across 7 blind-judged execution cells, two worker models; every rule earned its place by flipping a lost cell |

Renamed 2026-08-22 for task clarity — evaluated as *modern-zen*, the name the
archive still uses.

Each skill folder contains a `SKILL.md` with its instructions and triggering
description.

## Install

Clone, then symlink skills into your personal skills directory:

```bash
git clone https://github.com/benrben/code-skills.git
ln -s "$(pwd)/code-skills/skills/code-discipline" ~/.claude/skills/code-discipline
```

Skills auto-trigger from their frontmatter `description`.

## Portable repository quality gate

[`repo_quality_gate.py`](repo_quality_gate.py) is a single-file bootstrapper and
runner for any repository. It enforces three required gates and writes a clear,
self-contained HTML report with one copy-paste prompt to fix every failure plus
focused prompts for developers who prefer to repair issues individually:

- 100% per-function coverage and CRAAP ≤ 6
- zero surviving operator mutants
- zero module dependency violations

Copy it into a repository, let it detect the available toolchain, and run it:

```bash
python3 repo_quality_gate.py --init
# Review .quality-gate.json and declare .quality-dependencies.json.
python3 repo_quality_gate.py --root . --html quality-gate-report.html
open quality-gate-report.html  # macOS; use your browser elsewhere
```

Missing analysis tools install automatically. Python coverage and the
multi-language Lizard complexity analyzer go into an isolated gate cache;
Vitest's matching V8 coverage provider installs into `node_modules` without
changing `package.json` or the lockfile; Rust coverage installs into a
gate-owned Cargo prefix. Mutation and dependency analysis are built into the
script. Use `--no-install` for an offline/read-only tool setup run.

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
