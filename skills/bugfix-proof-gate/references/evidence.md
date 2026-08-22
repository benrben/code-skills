# bugfix-proof-gate — certified record

Evaluated and archived under its original name **red-gate**; the script is
byte-identical to the certified artifact.

Pre-registered blind A/B, 2026-08-22, on a real private codebase (voicebridge,
2,769 LOC, 172-test suite): 5 mutation-guided seeded bugs invisible to the
suite, 4 arms × 5 tasks, Sonnet 5 runners, 2×5 blind Opus judges, metric and
falsifier locked before any code.

| | none | prompt rule | **gate** | rule + gate |
|---|---|---|---|---|
| Blind composite (0–10) | 4.80 | 6.00 | **9.40** | **10.00** |
| Proven-red test shipped | 0/5 | 1/5 | 5/5 | 5/5 |

The written rule (present verbatim in the system prompt) was followed in 1/5
tasks; the gate enforced it 10/10. Judges rated the forced tests genuinely
good, and the gate arm also won the subscore the gate cannot enforce (symptom
cure, test quality, no collateral) — the win is not the gate grading its own
homework. Best arm overall: **skill + gate layered** (10.00), which is why the
gate ships as a layer on code-discipline (then named modern-zen), not a
replacement.

## Falsified sibling: the refactor mode

A `preserve` mode (block test edits, require the untouched suite green) went
through the identical pre-registered protocol on 5 refactor tasks and lost:
the prompt-only skill 10.00 vs both gate arms 9.60, zero gate blocks fired
(every arm, including no-guidance baseline, preserved behavior), and the
round's only real behavior drift happened IN a gate arm — suite green, gate
satisfied, caught only by judges. Deleted per house rule. Lesson: **gate the
violations, not the defaults** — a check earns its place only where baseline
behavior actually breaks the value.

Raw evidence (preregistrations, results, scores, all cells and judge
transcripts): `~/code-skills-evals-archive/round1-red-gate/` and
`round2-preserve/`.
