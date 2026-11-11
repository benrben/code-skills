# Guided project setup

Use this conversation the first time a user sets up quality gates for a project,
or when they ask to change that project's policy. Keep questions in chat; the runner
asks no questions, so never send the user to a terminal wizard.

## Start with the project

The user can start with one plain request: "Install code-discipline here."
That request includes project setup. Read this guide before installation and
continue with the first setup question as soon as the installed copy is verified.
Do not stop with "Installed", ask "Want me to do setup next?", or require the
user to invoke the skill again. When installation is requested, handle its supported project-local steps; do not
make the normal user flow a list of terminal commands, symlink paths, JSON edits,
or metric names. Keep those implementation details in the agent's work. Report
an actual prerequisite or required session reload plainly if the environment
needs one. Do not claim installation or readiness before verifying it.

For a supplied local checkout, use its launcher; it selects a compatible Python
and calls the supported local installer without downloading the skill:

```bash
<checkout>/skills/code-discipline/scripts/quality --install --root <project>
```

This installs the local working tree for Codex and Claude Code, including
uncommitted edits, and verifies both. When retrying installation or replacing
an existing managed copy at the user's request, add `--update`; a retry does
not need another permission question. Never replace an unmanaged path.
Existing project policy and Git
metadata remain untouched by installation. After installation, read the
installed `SKILL.md` and this guide directly; agent discovery after restart is
separate from completing setup in this conversation. Keep this command and its
implementation details out of the normal user-facing flow.

Inspect the target root, manifests, existing check commands, and `.quality/`
files without running project code. Name the project; show its path only when
needed to distinguish the destination.
Reuse a policy with `setup.completed: true`. During installation, existing
settings without that marker are a starting recommendation, not proof that the
user completed this setup. Preserve their values and offer the two choices below.
If settings do not exist, say:

> I'll suggest checks and limits for this project. You can keep them or change
> them. I'll save your choices so we only need to do this once.

Every gate is generic: it works for any language without project setup and
judges the agent's change. Do not run a product or risk questionnaire. Ask a
project question only when the answer changes a recommendation and the
repository cannot show it, such as whether advisory network access is allowed.

## Two choices for the normal setup

Prepare a complete recommended policy internally from project evidence and the
gate table below. Keep the user's experience short:

1. Show a short overview of the recommended checks, including any material
   tooling, network, or runtime cost. Use everyday names such as "honesty
   checks", "code checks", "tests", and "security checks", with a brief reason
   for each group. Ask: **"Use these checks, or customize them?"**
2. After the checks are chosen, show the suggested limits for them with plain
   explanations and units. For example, "90% line coverage means tests run at
   least 9 out of 10 executable lines that the change adds or edits." State that
   errors must be fixed for enabled checks that require zero findings. Offer the
   full numeric summary as details, rather than making the user answer a long
   list of metric questions. Ask: **"Keep these limits, or adjust them?"**

Wait for the answers. Accepting the displayed recommendation explicitly accepts
that proposed group; it is not permission to apply unrelated defaults. Do not
ask separately about every gate and threshold on the recommended path. Do not
add another confirmation after both choices are complete. Save, verify, then
say: **"Saved for this project. Future work will use these settings."** Report
verification blockers instead of claiming readiness when checks cannot run.

If the user chooses customization, ask only about the settings they want to
change, one topic at a time. Every check remains individually switchable. Use
the detailed tables below for internal configuration and explanations on
request; do not show their JSON keys or full tables by default.

For ordinary coding, reuse existing settings without forced onboarding. During
installation, reuse a completed policy; otherwise finish the two setup choices
using existing settings as the starting recommendation. If the user asks to
review a completed policy, use the same two choices with that policy as the
starting recommendation.

## Choose gates, one topic at a time

Use this detailed path when the user chooses customization. For each topic,
explain what the checks catch in the agent's change, what tools they need, and
the main time cost. Recommend choices for this project, then ask which checks
to enable or disable. Offer a custom answer. Use one short question at a time,
or at most three closely related questions; do not present the entire table
as a form. Wait for each answer before moving to dependent choices. Silence, a
preselected option, and detection do not accept a policy.

Start with honesty checks, then code checks, tests, maintainability, and
security. Every gate can be enabled or disabled for this project. Offer
individual choices within a group.

| Topic | Saved gate keys | Explain in plain language |
| --- | --- | --- |
| Honesty | `shortcuts` | Catches ways an agent can make checks pass without doing the work: new suppression, skip, or focus markers (such as `# type: ignore`, `eslint-disable`, `.skip(`, `.only(`), error handlers that silently swallow failures, production files hidden from the checks, and edits to the saved policy. Needs no tools. |
| Code checks | `project_checks` | Runs the lint, type, format, and build commands the repository already declares. Uses only the repository's own tools and never installs any. A repository with no such commands has nothing to run here. |
| Tests | `tests`, `coverage` | `tests` runs the project's test command once and fails on failures, skipped or filtered tests, or a test count that drops. `coverage` reads that same run and checks that tests reach the lines the change added or edited. Coverage uses the language's own tooling or pinned tools the skill installs into its cache. C/C++ projects built with CMake or Meson are measured with gcov. PHP needs pcov or Xdebug, and .NET needs `coverlet.collector`; until then coverage reports BLOCKED. |
| Maintainability | `complexity`, `duplication`, `file_loc`, `dead_code` | Functions the change makes too branchy, deeply nested, or risky for their coverage (CRAP); new copies of existing code; files that grow past the size limit; and functions the change leaves unused. Complexity, duplication, and dead code use pinned analyzers the skill installs into its cache; file size needs no tools. |
| Security | `secrets`, `vulnerabilities` | Known credential formats in any changed text file, and known vulnerable versions of dependencies the change adds or upgrades. Secrets needs no tools. Vulnerability checks query an advisory database, so explain that network need before enabling them; a manifest without a lockfile fails because its versions cannot be checked. |

