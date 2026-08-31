# code-skills

One coding skill with an executable repository quality gate for Claude Code and
Codex.

It checks:

1. formatting and lint
2. static types
3. contracts and schemas
4. tests, coverage, and complexity
5. File LOC (1,000 physical lines by default)
6. dead code
7. flaky tests
8. mutation testing
9. module boundaries

## Install once

Keep one shared checkout. Every repository can use the same installation.

```bash
git clone https://github.com/benrben/code-skills.git "$HOME/code-skills"
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
ln -s "$HOME/code-skills/skills/code-discipline" \
  "$HOME/.agents/skills/code-discipline"
ln -s "$HOME/code-skills/skills/code-discipline" \
  "$HOME/.claude/skills/code-discipline"
```

Install only the link for the agent you use. Restart the agent after the first
installation.

Requirements: Python 3.10+, Git, and the target repository's own runtime and
dependencies.

## Set up a repository

From any directory, run:

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" \
  --root /path/to/repository \
  --init
```

This creates two small control files in the target repository:

- `.quality-gate.json` — commands and adapters
- `.quality-thresholds.json` — every numeric quality goal

Then ask your coding agent:

```text
Use $code-discipline to finish setting up this repository's quality environment,
including module boundaries, and run the fast quality check.
```

The skill detects the repository's stack, preserves its package manager and
lockfile, and configures the relevant formatter, linter, type checker, schema
checks, tests, coverage, dead-code detection, flaky-test runs, mutation testing,
and architecture rules.

### Agent prompt: install in this repository

```text
Install the code-discipline skill in the current repository from
https://github.com/benrben/code-skills.git. Preserve all existing changes. Use
one shared checkout at $HOME/code-skills and create project-local symlinks at
.agents/skills/code-discipline and .claude/skills/code-discipline. Never
overwrite a real file, directory, unexpected symlink, or existing quality
configuration. If .quality-gate.json and .quality-thresholds.json are both
absent, initialize them with repo_quality_gate.py --root . --init. Then read the
installed repository-setup.md, finish configuring this repository's actual
toolchain, verify the links and versions, and run the fast local-change gate.
Report everything changed and any remaining work. Do not commit or push.
```

## Run it

While working on uncommitted changes:

```bash
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" \
  --root . \
  --local-changes \
  --fast
```

Before shipping the whole repository:

```bash
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" \
  --root .
```

Fast mode is diagnostic and intentionally exits nonzero. Its JSON state tells
you when to run the full command. A full exit code of `0` means every applicable
gate passed.

Reports are written to a repository-specific user cache by default, so the
target worktree stays clean.

## Update every linked repository

Run one command from anywhere:

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" --update-from-github
```

This performs a fast-forward-only update of the shared checkout. Every
repository using the symlink immediately gets the new skill and runner. The
command refuses to update when the shared checkout has local changes.

To update from a release tag or commit instead of `main`:

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" \
  --update-from-github TAG_OR_COMMIT
```

Repository-owned `.quality-gate.json`, `.quality-thresholds.json`, and
`.quality-dependencies.json` files are never overwritten.

### Agent prompt: update from GitHub

```text
Update the code-discipline skill used by the current repository from
https://github.com/benrben/code-skills.git. Preserve all repository changes and
quality configuration. Resolve the existing skill symlink, then run the shared
checkout's repo_quality_gate.py --update-from-github command. The update must be
fast-forward-only and must stop if the shared checkout is dirty; never stash,
reset, or discard changes. If this repository uses a standalone runner, update
that runner instead. Verify the resolved skill path, old and new versions, skill
validation, and updater tests. Report results. Do not modify product code,
commit, or push.
```

### Standalone copied runner

If a repository cannot use the shared skill, copy the runner and bundled
defaults once:

```bash
cp "$HOME/code-skills/repo_quality_gate.py" /path/to/repository/
cp "$HOME/code-skills/skills/code-discipline/quality-thresholds.json" \
  /path/to/repository/quality-thresholds.json
```

That copy can update itself later:

```bash
python3 repo_quality_gate.py --update-from-github
```

The downloaded Python and JSON are validated before atomic replacement. Local
repository configuration remains untouched.

## Thresholds

Defaults live in
[`quality-thresholds.json`](skills/code-discipline/quality-thresholds.json).
`--init` copies them to `.quality-thresholds.json`, where a repository can set
its goals. File LOC defaults to 1,000 lines; coverage defaults to 100%; function
complexity and CRAAP default to 6.

Threshold values placed in `.quality-gate.json` are ignored, keeping one source
of truth.

## More help

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" --help
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" --help
```

The detailed environment guide is
[`repository-setup.md`](skills/code-discipline/references/repository-setup.md).

## Development

```bash
python3 -m unittest tests.test_repo_quality_gate
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/code-discipline
```
