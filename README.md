# code-skills

Personal collection of [Agent Skills](https://github.com/anthropics/skills)
for Claude Code. Every skill here is **eval-certified**: it exists because it
won a pre-registered, blind-judged A/B on a real codebase — and anything that
lost such an eval was deleted, not kept.

## Skills

| Skill | Task it serves | Certified record |
|---|---|---|
| [code-discipline](skills/code-discipline/SKILL.md) | Any coding work — writing, fixing, refactoring, reviewing. Nine verses of why, each with the terse rules that enforce it | 6–1 across 7 blind-judged execution cells, two worker models; every rule earned its place by flipping a lost cell |
| [bugfix-proof-gate](skills/bugfix-proof-gate/SKILL.md) | Fixing a bug. Stop hook: the fix cannot finish without a regression test proven to fail on the unfixed code | Blind composite 9.40 vs 6.00 (prompt rule) vs 4.80 (nothing); the written rule was obeyed 1/5, the gate 10/10 |
| [feature-proof-gate](skills/feature-proof-gate/SKILL.md) | Building a feature / new code. Stop hook: five-stage evidence ladder — prove, cover, and mutation-test every changed line | Certified on two codebases: 9.31 vs 7.75 (alfred), replicated 9.90 vs 7.90 (tinydb, zero losses); tests 13/13 under the gate; mutant survival 2–3% vs ~50% |

Renamed 2026-08-22 for task clarity — evaluated as *modern-zen*, *red-gate*,
and *hardener* respectively (the names the archive and gate messages still
use); the gate scripts are byte-identical to the certified artifacts.

Each skill folder follows the standard layout: `SKILL.md` (instructions +
triggering description), `scripts/` (the executable gates — run, never loaded
into context), `references/` (the certified evidence record).

## Install

Clone, then symlink skills into your personal skills directory:

```bash
git clone https://github.com/benrben/code-skills.git
ln -s "$(pwd)/code-skills/skills/code-discipline"   ~/.claude/skills/code-discipline
ln -s "$(pwd)/code-skills/skills/bugfix-proof-gate" ~/.claude/skills/bugfix-proof-gate
ln -s "$(pwd)/code-skills/skills/feature-proof-gate" ~/.claude/skills/feature-proof-gate
```

Skills auto-trigger from their frontmatter `description`. The two gates
additionally need per-project activation — a config file and a Stop-hook entry
in the project's `.claude/settings.json`; each `SKILL.md` carries the exact
snippet.

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
