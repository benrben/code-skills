#!/bin/bash
# red_gate.sh — Claude Code Stop hook enforcing the proven-red rule:
# a bugfix must ship with a regression test that FAILS on the unfixed code
# and PASSES on the fixed code, with the full suite green.
#
# Config: redgate.json in the project root:
#   {"test_cmd": "python3 -m pytest -q", "test_path": "tests/"}
# State:  .redgate/ in the project root (attempts counter + log). After 3
#         blocks the gate stands down and records gate_final_status=failed.
#
# Hook contract: emit {"decision":"block","reason":...} on stdout to block
# the stop; emit nothing (exit 0) to allow it.

set -u
cd "$(pwd)" || exit 0
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT" || exit 0

CONFIG="$ROOT/redgate.json"
[ -f "$CONFIG" ] || exit 0
TEST_CMD=$(python3 -c "import json;print(json.load(open('$CONFIG'))['test_cmd'])" 2>/dev/null) || exit 0
TEST_PATH=$(python3 -c "import json;print(json.load(open('$CONFIG'))['test_path'])" 2>/dev/null) || exit 0
# Diff basis: the recorded pristine commit, so a runner that commits its work
# can neither dodge the gate nor confuse the transplant check.
BASE_REF=$(python3 -c "import json;print(json.load(open('$CONFIG')).get('base_ref','HEAD'))" 2>/dev/null) || BASE_REF=HEAD

STATE="$ROOT/.redgate"
mkdir -p "$STATE"
ATTEMPTS_FILE="$STATE/attempts"
ATTEMPTS=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)

log() { printf '{"ts":"%s","attempt":%s,"event":"%s"}\n' "$(date -u +%FT%TZ)" "$ATTEMPTS" "$1" >> "$STATE/log.jsonl"; }

allow() { log "allow:$1"; echo "$1" > "$STATE/status"; exit 0; }

block() {
  ATTEMPTS=$((ATTEMPTS + 1))
  echo "$ATTEMPTS" > "$ATTEMPTS_FILE"
  log "block:$1"
  printf '{"decision":"block","reason":"RED GATE (%s): a bugfix must include a regression test proven on the unfixed code. %s Required: at least one new or modified test under %s that FAILS on the pre-fix code and PASSES on your fixed code, with the full suite green. Then finish."}\n' "$1" "$2" "$TEST_PATH"
  exit 0
}

# Stand down after 3 blocks — bounded cost, honest failure recorded.
if [ "$ATTEMPTS" -ge 3 ]; then
  allow "gate_final_status=failed"
fi

# 1. Changed/added test files vs the pristine commit (committed or not).
CHANGED=$(
  { git diff --name-only "$BASE_REF" 2>/dev/null;
    git status --porcelain --untracked-files=all | awk '{print $2}'; } | sort -u
)
TEST_FILES=$(echo "$CHANGED" | grep -E "^${TEST_PATH}.*\.py$" || true)
if [ -z "$TEST_FILES" ]; then
  block "no-test-changes" "No test file was added or modified."
fi

# Portable timeout (macOS has no GNU timeout): run_tc <secs> <workdir> <logfile> <cmd...>
run_tc() {
  python3 - "$@" <<'PYEOF'
import subprocess, sys
secs, workdir, logfile = int(sys.argv[1]), sys.argv[2], sys.argv[3]
with open(logfile, "w") as log:
    try:
        r = subprocess.run(sys.argv[4:], cwd=workdir, stdout=log, stderr=log, timeout=secs)
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        sys.exit(124)
PYEOF
}

# 2. Transplant the new tests onto the pristine code; they must fail there.
CHECK_DIR=$(mktemp -d)
trap 'rm -rf "$CHECK_DIR"' EXIT
git clone --quiet --no-hardlinks "$ROOT" "$CHECK_DIR" 2>/dev/null || exit 0
git -C "$CHECK_DIR" checkout --quiet "$BASE_REF" 2>/dev/null || exit 0
for f in $TEST_FILES; do
  mkdir -p "$CHECK_DIR/$(dirname "$f")"
  cp "$ROOT/$f" "$CHECK_DIR/$f"
done
run_tc 120 "$CHECK_DIR" "$STATE/prefix_run.txt" $TEST_CMD $TEST_FILES
PRE_EXIT=$?
# pytest: 0 = all passed, 5 = nothing collected — neither proves anything.
if [ "$PRE_EXIT" -eq 0 ] || [ "$PRE_EXIT" -eq 5 ]; then
  block "tests-pass-on-unfixed" "Your new tests PASS on the unfixed code — they prove nothing."
fi

# 3. Full suite must be green on the fixed tree.
run_tc 300 "$ROOT" "$STATE/suite_run.txt" $TEST_CMD
SUITE_EXIT=$?
if [ "$SUITE_EXIT" -ne 0 ]; then
  block "suite-red" "The full suite fails on your fixed tree."
fi

allow "gate_final_status=passed"
