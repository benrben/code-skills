---
name: code-discipline
description: Engineering discipline for any programming language. Use whenever code is being written, reviewed, refactored, debugged, tested, or designed — even when the user never says "clean code". It enforces behavior-preserving refactors (error paths included), minimal fixes that cover their own edge cases, regression tests proven to fail on unfixed code, decided failure behavior for every new I/O path, severity-ordered reviews that cite the principle behind each finding, YAGNI-restrained design, and an executable repository-quality loop when strict formatting, lint, types, contracts, coverage, mutation, dead-code, flaky-test, or dependency gates are requested.
---

# The Zen of Modern Development

One bet under every verse: **code is read far more often than it is written —
optimize for the reader.** Each verse below is the why; the rules under it
are the do. When rules conflict, the tiebreaker: which version does the
reader understand faster? If the honest answer is the less pure one, take it
and record why — practicality beats purity.

## Repository quality loop

Use the loop only when the user asks for strict repository hardening, the
quality gate, formatting/lint enforcement, static types, contract validation,
mutation testing, CRAAP enforcement, dead-code detection, flaky-test detection,
architecture enforcement, or continued repair until all checks pass. Ordinary
coding work does not authorize this full-repository run.

The deterministic entrypoint is `scripts/quality_loop.py` beside this file. In
Claude Code plugins it is available at
`${CLAUDE_PLUGIN_ROOT}/skills/code-discipline/scripts/quality_loop.py`; in
Codex, use the absolute path beside the loaded `SKILL.md`. Run it from the
target repository root:

```bash
python3 <skill-directory>/scripts/quality_loop.py --root .
```

During repair iterations, add `--fast`. Fast mode executes every static gate
and one complete tests/coverage/CRAAP pass, but defers flaky-test repetitions
and mutation testing. It is diagnostic only and never exits `0`. Read
`ready_for_full` in the JSON state: when true, immediately run the provided
`full_rerun_command` without `--fast`. Only that full command can certify the
repository. Use `--html PATH` for an explicit report file or `--artifact-dir
DIR` for the default HTML and JSON filenames in a dedicated directory.

For a full mutation run, `--mutation-workers auto` executes mutants in up to
four isolated repository snapshots; a positive integer selects an exact upper
bound. Every worker, including a single worker, uses a disposable snapshot, so
the active worktree is never mutated. Use one worker when tests cannot isolate
fixed ports, shared databases, external accounts, or other process-global
resources. Tests may use `QUALITY_GATE_MUTATION_WORKER` to derive worker-specific
resources. Parallelism changes throughput only; it must not reduce the mutant
set or weaken the zero-survivor requirement. Only one quality loop may run per
repository at a time because coverage tools commonly share temporary paths.

The core writes its HTML and JSON state to a user cache, leaving the target
worktree unchanged except for repairs the agent intentionally makes. Exit `0`
means every applicable gate passed. Exit `1` means measured failures remain: read
`fix_prompt` and `failures` in the printed state JSON path, repair one coherent
batch, run focused tests, and rerun; in fast mode it can instead mean the
executed checks are green and full certification is ready. Exit `2` means
configuration, an adapter, or the runner failed; repair that blocker before
changing production behavior.

Coverage and CRAAP analysis always run when their adapters are available. If
the baseline test suite is red, preserve those measurements as diagnostic
evidence and repair the tests; do not claim the metrics are certified. Flaky
and mutation results require a green unmodified baseline and remain blocked
until it passes.

Before the first run, inspect and preserve existing worktree changes. Do not
run mutation analysis concurrently with another process writing source files.
If `.quality-dependencies.json` is missing, derive its modules and permitted
directions from the intended architecture after reading the repository; never
bless accidental imports as the specification.

The finish conditions are zero formatter/linter/type/contract/dead-code
violations where those checks apply; a passing and repeatable full test suite;
100% executable-line coverage and CRAAP ≤ 6 for every production function;
zero surviving operator mutants; and zero module ownership or direction
violations. Never lower thresholds, disable a gate, cap the final mutation run,
skip tests, weaken assertions, add suppressions or exclusions, broaden an
allow-list merely to pass, or replace a command with a no-op. Continue until
the core exits `0`, then report every applicable summary and the state/report
paths. Do not commit or push unless the user asked.

## Readability counts.

> *Beautiful is better than ugly. Clear is better than clever.
> Explicit is better than implicit. Names should tell the truth.*

- Ship the version a competent stranger understands faster.
- A name that needs a comment is unfinished work — rename, split, or type
  until the comment dies; keep only comments that explain *why*.

## Functions should be small.