Example question:

> Coverage shows which of the changed lines the tests actually ran. Branch
> coverage also checks both outcomes of decisions, such as success and
> failure. Higher targets need more test work. Would you like the coverage
> gate enabled for this project?

Use `gates` in `.quality/quality-gate.json` for explicit choices. Keys are the
runner's gate keys above, not CLI flag names. Numeric thresholds belong in the
threshold file. For example, after the user chooses these values:

```json
{"gates": {"tests": true, "coverage": true, "vulnerabilities": false}}
```

Unspecified gates retain existing behavior. To make a first-time policy complete,
record a boolean for every key in the table, including choices accepted as a
group. An enabled gate requires real evidence; a missing tool is not a reason
to silently turn it off. `tests`, `coverage`, and `complexity` are separate
switches, but coverage and the CRAP score come from the one test run, so
enabling either still runs the test command. CLI check flags select a run; they
do not enable a gate saved as OFF. To re-enable it, change the project's saved
choice.

An older policy may use previous gate keys. The runner maps them to the new
ones (for example `quality` to `tests`, `coverage`, and `complexity`, and
`format_lint` or `types` to `project_checks`) and ignores removed gates with a
visible note. When reviewing such a policy, propose the new keys and save them.

## Set limits for enabled gates

For the recommended path, summarize the proposed limits together and ask one
keep-or-adjust question. For customization, explain each relevant limit before
asking for a value. Name the unit, whether
higher or lower is stricter, and what a failure means. Offer a project-specific
recommendation, the bundled value as a reference, and a custom value.
Bundled values are proposals, not accepted requirements. Let the user accept a
related group of recommendations in one answer after explaining its values.
Skip threshold questions for disabled gates.

| Limits | Meaning and units | Bundled reference |
| --- | --- | --- |
| `coverage.line_coverage_limit`, `coverage.branch_coverage_limit` | Minimum percent of the change's executable lines, and of decision outcomes on those lines, that tests ran. Higher is stricter. When the runtime reports no branch data, branch coverage is shown as not reported rather than failed. | 90% each |
| `complexity.cyclomatic_limit`, `complexity.cognitive_limit`, `complexity.nesting_limit` | Maximum per changed function: independent paths, how hard the control flow is to follow, and nested control levels. Lower is stricter. | 10, 15, and 4 |
| `complexity.crap_limit` | Maximum per changed function. CRAP combines cyclomatic complexity with missing line coverage: `complexity^2 * (1 - coverage)^3 + complexity`. A fully covered function scores its complexity. Lower is stricter. | 30 |
| `file_loc.max_lines` | Maximum physical lines per production file. A change fails when it pushes a file over the limit or grows a file that is already over it. Lower is stricter. | 600 lines |
| `duplication.min_tokens`, `duplication.min_lines` | Smallest repeated stretch that counts as a copy, in code tokens (at least 10) and lines (at least 2). Lower is stricter. | 50 tokens and 5 lines |
| `tests.min_executed` | Minimum tests that actually run (at least 1). An empty suite provides no test evidence. | 1 |
| `shortcuts.max_findings`, `secrets.max_findings`, `project_checks.max_failures`, `tests.max_skipped`, `tests.max_failures`, `duplication.max_new_clones`, `dead_code.max_findings`, `vulnerabilities.max_findings` | Fixed at 0 when these gates are enabled; commands must also exit 0. Explain this rule rather than offering unsupported custom allowances. Users choose whether to enable each gate. | 0 each |

Do not invent other limits. Validate all proposed numeric values with the
runner's threshold validation before executing checks. Explain rejected values
and ask for a valid replacement rather than silently clamping them.

## Save and verify

Summarize the accepted ON/OFF choices and limits in a compact table. State which
checks a PASS will cover. After the user has supplied all needed decisions,
initialize missing files with the configuration-only entrypoint:

```bash
python3 <skill-directory>/scripts/quality_loop.py --root PROJECT --init
```

Use a Python 3.9+ interpreter. This command creates configuration templates
without running checks or asking questions and refuses to overwrite existing
files. When files already exist, edit only the requested project settings; do
not initialize over them. Save the chosen policy before running the first
baseline with `<skill-directory>/scripts/quality --root PROJECT --no-install`.

Save only in the target project's `.quality/` folder. Preserve existing commands
and unrelated settings. Mark the completed conversation with
`setup.completed: true` in `quality-gate.json`; the agent uses this marker to
avoid repeating onboarding. Existing configurations without the marker remain
valid for coding tasks, but an install-and-setup request must finish the choices
and save the marker while preserving unrelated settings.

The `shortcuts` gate treats any change to `.quality/quality-gate.json` or
`.quality/quality-thresholds.json` in the agent's work as a policy change that
needs the user's approval. After the user has confirmed the saved choices in
this conversation, run the checks with `--allow-policy-change`. Never add that
flag on your own during repair.

Run the enabled checks and explain any missing tools or evidence. Keep disabled
checks visibly OFF. If the user disables every gate, save that choice, explain
that there is no verification evidence, and do not claim certification. On later
tasks, reuse the project policy. Reopen only the affected setup topic when the
user requests a change or a new gate needs a decision; never tighten or loosen
policy automatically during repair.
