---
name: feature-proof-gate
description: This skill should be used for feature-building and new-code tasks where the change must carry full test evidence — setting up or configuring the feature Stop-hook gate on a project, asking why a "HARDENER" message blocked completion, or asking how to satisfy its stages. The gate is a five-stage ladder — suite green, a test exists, tests fail on the pre-change code, every changed line covered, every mutant of the changed lines killed.
---

# feature-proof-gate

**Task: building a feature or adding new code.** A deterministic Stop hook:
a five-stage **evidence ladder** the agent climbs before it may finish.
Certified in pre-registered blind A/Bs on two codebases — 9.31 vs 7.75 over
the skill alone on the first, replicated 9.90 vs 7.90 with zero losses on the
second; tests shipped 13/13, mutation survival on changed code 2–3% vs ~50%
(see `references/evidence.md`). Evaluated under its original name *hardener*;
the certified script is unchanged, so its block messages still begin
`HARDENER v2`.

The ladder:

1. **suite green** — full test suite passes
2. **a new or changed test exists** under the project's test path
3. **prove** — the changed tests FAIL on the pre-change code (they test the new thing)
4. **coverage** — every changed executable source line runs under the suite
5. **mutation** — full-operator mutants of the changed lines (comparison
   flips, and/or, True/False, ±, in/not in, is/is not, min/max, integer ±1,
   if-condition forcing, return→None) all die under the suite

Zero mutants at stage 5 reports `passed-vacuous-mutation`, never a silent
pass. After 3 blocks the gate stands down honestly
(`gate_final_status=failed` in `.hardgate/`).

## Install on a project

1. Create `hardgate.json` in the project root:

```json
{"test_cmd": "python3 -m pytest -q", "src_glob": "*.py",
 "test_path": "tests/", "base_ref": "HEAD"}
```

- `src_glob` — which files count as source (fnmatch on repo-relative paths;
  it must NOT match test files)
- `base_ref` — the pre-change ref; pin a tag/sha if the agent may commit
- Python projects need `pytest-cov` for the coverage stage.

2. Register the hook in the project's `.claude/settings.json`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "${CLAUDE_PLUGIN_ROOT:-/Users/benreich/code-skills/skills/feature-proof-gate}/scripts/hard_gate.sh",
  "timeout": 900}]}]}}
```

`hard_gate.sh` finds `mutate_lines.py` beside itself in `scripts/` — keep them
together. Projects without `hardgate.json` are ignored, so the hook is safe to
enable broadly. Add `.hardgate/` to the project's `.gitignore`.

## When blocked

Each reason names its stage; satisfy it literally:

- `suite-red` — make the full suite green first.
- `no-test-changes` — write tests for the new behavior.
- `tests-pass-on-unchanged` — the tests don't exercise the new thing; aim them
  at the changed behavior until they fail on the pre-change code.
- `uncovered-changed-lines` — listed `file:line` never execute under the
  tests; add cases that reach them.
- `surviving-mutants` — each listed `file:line:operator` is a spot where
  flipping that operator breaks none of the tests; add or sharpen assertions
  (boundaries, exact values, branch outcomes) until every mutant dies.

Never weaken existing tests or widen `src_glob` exclusions to get through.

## Relation to bugfix-proof-gate, and limits

Stage 3 IS the bugfix-proof-gate check; this gate subsumes it for feature
work — use bugfix-proof-gate alone where the cheapest certified guard is
wanted. The gate proves tests exist, target the change, execute it, and feel
operator flips; it cannot judge whether the feature itself is correct or the
assertions capture the right business behavior. Pair with code-discipline —
values plus the check beat either alone in every certified round.
