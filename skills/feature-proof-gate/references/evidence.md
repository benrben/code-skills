# feature-proof-gate — certified record

Evaluated and archived under its original name **hardener**; the scripts are
byte-identical to the certified artifacts.

Pre-registered blind A/B, 2026-08-22, on a real private codebase (voicebridge):
8 feature tasks × 3 arms, Sonnet 5 runners, 2×8 blind Opus judges, decision
rule locked before data (mean higher AND ≥4/8 pairwise wins AND ≤1 loss).

| | none | modern-zen | **modern-zen + hardener** |
|---|---|---|---|
| Blind composite (0–10) | 7.56 | 7.75 | **9.31** |
| Tests shipped | 4/8 | 4/8 | **8/8** |
| Mutant survival on changed code | 49% | 51% | **3%** |
| Mean cost / wall per cell | $0.70 / 109s | $0.76 / 114s | $1.30 / 291s |

Won every clause (4 pairwise wins, 1 loss, 3 tolerated ties) and the
untouchable subscore (7.38 vs 6.81) — not a closed loop. Cells where the gate
capped out and stood down still scored 9.5–10: each block moved the work
forward before the gate gave up. Cost of quality: ~1.7× tokens, ~2.5× wall.

## v1 died first — the autopsy built v2

Hardener v1 (operator-flip mutants only, no coverage stage, operators split
into an enforcement set and a held-out scoring set) lost its own pre-registered
bar: mean +1.80 and zero losses, but three exact ties on cells where its
operator set was *vacuous* — the changed code contained nothing it knew how to
mutate, and one near-vacuous cell let wrong-target tests through untouched.
Deleted per house rule despite the favorable mean, because the bar is the bar.
The autopsy produced everything that made v2 win: the coverage stage (catches
tests aimed at the wrong code), the full operator set plus if-forcing and
return-nulling (kills vacuity), the vacuity guard (silence ≠ approval), and
density-prechecked eval tasks.

The pair of rounds is the method end to end: when a gate guarding a
**measured** compliance gap loses, autopsy the tool before abandoning the
idea. The gap here was measured first (features shipped with failure paths
untested 4/5 even under the skill).

## Replication on codebase #2 (round 5)

The certification replicated on **tinydb** (public embedded-DB library — a
different domain and task surface): blind composite **9.90 vs 7.90**, zero
pairwise losses, untouchable subscore a perfect 8.00, tests 5/5 vs 3/5,
mutation survival 2% vs 39%. The cross-codebase pattern matched round 4
almost exactly. Cost of quality scales with suite size: ~3× tokens / ~4.7×
wall there vs ~1.7× / 2.5× on the smaller suite.

Raw evidence: `~/code-skills-evals-archive/round5-hardener-replication/`
(replication), `round4-hardener-v2/` (v2 win),
`round3-hardener-v1/` (v1 deletion, artifact preserved in `deleted/`), and
`gap-scan/` (the measurement that justified building it).
