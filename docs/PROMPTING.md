# Prompting guide

What works for each model, from the models' own docs, community reports and our tests on an M5 Pro with 48 GB
(October 2026).
Tags: [O] official docs or code, [P] the LTX MLX port's docs, [V] checked in the local code, [C] community report,
[T] our test, [I] inference, not yet tested.

## Fish Drama 3 (voice)

Do:
- Cues are free text in brackets, placed where they happen, mid-sentence if possible: `[nervous laugh]`, `[sighs]`,
  `[forced smile that cracks]`. A cue lasts until the next one. Paired word tags exist: `<emphasis>word</emphasis>`,
  `<whisper>`, `<soft>`, `<slow>`. [O]
- Write the whole exchange as one text with `<|speaker:0|>` / `<|speaker:1|>`; 2-4 short turns (5-20 words each),
  under about 300 bytes per take. One pass sounds more natural than line-by-line clones. [O][T]
- Make pauses with "...", a dash, a comma or a new short sentence. [O]
- Sampling: temperature 1.0, top_p 0.9 (Fish defaults). Low temperature/top_p makes silent takes more likely; a higher
  top_k (e.g. 100) may reduce them but can blur the voices. [O][C]
- References: 10-20 s, one clean voice, 44.1 kHz, exact transcript, recorded rather than generated. Two cloned voices
  often merge: pick clearly different voices, swap the slot order if speaker 1 drifts. [O][C]
- Render 2-3 takes and keep the best; check each (length, silence, read-back). [O][T]

Don't:
- Open a take with a one-word sentence ("Claire.") or with a cue/filler: it often stops there and stays silent. [O][C][T]
- Put line breaks in the text (they end a turn early in mlx-audio 0.6.1). [C]
- Ask for `[long pause]` or screams: the model can get stuck in silence or noise and drop the next line. [T]

## MOSS-Image (people, scenes)

- English prose, 120-250 tokens; describe subject, setting, framing, light, materials, palette, mood. Skip empty quality
  tags. **Stay under about 400 tokens**: longer prompts (and JSON layouts) hit mflux bug #920 and give a black image. [O][C][T]
- 50 steps, CFG 3 (long prompts) to 6 (short), negative `nsfw, worst quality, bad anatomy, watermark, jpeg artifact`. [O]
- 1-2 MP; for 9:16 use 1088x1920 when faces are small (full body). Trained at about 1 MP. Plain grey backdrops can show
  faint vertical banding. [O][C]
- Text: exact words in quotes on their own lines, name the surface; still unreliable: do titles in post or with Klein. [O][T]
- Same character in many shots: keep the seed and the identity paragraph word for word, change only pose/framing/place. [I]

## FLUX.3 Klein (fast, text, edits)

- Order: medium, subject and action, setting, light (source, direction, colour), framing (shot size, angle, lens). Write
  a full caption, not a one-line edit. No negative prompts or guidance (distilled); describe what you want instead. [O]
- Text in quotes is spelled well. [T]
- Edit: 1-10 references, named `<ref_image_0>`, `<ref_image_1>`, ... in the prompt (0-indexed in mflux). Put the
  background first (it sets the size). Give each reference one role; say what must stay unchanged. Each reference is
  shrunk to about 1 MP, so a tiny face in a full-body card loses identity: give a head-and-shoulders reference too. [O][V]

## LTX-2.5 (video)

- One paragraph, 4-8 present-tense sentences: shot, place and light, action with real verbs, character (the same
  identifying words in every shot), camera, sound. Show emotion with physical cues, not labels. [O]
- Camera: say how the frame looks at the end, relative to the subject ("pushes in while she stays in place; her face
  fills the frame"). "Static frame, locked-off camera, no zoom" stops drifting zooms. [O][T]
- Sound: name every sound, dialogue in quotes; budget about 2.5 words a second; write pauses ("a beat of silence"). [O][C]
- Image to video: describe what changes, not what the image shows; same aspect ratio; describing another place makes
  the model cut or fade. [O][T]
- Voice to video (a2v): one speaker per clip, clean vocals, front-facing sharp image framed like the final shot, the
  exact transcript in quotes; faces should be at least 150-200 px wide (waist-up), or eyes blur. Try 2-3 seeds. [O][C][T]
- Render a little longer and trim the end (the last second can flash or blur). [C]
- Extend: the model picks up the clip's last second of picture and sound. Repeat the person, outfit and place words
  of the first clip; describe only what happens next. Don't ask for an end framing that fills the frame: it caused a
  push-in despite "no zoom". To carry a voice, cut the clip just after the line; a silent last second loses it. [T]
- Not available in our build: `enhance` (Gemma only), `--nag` / negative prompt on a2v. `--dfr` (sharper faces) needs
  the gated Pixel-Spatial-Upscaler LoRA and about 40 GB. [P]

## Sources

Drama 3: huggingface.co/fishaudio/drama-3, docs.fish.audio/features/text-to-speech/drama-3,
recipes.vllm.ai/fishaudio/drama-3, github.com/Blaizzy/mlx-audio (docs/models/tts/fish-drama3.md, PR 1055),
github.com/fishaudio/fish-speech issues 1356-1358, sglang-omni PRs 2810/2890.
MOSS/Klein: github.com/OpenMOSS/MOSS-Image (USERGUIDE.md, prompt-enhancer skill), arxiv.org/html/2610.23374,
github.com/filipstrand/mflux issue 920, docs.bfl.ml FLUX.3 guides, docs.comfy.org/tutorials/flux/flux-3-klein.
LTX: docs.ltx.io prompting, image-to-video and audio-to-video guides, huggingface.co/Lightricks/LTX-2.5,
github.com/dgrauet/ltx-2-mlx (docs/PIPELINES.md, issues 34/164/188), github.com/Lightricks/LTX-2 issues 11/59/148/320.
