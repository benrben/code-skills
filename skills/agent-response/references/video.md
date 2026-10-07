# Explainer video

Use this mode when a transformation, moving mechanism, or timed sequence helps
teach the concept, or when the user explicitly requests video.

“3Blue1Brown” or “3b1b” refers to Grant Sanderson's visual mathematics work.
Translate that cue into geometric reasoning, persistent objects, consistent
color meanings, and a gradual path from intuition to notation. It is not a
request for a literal blue-and-brown palette. See the
[creator's description](https://www.3blue1brown.com/about/).

## Choose the production workflow

Respect an explicitly chosen framework. If the environment provides a mandatory
video entrypoint such as `hyperframes`, read and follow it before production.
Otherwise use an available renderable workflow that fits the task. Manim is a
natural option for mathematical animation; HTML animation plus a video renderer
can fit other explainers. Check renderer and audio capability early.

Keep the explanation and media project reproducible. A video request needs a
playable rendered file, not just scene code, prompts, or a storyboard.

## Plan what each scene teaches

Set a concrete learning goal. Use a question, a visual model, a worked
transformation, and a conclusion when that structure fits. For each scene, note
the narration, visible objects, change, and insight the viewer should gain.

Carry the same objects and terminology across scenes. Introduce notation after
the visual meaning is established. Reveal a relationship when the narration
explains it. Pause after a consequential change so the viewer can inspect it.
Use motion to show the mechanism rather than to decorate static paragraphs.

Animation is not automatically superior to static graphics. A research review
found that speed and complexity can impair understanding. Our design response
is to use clear stages and enough viewing time, and to provide playback control
where the delivery supports it. See
[Tversky, Morrison, and Bétrancourt (2002)](https://www.tc.columbia.edu/faculty/bt2158/faculty-profile/files/_Morrison_Betrancourt_AnimationCanitfacilitate.pdf).

## Narration and timing

Draft natural spoken language; equations often need a spoken paraphrase. Follow
the requested audio provider when available. Generate a representative segment
first to check pronunciation and pacing before producing the rest.

When using ElevenLabs, read credentials through the configured secret mechanism
or environment. Keep keys out of source, browser code, logs, and artifacts.
Check current API capabilities and voice availability instead of pinning model
names or prices in this skill. Its
[speech-with-timing API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps)
returns audio and character alignment; alignment can be absent. Use actual audio
durations and available alignment to synchronize scenes and captions. If absent,
use a supported alignment workflow or measured segment timing and verify it.

If the requested provider lacks credentials, finish the script and visual work
and identify the audio blocker. Use local TTS or a silent captioned version when
that is compatible with the user's request; disclose the substitution. Do not
silently replace a specifically required voice or provider.

Provide captions and a transcript with the delivery. Review generated captions
for terminology and relevant sound information, following
[W3C caption guidance](https://www.w3.org/WAI/media/av/captions/).

## Example and verification

Prompt: “Create a 90-second explainer of compound growth using geometric
animation. Compare 100 growing at 10% for two periods. Synchronize narration
with the changes and include captions and a transcript.”

Check the model's values: 100, 110, then 121. A labeled area or length representing
value must preserve the stated scale through each transformation.

Render a preview, inspect scene boundaries and representative frames, then play
the final video with sound. Verify legibility, visual scales, narration timing,
caption timing, clipping, and the ending. Deliver the rendered file and editable
source with a brief statement of the checks actually completed.

Sample the rendered video at each narrated step. Confirm that the named object,
highlight, and resulting state appear at the intended moment. Passing layout
or contrast checks alone does not verify that an explanation is synchronized.