> *One thing should happen at one level of abstraction.
> Flat is better than nested. Boolean arguments hide two functions.*

- Extract until no function mixes a high-level step with plumbing.
- Guard clauses and early returns; happy path on the left margin.
- Arguments: none ideal, one fine, two justified, many = a named object.
- A boolean argument means two functions — give each behavior its own name.
- One obvious way per codebase — write the language you're in, and match
  the house: read the neighboring feature first; its locking, permissions,
  status reporting, and test conventions are your contract.

## Commands act; queries answer.

> *Doing both creates surprise. Side effects should be visible.
> Hidden changes become hidden defects. Complex conditions deserve names.*

- A function answers a question or changes the world — never both.
- Mutation of an argument, global, or storage shows in name and signature.
- A multi-clause boolean gets a name; then it's readable and testable.

## Errors should never pass silently.

> *Unless explicitly silenced. Errors deserve their own path.
> In the face of ambiguity, refuse the temptation to guess.
> Null is ambiguity wearing a small disguise.*

- Failure travels on its own channel; the happy path reads straight down.
- Catch the narrowest failure you can actually handle; keep the cause
  attached when wrapping — in your language's idiom.
- Silence only as a visible decision with a written reason; a bare empty
  catch is a shrug.
- Prefer a typed absence or an honest empty value over null. A documented
  "not found" from a single-item lookup is honest absence, not a crime.
- When asked to go fast, name what you're cutting — never silently drop
  error handling on money, auth, or data, or tests on the riskiest logic.
  Record every deferral as a TODO with its reason.

## Duplication is cheaper than the wrong abstraction.

> *Once the shared idea is known, say it once.
> Special cases aren't special enough to break the rules.
> Although practicality beats purity.*

- Extract when the shared idea is genuinely one domain rule — third
  occurrence is the default. Merging what only looks alike welds strangers.

## Strangers should not be reached through chains of strangers.

> *Friends may speak directly. External libraries belong behind boundaries.
> Their changes should not become your changes.*

- Don't walk `a.getB().getC().do()` — ask the friend for what you need.
- Wrap external libraries in an interface you own, sized to what you use.
- All code you didn't reason through yourself — dependencies, generated
  snippets, your own earlier output — enters through the same door: your
  interface and your tests. No trust by authorship.

## A failing test comes first.

> *Red reveals the missing behavior. Green permits refactoring.
> Tests should depend on behavior. Its failure should tell one clear story.*

- Red proves the test; a test that never failed proves nothing. A
  regression test is proven on the unfixed code — run it, watch it fail.
- Fix the reported symptom, not the first defect you find: reproduce the
  symptom itself with production-realistic values, and prove your fix cures
  that reproduction — a nearby bug is not the cause until it explains the
  report's magnitude and frequency.
- Tests drive the public path with production values, not an internal
  helper with tuned constants.
- Fast, independent, wired into CI — a test that doesn't run doesn't exist.
- One test, one behavioral promise.
- Ceremony is for code that lives — a throwaway script gets run-and-look.

## Refactoring changes structure without changing behavior.

> *A green suite makes improvement ordinary. Simple is better than complex.
> Complex is better than complicated. YAGNI is design discipline.*

- Behavior includes the error paths: partial failures, fallbacks, what
  stays set when step two throws — trace them in the diff.
- The diff of a refactor is a move, not an improvement: keep signatures,
  argument-passing, defaults, and statement order exactly as they were —
  even where a nicer shape tempts you. Improvements ship separately; never
  entangle a refactor with new features.
- Ship the smallest complete correct thing. Suite green before and after —
  run it, don't assume it.
- A fix covers its own edges: ask what sits just past its boundary — the
  sub-frame, the empty remainder, the off-by-one — nothing extra rides.
- Complicated is self-inflicted: you've drifted when you explain mechanism
  instead of purpose. Imagined requirements deserve no real complexity.

## Dependencies point inward.

> *The database is a detail. The framework and the web are details too.
> Architecture should reveal use cases. Boundaries pass simple data.
> Concurrency should be isolated. Shared mutable state should be rare and obvious.*

- One actor, one reason to change, per class or module.
- Layers and abstractions earn their place on concrete triggers — a second
  caller, a second implementation — not on diagrams.
- Every new I/O path gets a decided failure behavior: a secondary feature
  never breaks the primary path; degraded success beats total failure.
  Test the failure path.
- Shared mutable state stays named, guarded, owned. Background failures
  land somewhere visible.

## Output

Writing: the discipline shows as the shape of the code — never narrate
these rules in comments. Reviewing: quote the verse behind each finding,
order by cost — defects first, structure next, style last — and say so when
the code is already fine.
