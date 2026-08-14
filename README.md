# code-skills

Personal collection of [Claude Code skills](https://docs.anthropic.com/en/docs/claude-code) — one directory per skill, each self-contained in a single `SKILL.md`.

## Skills

### [modern-zen](modern-zen/SKILL.md)

Engineering discipline for agentic coding, in any language. "The Zen of Modern Development": nine poem stanzas — the Zen of Python braided with clean-code, TDD, and clean-architecture couplets — each verse followed by the terse rules that enforce it. Built for coding agents: only the discipline an agent needs at the keyboard, nothing else.

What it enforces: behavior-preserving refactors (error paths included), fixes proven against the reported symptom rather than the first defect found, regression tests that fail on unfixed code, a decided failure behavior for every new I/O path (a secondary feature never breaks the primary path), match-the-house conventions, severity-ordered reviews that cite the principle behind each finding, and YAGNI-restrained design.

**Evidence, not vibes.** The skill was developed through blind A/B evaluation on a real codebase: with-skill vs. no-skill agents did identical refactor / bugfix / feature tasks on identical copies, and blinded judges scored the arms empirically — running the suites, probing behavior fidelity, and live-testing failure modes. Certified record for the current version: **6–1 across 7 blind-judged execution cells** (two worker models), plus wins on review-advice probes. Every rule in the file earned its place by flipping a lost cell.

## Install

Clone, then symlink each skill into your personal skills directory:

```bash
git clone https://github.com/benrben/code-skills.git
ln -s "$(pwd)/code-skills/modern-zen" ~/.claude/skills/modern-zen
```

Skills auto-trigger from their frontmatter `description` — no invocation needed; `modern-zen` activates whenever code is being written, reviewed, refactored, debugged, tested, or designed.

## Change policy

A skill here only changes when a blind-judged loss names the missing or broken rule. No speculative additions (extra text measurably dilutes attention on the load-bearing rules) and no speculative deletions. Propose a change by pairing it with the eval evidence that demands it.
