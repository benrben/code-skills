# Research behind the four response modes

Research date: 7 October 2026. This note records sources and separates their
claims from the design judgments used by the skill.

## Origin and corrections

The passage describes four media: simplified prose, diagrams/images, interactive
web pages, and bespoke explainer videos. An
[original post attributed to Andrej Karpathy](https://x.com/karpathy/status/2105819303471976479)
was located, but direct access returned HTTP 403 during research. An
[indexed mirror](https://x.twstalker.com/karpathy/status/2105819303471976479)
reproduces the four-media passage. This establishes a likely provenance, not
independent verification of the original post's contents or date. The technical
guidance below rests on standards, official documentation, and research papers.

Corrections to the supplied passage:

- The standard is **ASD-STE100**.
- The name is **Andrej Karpathy**. His [own biography](https://karpathy.ai/)
  describes an AI researcher and educator with OpenAI and Tesla roles. It does
  not support the passage's claim that he trains Claude at Anthropic.
- **3Blue1Brown** is Grant Sanderson's project. Its
  [about page](https://www.3blue1brown.com/about/) describes visual mathematics
  and the Manim animation engine.
- **ElevenLabs** supplies speech generation; audio narration is one component
  of a rendered video workflow.

## 1. ASD-STE100-inspired writing

ASD-STE100 was developed for aerospace technical documentation. The official
[download page](https://asd-ste100.org/STE_downloads.html) identifies Issue 9,
January 2025. The [standard](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf)
combines writing rules with a controlled dictionary.

**What is supported:** This is an established controlled language, not a new
prompting invention. Its vocabulary and writing constraints provide concrete
ways to reduce ambiguity.

**What is uncertain:** The sources reviewed do not establish an optimal “80%”
setting or guarantee that LLM output follows the standard. STEMG explicitly
warns that plausible AI text can fail its rules and vocabulary. Its
[tool guidance](https://asd-ste100.org/STEsoftware.html) also explains why
automated checking cannot replace informed review.

**Skill decision:** Use the clarity principles with natural English by default.
Reserve compliance claims for a documented check against the actual standard.

## 2. Diagrams

[W3C Technique G103](https://www.w3.org/WAI/WCAG22/Techniques/general/G103)
describes how charts, diagrams, and other visual explanations can help people
understand difficult text, data, processes, and relationships. This is practical
accessibility guidance, not evidence that every diagram improves every answer.

[W3C's complex-image tutorial](https://www.w3.org/WAI/tutorials/images/complex/)
explains the need for text equivalents that convey a visual's essential
information. [Mermaid's official documentation](https://mermaid.js.org/config/accessibility)
provides accessible diagram titles and descriptions through `accTitle` and
`accDescr`.

**Skill decision:** Match the diagram type to the relationship being explained.
Keep an accompanying interpretation and check the actual relationships as well
as the rendering. A beautiful diagram can still encode a false dependency.

## 3. Interactive web pages

HTML, CSS, and JavaScript can turn an explanation into a model the reader
controls. [MDN's range-input documentation](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/range)
documents bounded numeric sliders and cautions that they are imprecise.
[MDN's reduced-motion documentation](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/%40media/prefers-reduced-motion)
explains how pages can respect the user's motion preferences.

**What is supported:** These browser capabilities make interactive calculators,
comparisons, and simulations feasible.

**What is uncertain:** The reviewed sources do not show that an AI-generated
page is inherently correct or a better teaching medium than text. A functioning
slider proves interaction, not the correctness of the calculation.

**Skill decision:** Use HTML when changing an input reveals something useful.
Expose assumptions, check independent expected results, and test the delivered
page in a browser.

## 4. Bespoke explainer videos

[3Blue1Brown's account of its work](https://www.3blue1brown.com/about/)
demonstrates visual mathematical explanation and identifies Manim as its
animation engine. [ElevenLabs' TTS documentation](https://elevenlabs.io/docs/overview/capabilities/text-to-speech)
documents speech generation; its
[timing endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps)
documents character-level alignment for audio-text synchronization.
[W3C caption guidance](https://www.w3.org/WAI/media/av/captions/)
explains captions and the need to review automated output.

**Evidence limit:** [Tversky, Morrison, and Bétrancourt's 2002 review](https://www.tc.columbia.edu/faculty/bt2158/faculty-profile/files/_Morrison_Betrancourt_AnimationCanitfacilitate.pdf)
questions a general advantage of animation over comparable static graphics.
It identifies perceptual problems with rapid or complex animation. This paper
predates modern AI video tools; it informs presentation design, not a benchmark
of current generation quality.

**Skill decision:** Use video when a timed transformation serves the learning
goal. Write a scene plan, synchronize visuals to actual narration, render, and
review playback. Requesting a style and a TTS key alone is not a complete video
production or verification procedure.

## Overall conclusion for skill design

The four approaches are useful output options. The reviewed evidence does not
establish a universal text-to-diagram-to-HTML-to-video ranking. `agent-response`
therefore chooses by the reader's task and respects explicit format requests.
Its format selector and workflows are practical design recommendations inferred
from the sources above.
