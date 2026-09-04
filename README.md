# code-discipline

A self-contained Codex and Claude Code skill for engineering discipline: rules
for readable code, a strict repository quality gate (tests, coverage,
complexity, smoke, module boundaries), and tools that install or update the
skill in any repository. Requirements: Python 3.10+ plus the target
repository's own toolchain.

## The workflow the skill enforces

```mermaid
flowchart LR
    S["skeleton:<br/>manifest · test runner<br/>one test · start cmd"] --> I["--init<br/>gate + git repo"]
    I --> F["fast run<br/>--local-changes --fast"]
    F -->|"items"| N["quality_items.py --next<br/>fix one file"] --> F
    F -->|"0 items"| K["git commit"]
    K -->|"next feature:<br/>test + code"| F
    K -->|"all features done"| R["ship report<br/>quality_loop.py --root .<br/>+ smoke + scope"]
    R -->|"green"| H["HAND OFF NOW"]
    R -->|"red"| N
```

Bootstrap the gate before the first source file, keep every step green, commit
each green step, and hand off only after the whole-repository ship report —
which also proves the configured core user story from outside the process
(`smoke.story`; `smoke.commands` remains available for a CLI or library),
checks composed-root tests for narrow anti-vacuous mocks, and rejects
`source.exclude` entries that hide production files (the `Gate scope` row).

The nine rules of [SKILL.md](skills/code-discipline/SKILL.md), one line each:

1. Deliverables first, gate before source — `--init` before the first feature.
2. Read the report, not the state file; fix one file per cycle.
3. Run the loop in the foreground only; never background it.
4. The core user story must run: structured smoke probes are part of the report.
5. Not finished until the ship report is green; never narrow the gate.
6. Mutation and flaky testing run only when asked.
7. Green means hand off now — nothing is added after green.
8. Commit each green step locally; push or rewrite history only when asked.
9. Fan out sub-agents by file, at most four, and measure once (below).

## Fanning out repair work

When a report lists items in many files — adopting the gate in an existing
repository, or any large first report — the parent delegates disjoint files
and keeps the only measurement:

```mermaid
flowchart TB
    P["parent: items grouped by file<br/>quality_items.py --briefs 4"] --> A1["sub-agent<br/>file 1 + its test"]
    P --> A2["sub-agent<br/>file 2 + its test"]
    P --> A3["sub-agent<br/>file 3 + its test"]
    P --> A4["sub-agent<br/>file 4 + its test"]
    A1 --> M["parent: rerun --local-changes --fast once<br/>the report is the evidence"]
    A2 --> M
    A3 --> M
    A4 --> M
    M --> K["git commit"]
```

Sub-agents never run `--init`, never edit `.quality/`, never commit; shared
modules (types, schemas, contracts) are edited by the parent first; a
sub-agent's "green" is a claim, the parent's single rerun is the evidence.

## Install from GitHub

In the current repository:

```bash
curl -fsSL -H "Accept: application/vnd.github.raw+json" "https://api.github.com/repos/benrben/code-skills/contents/skills/code-discipline/scripts/install.py?ref=main" | python3 - --repo --root .
```

Globally for all repositories on this computer:

```bash
curl -fsSL -H "Accept: application/vnd.github.raw+json" "https://api.github.com/repos/benrben/code-skills/contents/skills/code-discipline/scripts/install.py?ref=main" | python3 - --global
```

Restart the agents after the first installation. One command configures both:
Codex reads `.agents/skills/code-discipline`; Claude Code reads the
`.claude/skills/code-discipline` link to the same skill. Global installation
creates the same two paths under `$HOME`; nothing clones this repository.

## Update

```bash
python3 .agents/skills/code-discipline/scripts/install.py --update-current
python3 "$HOME/.agents/skills/code-discipline/scripts/install.py" --update-current
```

The first updates the repository copy, the second the global one; add
`--ref TAG_OR_COMMIT` to pin.
Updates validate a staged copy before atomically replacing the old one and
never overwrite repository-owned quality configuration. Every change under
`skills/` is also published to GHCR as an OCI Agent Skill package:
`skr install oci://ghcr.io/benrben/code-skills.code-discipline:latest`.

## Run the quality gate

```bash
python3 .agents/skills/code-discipline/scripts/repo_quality_gate.py --root . --init
python3 .agents/skills/code-discipline/scripts/quality_loop.py --root . --local-changes --fast
python3 .agents/skills/code-discipline/scripts/quality_items.py --root . --next
python3 .agents/skills/code-discipline/scripts/quality_loop.py --root .   # ship report
```

Targeted checks combine and never certify: `--lint`, `--types`, `--contracts`,
`--tests`, `--coverage`, `--branches`, `--slow-tests`,
`--extension-contracts`, `--extension-deps`, `--failure-paths`,
`--silent-errors`, `--test-integrity`, `--complexity`, `--craap`, `--loc`,
`--dead-code`, `--deps`, `--smoke`, plus `--flaky` and `--mutation` on request. Every run
prints each gate, `Coverage today`, `Since last run: fixed · remaining · new`,
a `To fix` list grouped by file when it is long, the exact next command, and
`QUALITY_LOOP=` / `ITEMS_TO_FIX=`. Every run writes `.quality/quality-gate-report.html`
and `.quality/quality-gate-state.json`; `--html PATH` moves the HTML. Defaults:
600 lines per file, 100% per-function line and branch coverage, 5 seconds per
test, 300 seconds per suite, 100% extension contracts, no core-to-extension
imports, 100% failure-path coverage, no silent handlers, and complexity and
CRAAP at 6. The
[setup guide](skills/code-discipline/references/repository-setup.md) documents
configuration, adapters, and metric formulas; the
[quality-loop reference](skills/code-discipline/references/quality-loop.md)
documents the report, the per-step cycle, and sub-agent briefs.

## Prompt for an agent: install in this repository

```text
Install the complete code-discipline skill in this repository using the
repository install command in README.md. Do not clone the code-skills repo.
Preserve existing work and quality configuration. Read repository-setup.md,
configure this repository's real toolchain, verify both scripts with
--version, then run the fast local-change loop and report the results.
```

## Verify and develop

```bash
python3 .agents/skills/code-discipline/scripts/quality_loop.py --version   # 3.7.0
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest tests.test_repo_quality_gate
.venv/bin/python skills/code-discipline/scripts/quality_loop.py --root . --no-install
```

The full gate uses the pinned tools in `.venv` and enforces
`.quality/quality-thresholds.json`. This workflow is measured, not assumed:
in pre-registered blind A/B rounds, loop v3.5 delivered green hand-offs in 3
of 4 cells where v3.4 delivered 0 of 2, and the fan-out cleared a 95-item
report that had previously been fatal.

## Architecture diagrams

Repository architecture and command flow are documented in
[skills/code-discipline/references/architecture-diagrams.md](/Users/benreich/SDLC/.relay/worktrees/416aea1b-80f9-4c56-9a7a-4d455a969f44/1-code-skills/skills/code-discipline/references/architecture-diagrams.md).
