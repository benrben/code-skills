# code-discipline

A self-contained Codex and Claude Code skill for engineering discipline and
strict repository quality gates. The installed skill includes `SKILL.md`, its
setup guide, all thresholds, the quality engine, the repair loop, the updater,
and HTML reporting.

Requirements: Python 3.10+ and the target repository's own runtime and
dependencies.

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
Codex reads `.agents/skills/code-discipline`, while Claude Code reads the safe
`.claude/skills/code-discipline` link to that same complete skill. Global
installation creates the same two paths under `$HOME`. The installer does not
clone or add the `code-skills` repository.

## Update from GitHub

Update the copy installed in the current repository:

```bash
python3 .agents/skills/code-discipline/scripts/install.py --update-current
```

Update the global copy:

```bash
python3 "$HOME/.agents/skills/code-discipline/scripts/install.py" --update-current
```

Add `--ref TAG_OR_COMMIT` to any install or update command to pin a release.
Updates validate a complete staged skill before atomically replacing the old
copy. Repository-owned quality configuration is never overwritten.

Every change under `skills/` is also published by GitHub Actions as an OCI
Agent Skill package in GHCR using the
[Publish Agent Skills action](https://github.com/marketplace/actions/publish-agent-skills).
If you use the `skr` CLI, the published package can be installed with:

```bash
skr install oci://ghcr.io/benrben/code-skills.code-discipline:latest
```

## Set up and run the quality gate

Initialize the current repository:

```bash
python3 .agents/skills/code-discipline/scripts/repo_quality_gate.py --root . --init
```

Run a fast diagnostic on local changes:

```bash
python3 .agents/skills/code-discipline/scripts/quality_loop.py --root . --local-changes --fast
```

Run complete repository certification:

```bash
python3 .agents/skills/code-discipline/scripts/quality_loop.py --root .
```

Every run generates an HTML report named `quality-gate-report.html` and a
`quality-gate-state.json` state file in the repository root, then prints both
paths. Use `--html PATH` only when you want a different location.

For a global installation, replace `.agents/skills/code-discipline` in those
commands with `$HOME/.agents/skills/code-discipline`.

`--init` creates `.quality-gate.json` for commands and adapters, plus
`.quality-thresholds.json` for every numeric goal. Defaults include File LOC at
1,000 lines, coverage at 100%, and per-function complexity and CRAAP at 6. The
skill's [repository setup guide](skills/code-discipline/references/repository-setup.md)
explains how to configure formatter/lint, static types, contracts and schemas,
tests, coverage, complexity, dead code, flaky-test detection, mutation testing,
and module boundaries.

## Prompt for an agent: install in this repository

```text
Install the complete code-discipline skill in this repository using the
repository install command in README.md. Do not clone or add the code-skills
repository. Preserve existing work and quality configuration. Read the
installed repository-setup.md, initialize and configure this repository's real
toolchain, verify both scripts with --version, then run the fast local-change
quality loop. Report changes and remaining failures. Do not commit or push.
```

## Prompt for an agent: update this installation

```text
Find whether this repository uses a local or global code-discipline skill, then
run the matching update command in README.md. Update the entire installed skill,
including SKILL.md, thresholds, setup guide, engine, loop, updater, and HTML
support. Preserve product code and repository-owned quality configuration.
Verify both scripts with --version and produce a temporary HTML quality report.
Report the old and new versions and results. Do not commit or push.
```

## Verify

```bash
python3 .agents/skills/code-discipline/scripts/repo_quality_gate.py --version
python3 .agents/skills/code-discipline/scripts/quality_loop.py --version
```

## Develop this source repository

```bash
python3 -m unittest tests.test_repo_quality_gate
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" skills/code-discipline
```
