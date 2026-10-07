# Interactive HTML

Use this mode when changing inputs, comparing cases, or stepping through a
model helps the user understand it. A page should answer a question through
interaction, not just put the same paragraphs in a browser.

## Design the learning interaction

Identify the input the reader can change, the output that responds, and the
relationship the interaction reveals. Start with a useful default scenario.
Keep controls close to the chart, result, or mechanism they affect.

Prefer a standalone HTML file with embedded CSS and JavaScript for a small
disposable explainer. Use the existing stack for an established project. Make
dependencies explicit; do not call a page self-contained if it requires a CDN
or remote service to function.

Use native labeled inputs, readable outputs, visible focus, and keyboard
operation. Pair a slider with precise numeric entry when exact values matter:
MDN describes [range controls](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/range)
as imprecise. Keep layout usable on narrow screens. Supply chart values or
an equivalent explanation in text. Respect
[reduced-motion preferences](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/%40media/prefers-reduced-motion)
and give the user control over explanatory animation.

## Make the model inspectable

Show units, assumptions, input ranges, and the key formula or recurrence near
the result. Label illustrative data. Make invalid inputs visible rather than
silently producing a plausible result.

For calculations, establish expected values independently of the UI. Check
ordinary and boundary cases before polishing transitions. The same data should
drive the number, chart, and explanatory text.

## Example: recurring investment calculator

Prompt: “Create a standalone HTML calculator for monthly contributions. Let me
change the contribution, annual effective growth rate, and number of years.
Show contributions, growth, and total balance year by year. Explain the model.”

An explicit illustrative model could use:

- Annual effective rate `a`; monthly rate `r = (1 + a)^(1/12) - 1`.
- A fixed contribution `c` at each month's end, with initial balance `B_0 = 0`.
- Monthly recurrence `B_m = B_(m-1) * (1 + r) + c`.
- Year-end contributions `12 * year * c`; growth is balance minus contributions.

State that the assumed constant rate is a scenario, not a forecast. State whether
fees, taxes, and inflation are included. An advertised annual nominal rate would
require a different conversion; do not silently substitute one convention for
another.

Useful independent checks: at zero growth, 100 per month for one year gives
1,200; after the first month the balance is exactly one end-of-month contribution.
At a monthly rate of 1%, two contributions of 100 give a balance of 201.

## Verify and deliver

Open the actual page in an available browser or preview tool. Change controls;
confirm every dependent output updates. Check numeric cases, reset behavior
when present, keyboard access, a narrow viewport, and console errors. Follow
applicable coding quality requirements for the generated implementation.

Deliver the working file or preview with a short usage note. Publication uses
the user's requested hosting workflow and authorization; creating an explanation
does not itself request public hosting.
