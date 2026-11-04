---
name: short-film
description: "Make a short film of 3-6 shots with the media tools: images, the same character in new shots, video clips, voice, music, the mix and the join. Use for a short film, a video story, a trailer or a clip with several shots."
license: MIT
compatibility: Needs the Workstation media tools (the full profile) and their models. Works offline.
metadata:
  author: mlx-media-workstation
  version: "1.0"
allowed-tools: list_workstation_tools create_job job_status
---

# Short film

Make a short film from the user's idea in stages. Each stage queues media jobs with `create_job`. Use the file
reference that `job_status` gives for each output (`library:<job id>/<file name>`) as the input of the next stage.

## Rules

- Video, voice and music jobs need the memory that the chat model holds. They start only after your answer ends. So
  queue the jobs of one stage, then end your answer. List the job ids and tell the user: "Send continue when the jobs
  are done in Jobs." Do not wait for these jobs.
- Keep one size for the whole film. Use 9:16 (images `768x1344`, videos `704x1280`) unless the user asks for another
  shape. Use 16:9 (images `1344x768`, videos `1024x576`) for a wide film. Every clip of one film must have the same
  size, or the clips cannot be joined.
- Keep a shot list in your answers: shot number, what happens, the job id of each output, and its file reference.
- Write prompts in English. Follow the field help of `list_workstation_tools`.
- Each `create_job` call asks the user first. Queue only the jobs of the stage you describe.
- When a job fails, read its error with `job_status`, fix the cause, and queue it again once. Then tell the user.

## Stage 0: check and plan

1. Call `list_workstation_tools`. Note which of these tools are installed: `image.klein`, `image.klein_edit`,
   `video.i2v`, `video.a2v`, `voice.drama3`, `music.ace`, `edit.mix`, and a tool that joins clips (for example
   `edit.concat`).
2. When a tool that a stage needs is not installed, tell the user its name before you start. Say what the film can
   still have without it (for example: no spoken lines without `voice.drama3`).
3. Write the plan: 3 to 6 shots of 4 to 6 seconds each. For each shot give the place, the action, the camera, and
   the spoken line (if any). Name one main character and describe the face, hair and clothes in one fixed sentence.
   Use that sentence in every prompt.
4. Ask the user to confirm the plan before you queue a job. Skip this question only when the user said "just make
   it".

## Stage 1: the first image

1. Queue `image.klein` for shot 1 with the fixed character sentence, the place, the light and the framing.
2. End the answer with the job id.

## Stage 2: the other shots (same character)

1. Read the shot 1 image with `job_status` and take its file reference.
2. For each other shot, queue `image.klein_edit` with `references` = [the shot 1 image] and a prompt that says
   `<ref_image_0>` is the character, what must stay the same (face, hair, clothes), and the new place and pose.
3. For a shot in a new place without the character, use `image.klein` instead.
4. End the answer with the job ids.

## Stage 3: voice lines (only for shots with speech)

1. For each spoken line, queue `voice.drama3` with `text` = the line (about 2.5 words a second, so 12 words for a
   5 s shot) and `takes` = 2.
2. End the answer. In the next stage, pick the take whose transcript matches the line (see `job_status`).

## Stage 4: the clips

1. For a shot with speech, queue `video.a2v` with `image` = the shot's image, `audio` = the chosen voice take, and
   `prompt` = who speaks and how. Use the film's video size.
2. For every other shot, queue `video.i2v` with `image` = the shot's image and a prompt of 4 to 8 sentences that
   describes what moves and how the camera moves, and every sound. Set `seconds` (4 to 6) and the film's size.
3. End the answer with the job ids. A clip takes about 2 to 4 minutes.

## Stage 5: join the clips

1. Check the clips with `job_status`: all of them must be done.
2. When a join tool is installed (for example `edit.concat`), queue it with the clips in shot order.
3. When there is no join tool, tell the user clearly: "This app has no tool to join clips." Give the file references
   in shot order, so they can join them in a video editor (for example iMovie). Then skip to stage 6 with the most
   important clip, or stop.

## Stage 6: music and the mix

1. Queue `music.ace` with `caption` = genre, mood, instruments and the use ("calm piano underscore for a short
   drama"), `instrumental` = true, and `duration` = the film's length plus 2 seconds (at least 10).
2. End the answer. In the next stage, pick one of the two takes.
3. Queue `edit.mix` with `video` = the joined film (or the clip) and `music` = the chosen take. Keep `under` = 14 for
   a film with speech; use 10 for a film without speech.
4. `edit.mix` needs no model memory and takes a few seconds. Wait for it with `wait_for_job` (or read it with
   `job_status`), then give the user the final file reference.

## The end

Give the user a short summary: the shots, the final file, and which steps were skipped and why.
