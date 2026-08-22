#!/bin/bash
# hard_gate2.sh — Hardener v2 Stop hook: a ladder of evidence for changed code.
#   1. suite green
#   2. a new/changed test exists
#   3. PROVE: the changed tests FAIL on the pre-change code (they test the new thing)
#   4. COVERAGE: every changed executable source line runs under the suite
#   5. MUTATION: full-operator mutants of changed lines all killed
# 0 mutants at stage 5 = passed-vacuous-mutation (coverage already enforced) — never a silent pass.
# Config hardgate.json: {"test_cmd", "src_glob", "test_path", "base_ref"}
# State .hardgate/: attempts, log, status. Stands down honestly after 3 blocks.

set -u
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT" || exit 0
CONFIG="$ROOT/hardgate.json"
[ -f "$CONFIG" ] || exit 0
cfg() { python3 -c "import json;print(json.load(open('$CONFIG')).get('$1','$2'))" 2>/dev/null; }
TEST_CMD=$(cfg test_cmd "python3 -m pytest -q"); SRC_GLOB=$(cfg src_glob "*.py")
TEST_PATH=$(cfg test_path "tests/"); BASE_REF=$(cfg base_ref HEAD)
V2DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STATE="$ROOT/.hardgate"; mkdir -p "$STATE"
ATTEMPTS=$(cat "$STATE/attempts" 2>/dev/null || echo 0)
log() { printf '{"ts":"%s","attempt":%s,"event":"%s"}\n' "$(date -u +%FT%TZ)" "$ATTEMPTS" "$1" >> "$STATE/log.jsonl"; }
allow() { log "allow:$1"; echo "$1" > "$STATE/status"; exit 0; }
block() {
  ATTEMPTS=$((ATTEMPTS + 1)); echo "$ATTEMPTS" > "$STATE/attempts"; log "block:$1"
  printf '{"decision":"block","reason":"HARDENER v2 (%s): %s"}\n' "$1" "$2"; exit 0
}
[ "$ATTEMPTS" -ge 3 ] && allow "gate_final_status=failed"

# 1. suite green
( cd "$ROOT" && $TEST_CMD -p no:cacheprovider > "$STATE/suite.txt" 2>&1 )
[ $? -ne 0 ] && block "suite-red" "The full test suite fails on your tree. Make it green first."

# changed files vs base
CHANGED=$(
  { git diff --name-only "$BASE_REF" 2>/dev/null;
    git status --porcelain --untracked-files=all | awk '{print $2}'; } | sort -u
)
TEST_FILES=$(echo "$CHANGED" | grep -E "^${TEST_PATH}.*\.py$" || true)

# 2. tests exist
[ -z "$TEST_FILES" ] && block "no-test-changes" "No test file was added or modified. New behavior ships with tests."

# 3. PROVE: transplant changed tests onto the pre-change code; they must fail there.
CHECK_DIR=$(mktemp -d); trap 'rm -rf "$CHECK_DIR"' EXIT
git clone --quiet --no-hardlinks "$ROOT" "$CHECK_DIR" 2>/dev/null && \
git -C "$CHECK_DIR" checkout --quiet "$BASE_REF" 2>/dev/null
for f in $TEST_FILES; do
  mkdir -p "$CHECK_DIR/$(dirname "$f")"; cp "$ROOT/$f" "$CHECK_DIR/$f" 2>/dev/null
done
( cd "$CHECK_DIR" && $TEST_CMD $TEST_FILES > "$STATE/prove.txt" 2>&1 )
PRE=$?
if [ "$PRE" -eq 0 ] || [ "$PRE" -eq 5 ]; then
  block "tests-pass-on-unchanged" "Your changed tests PASS on the pre-change code — they do not test the new behavior."
fi

# 4. COVERAGE: every changed executable source line must run under the suite.
( cd "$ROOT" && $TEST_CMD -p no:cacheprovider --cov=. --cov-report=json:"$STATE/cov.json" > "$STATE/covrun.txt" 2>&1 )
UNCOV=$(python3 - "$ROOT" "$BASE_REF" "$SRC_GLOB" "$STATE/cov.json" <<'PYEOF'
import fnmatch, json, pathlib, re, subprocess, sys
root, base, glob, covpath = sys.argv[1:5]
def run(c): return subprocess.run(c, cwd=root, capture_output=True, text=True).stdout
cov = json.load(open(covpath))["files"]
def norm(p): return str(pathlib.Path(p))
covmap = {norm(k): v for k, v in cov.items()}
missing = []
tracked = run(["git", "diff", "--name-only", base]).splitlines()
untracked = [l.split(None, 1)[1] for l in run(["git", "status", "--porcelain", "--untracked-files=all"]).splitlines() if l.startswith("??")]
def lines_for(f):
    d = run(["git", "diff", "-U0", base, "--", f])
    nums = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", d, re.M):
        s, n = int(m.group(1)), int(m.group(2) or 1)
        nums.update(range(s, s + n))
    return nums
targets = {}
for f in tracked:
    if fnmatch.fnmatch(f, glob) and (pathlib.Path(root)/f).exists():
        targets[f] = lines_for(f)
for f in untracked:
    if fnmatch.fnmatch(f, glob):
        p = pathlib.Path(root)/f
        if p.exists():
            targets[f] = set(range(1, len(p.read_text().splitlines()) + 1))
for f, nums in targets.items():
    c = covmap.get(norm(f))
    if c is None:
        missing.extend(f"{f}:{n}" for n in sorted(nums)[:6]); continue
    executable = set(c["executed_lines"]) | set(c["missing_lines"])
    uncov = sorted(n for n in nums if n in executable and n in set(c["missing_lines"]))
    missing.extend(f"{f}:{n}" for n in uncov)
print(";".join(missing[:10]) + ("" if len(missing) <= 10 else f";(+{len(missing)-10} more)"))
PYEOF
)
if [ -n "$UNCOV" ]; then
  block "uncovered-changed-lines" "These changed source lines never execute under your tests — untested code: $UNCOV. Cover them."
fi

# 5. MUTATION: full operator set on changed lines; all mutants must die.
RESULT=$(python3 "$V2DIR/mutate_lines.py" --root "$ROOT" --base "$BASE_REF" \
  --glob "$SRC_GLOB" --test-cmd "$TEST_CMD" --max-mutants 250 2>"$STATE/mutate_err.txt")
echo "$RESULT" > "$STATE/last_run.json"
MUTANTS=$(echo "$RESULT" | python3 -c "import json,sys;print(json.load(sys.stdin)['mutants'])" 2>/dev/null) || allow "gate_error"
SURVIVED=$(echo "$RESULT" | python3 -c "import json,sys;print(json.load(sys.stdin)['survived'])" 2>/dev/null) || allow "gate_error"
if [ "$SURVIVED" -gt 0 ]; then
  LIST=$(echo "$RESULT" | python3 -c "import json,sys;print('; '.join(json.load(sys.stdin)['survivors'][:8]))")
  block "surviving-mutants" "$SURVIVED mutant(s) of your changed code survive the suite — flipping the marked operator breaks none of your tests. Strengthen tests until each dies: $LIST"
fi
if [ "$MUTANTS" -eq 0 ]; then
  allow "gate_final_status=passed-vacuous-mutation"
fi
allow "gate_final_status=passed"
