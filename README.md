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
