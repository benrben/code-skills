# code-skills

`code-discipline` is a Claude Code and Codex skill with an executable quality
gate for formatting, lint, types, contracts, tests, coverage, complexity, File
LOC, dead code, flaky tests, mutation testing, and module boundaries.

Requirements: Python 3.10+, Git, and the target repository's own runtime and
dependencies.

## Commands

Choose one installation: repository-local when the skill should travel with one
project, or global when all your repositories should share one copy.

### 1. Install in one repository from GitHub

Run from the target repository root. This records `code-skills` as a Git
submodule and exposes the skill to both agents.

```bash
git submodule add https://github.com/benrben/code-skills.git .code-skills
mkdir -p .agents/skills .claude/skills
ln -s ../../.code-skills/skills/code-discipline \
  .agents/skills/code-discipline
ln -s ../../.code-skills/skills/code-discipline \
  .claude/skills/code-discipline
python3 .code-skills/repo_quality_gate.py --root . --init
```

Commit `.gitmodules`, `.code-skills`, the two symlinks, and the generated quality
configuration when the repository should share them with its contributors.

### 2. Install globally from GitHub

Run once. Every repository on this computer can then use the same installation.

```bash
git clone https://github.com/benrben/code-skills.git "$HOME/code-skills"
mkdir -p "$HOME/.agents/skills" "$HOME/.claude/skills"
ln -s "$HOME/code-skills/skills/code-discipline" \
  "$HOME/.agents/skills/code-discipline"
ln -s "$HOME/code-skills/skills/code-discipline" \
  "$HOME/.claude/skills/code-discipline"
```

Restart the agent after its first installation.

Initialize any target repository with the global copy:

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" --root . --init
```

### 3. Update the repository-local skill from GitHub

```bash
python3 .code-skills/repo_quality_gate.py --update-from-github
git add .code-skills
```

The first command performs a fast-forward-only update. The second records the
new submodule commit in the parent repository; commit it when ready.

### 4. Update the global skill from GitHub

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" --update-from-github
```

Every repository using the global symlink immediately receives the update.

Both update commands stop when the skill checkout has local changes. They never
overwrite repository-owned `.quality-gate.json`, `.quality-thresholds.json`, or
`.quality-dependencies.json`.

### 5. Install or update to a tag or commit

Append the desired Git tag or commit:

```bash
python3 .code-skills/repo_quality_gate.py \
  --update-from-github TAG_OR_COMMIT

python3 "$HOME/code-skills/repo_quality_gate.py" \
  --update-from-github TAG_OR_COMMIT
```

### 6. Run the quality gate

Repository-local installation:

```bash
python3 .code-skills/skills/code-discipline/scripts/quality_loop.py \
  --root . --local-changes --fast
```

Global installation:

```bash
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" \
  --root . --local-changes --fast
```

Remove `--local-changes --fast` for complete repository certification. Fast
mode is diagnostic and intentionally exits nonzero; a full exit code of `0`
means every applicable gate passed.

### 7. Verify the installation

Repository-local:

```bash
python3 .code-skills/repo_quality_gate.py --version
python3 .code-skills/skills/code-discipline/scripts/quality_loop.py --version
```

Global:

```bash
python3 "$HOME/code-skills/repo_quality_gate.py" --version
python3 "$HOME/code-skills/skills/code-discipline/scripts/quality_loop.py" \
  --version
```

## Agent prompt: install in this repository

```text
Install code-discipline in the current repository from
https://github.com/benrben/code-skills.git using the repository-local submodule
commands in README.md. Preserve all existing changes and never overwrite an
existing file, directory, symlink, or quality configuration. Initialize quality
configuration only when it is absent, read repository-setup.md, configure this
repository's actual toolchain, verify the installation, and run the fast
local-change gate. Report changes and remaining work. Do not commit or push.
```

## Agent prompt: update from GitHub

```text
Update the code-discipline installation used by the current repository with the
matching repository-local or global command in README.md. Preserve product code
and repository-owned quality configuration. Use only a fast-forward update and
stop if the skill checkout is dirty; never stash, reset, or discard changes.
Verify the resolved skill path, old and new versions, skill validation, and
updater tests. Report results. Do not commit or push.
```

## Thresholds

`--init` creates:

- `.quality-gate.json` for commands and adapters
- `.quality-thresholds.json` for every numeric goal

Defaults are in
[`quality-thresholds.json`](skills/code-discipline/quality-thresholds.json).
File LOC defaults to 1,000 lines, coverage to 100%, and function complexity and
CRAAP to 6. Threshold values in `.quality-gate.json` are ignored so there is one
source of truth.

## More help

```bash
python3 repo_quality_gate.py --help
python3 skills/code-discipline/scripts/quality_loop.py --help
```

See the detailed
[`repository-setup.md`](skills/code-discipline/references/repository-setup.md)
when configuring a new language stack.

## Development

```bash
python3 -m unittest tests.test_repo_quality_gate
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/code-discipline
```
