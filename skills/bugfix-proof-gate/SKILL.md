---
name: bugfix-proof-gate
description: This skill should be used for bug-fixing tasks where the fix must carry proof — setting up or configuring the bugfix Stop-hook gate on a project, asking why a "RED GATE" message blocked completion, or asking how to satisfy it. The gate blocks finishing a bugfix until a new or changed test fails on the pre-fix code, passes on the fix, and the full suite is green.
---

# bugfix-proof-gate

**Task: fixing a bug.** A deterministic Stop hook enforcing the proven-red
rule: **a bugfix ships with a regression test demonstrated to fail on the
unfixed code**. Certified in a pre-registered blind A/B — the written rule
alone was followed 1/5 times; the gate enforced it 10/10 and blind judges
scored its arm 9.40 vs 6.00 (see `references/evidence.md`). Evaluated under
its original name *red-gate*; the certified script is unchanged, so its block
messages still begin `RED GATE`.

## Install on a project

1. Create `redgate.json` in the project root:

```json
{"test_cmd": "python3 -m pytest -q", "test_path": "tests/", "base_ref": "HEAD"}
```

- `test_cmd` — command that runs the suite (exit 0 = green)
- `test_path` — path prefix identifying test files (`.py` under it count)
- `base_ref` — git ref of the pre-fix state. `HEAD` fits uncommitted-fix
  workflows; pin a tag or sha (e.g. a `redgate-base` tag) if the agent may
  commit during the task.

2. Register the hook in the project's `.claude/settings.json`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "${CLAUDE_PLUGIN_ROOT:-/Users/benreich/code-skills/skills/bugfix-proof-gate}/scripts/red_gate.sh",
  "timeout": 600}]}]}}
```

Projects without `redgate.json` are ignored — the hook is safe to enable
broadly and activate per project.

## How it verifies

On every attempted stop: (1) require a changed/added test under `test_path`;
(2) clone the repo at `base_ref`, transplant the changed tests, run only them
there, and require at least one failure; (3) run the full suite in the working
tree and require green. Any violation blocks with a terse reason
(`no-test-changes`, `tests-pass-on-unfixed`, `suite-red`). After 3 blocks the
gate stands down and records `gate_final_status=failed` in `.redgate/` —
bounded cost, honest failure. Add `.redgate/` to the project's `.gitignore`.

## When blocked

Satisfy the message literally: write or fix a test that reproduces the
reported bug, verify it fails on the pre-fix code and passes on the fix, keep
the suite green, then finish. Do not weaken or delete existing tests to get
through.

## Scope and limits

Certified for bugfix flows. A refactor mode was built, measured, and deleted
(refactor discipline showed no compliance gap — see evidence). The gate checks
the mechanical half only: it cannot judge whether the test targets the
reported symptom realistically, or whether the fix itself is correct. Pair it
with the code-discipline skill — the eval's best arm was skill + gate layered.
For new-feature work, use the feature-proof-gate instead (it subsumes this
check and adds coverage and mutation stages).
