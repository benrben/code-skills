---
name: code-discipline
description: Engineering discipline for any programming language. Use whenever code is written, reviewed, refactored, debugged, tested, or designed. Enforces behavior-preserving changes, regression tests, explicit failure behavior, readable design, and the bundled incremental quality loop when a repository has configured it.
---

# The Zen of Modern Development

One bet under every verse: **code is read far more often than it is written —
optimize for the reader.** Each verse below is the why; the rules under it
are the do. When rules conflict, the tiebreaker: which version does the
reader understand faster? If the honest answer is the less pure one, take it
and record why — practicality beats purity.

## Repository quality loop

Discipline means the work is not finished until three things are true: every
deliverable the user asked for exists, the application has been started and
loaded once, and the ship report is green. The bundled loop prints that
report. Read [references/quality-loop.md](references/quality-loop.md)
completely before running it.

1. **Deliverables first, gate before source.** Before writing code, list
   what the task must deliver (README, the command that starts the
   application — which you will run, tests for what changes, decisions to
   record). "Finished" means that list is done *and* the ship report is
   green. In a new project the first files are the skeleton: the package
   manifest, the test runner with one passing test of one real function, and
   the start command. Run `--init`, then `--local-changes --fast`, and reach
   0 items *before* the first feature — never write the whole application
   and repair it afterwards.
2. **Read the report, not the state.** The terminal report is the whole
   interface: the gates, `Since last run`, and the files to fix in order.
   Never open `.quality/quality-gate-state.json` or the HTML. Run
   `quality_items.py --next` (printed as `Next file:`), fix that one file,
   rerun `--local-changes --fast`. One file per cycle. Add a check flag —
   `--coverage`, `--branches`, `--slow-tests`, `--extension-contracts`,
   `--extension-deps`, `--failure-paths`, `--silent-errors`,
   `--test-integrity`, `--craap`, `--lint`, `--types`, `--tests`,
   `--complexity`, `--dead-code`, `--deps`, `--loc`, `--smoke` (they combine) — only when the report points at one
   gate. Do not run the test suite by hand between
   cycles: the fast run is the test run.
3. **Foreground only.** Run the loop in the same command whose exit code you
   read. Never run it in the background, never wait for a notification, never
   end a session while a run is in flight.
4. **The core user story must run.** The ship report includes **Runs (smoke)**.
   For a web app, `smoke.story` runs a deterministic outside-process probe and
   parses its fresh step report; every required probe and page-error check must
   pass. Use `smoke.commands` only for a CLI or library entry point. A unit test
   or a one-drag browser check does not prove a composed workflow. The report
   is red until the configured story is green.
5. **Not finished until the ship report is green.** Before handoff run the
   ship report — no check flags: `--local-changes` for a change to an
   existing repository, `--root .` alone for a new project. Fix every red
   line, rerun, repeat. Red means not finished. Never lower a threshold,
   disable a check, or hide a finding to get green. Never add a production
   file to `source.exclude` (the **Gate scope** row catches it): an entry
   point gets a startup or smoke test instead.
6. **Mutation and flaky testing only when asked.** They are off in new
   configurations and show as OFF in the report. Run `--mutation` or `--flaky`
   when the user asks for them. `[N/A]` rows are optional checks that are not
   configured; they are not to-do items.
7. **Green means hand off now.** The message after `QUALITY_LOOP=PASS` is the
   hand-off: the deliverables, ticked, and the report's status line. Do not
   add tools, configs, or checks after green.
8. **Commit each green step.** When the fast run prints READY_FOR_FULL,
   commit the step locally (`git add -A && git commit -m …`); `--init`
   creates the repository when there is none. The next step's fast run then
   measures only what changed. Never push, amend, or rewrite history unless
   the user asks.
9. **Fan out, measure once.** When the report lists items in more than one
   file, or a feature splits into independent directories, use sub-agents
   (the Agent tool): one per production file plus its test file, or one per
   directory, at most four at once, all in the foreground.
   `quality_items.py --briefs 4` prints the briefs. You keep the gate,
   `.quality/`, the shared modules (types, schemas, contracts — edit them
   *before* fanning out) and every commit; a sub-agent never runs `--init`,
   never edits `.quality/`, never commits. Wait for all of them, then run
   `--local-changes --fast` once: the report is the evidence, a sub-agent's
   "green" is a claim. Never hand off while a sub-agent is running.

In a new project, `python3 <skill-directory>/scripts/repo_quality_gate.py
--root . --init` produces a gate that runs on the first try (then follow
[references/repository-setup.md](references/repository-setup.md)). In an
existing repository without a gate, run its focused checks and set the gate up
only when asked. For a requested commit use `--commit [REF]`; whole-repository
scope is for the ship report of a new project and for requested hardening,
audit, or release certification (those alone are read-only). Local commits of
your own green steps are part of the loop (rule 8); pushing, amending, or
rewriting history needs the user.

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
