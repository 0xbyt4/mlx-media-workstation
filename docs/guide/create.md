# Create

On the Create screen you pick a tool, fill in its form and start a job. Jobs run one at a time, so each model gets
the memory it needs.

![The Create screen with the Text to Video form](../images/create.png)

## Start a job

1. Open Create (Cmd-1).
2. Click a tool in the tool list. Each row shows the tool's peak memory, "Tight" or "Needs N GB", or "Not
   installed". After the first job of the tool on this Mac, it shows the time too.
3. Fill in the fields. A field marked "(required)" must have a value.
4. Optional: open "Advanced settings" for the other fields. Its summary names them.
5. Read the "On this Mac" card. See [The "On this Mac" card](#the-on-this-mac-card).
6. Press Generate, or press Cmd-Return.

When a job runs already, the button says "Add to queue". When the tool's model is not installed, the button says
"Install first".

The job goes to the queue. A toast offers "View in Jobs". See [Jobs and Library](jobs-and-library.md).

Other controls of the form:

- **Seed**: empty means random. The same seed and settings give the same result.
- **Reset to defaults**: puts every field of the tool back to its default.
- **Last result from this tool**: the newest finished job of the tool. "Use its settings" fills the form with its
  settings.
- **File fields**: press "Choose" or drop a file on the field. A file can be at most 200 MB.

The form remembers its values per tool in this window. It does not remember the chosen files after a restart.

## The "On this Mac" card

The card next to Generate shows these facts before you start:

- **Peak**: the highest memory that the tool's model uses, against the memory of this Mac. A pill says "Fits",
  "Tight" or "Needs N GB". See [Models](models.md#read-the-memory-fit).
- **Time**: the median time of the finished jobs of this tool on this Mac. Before the first job, it says "No time
  yet". The tool's description gives the time that we measured.
- **Queue**: "Starts now", or how long the jobs before it take.
- **Chat pauses**: the local LLM unloads while this job runs, because both do not fit in the memory together. Prompt
  help and Chat wait until the job ends.
- **Saves to**: `outputs/<job id>/`.

The peaks were measured on a 48 GB M5 Pro. The app plans with one peak per model: the highest peak of the tools that
use it. So the card shows 35 GB for every LTX-2.5 tool and 39 GB for Fish Drama 3.

## Improve a prompt

"Improve" is under the main text field of most tools. It asks the local LLM to turn your short idea into the full
text that the tool needs. It follows the rules in [PROMPTING.md](../PROMPTING.md).

1. Type a short idea in the field, for example `a fox in snow at dawn`.
2. Press Improve.
3. Read the new text and the note under it.
4. To get your own text back, press Undo. To get the new text again, press Redo.

- Improve sees the images of the form (for Image to Video, the start image). For a video, it sees the frame where
  the clip ends. It never gets the audio.
- The first request loads the LLM. This reads about 16 GB from disk.
- When a job runs that does not fit next to the LLM, Improve waits. It runs when that job ends, before the next
  queued job. Your text stays in the field. Press Cancel to stop the wait.
- On a Mac with less than about 26 GB of memory, the Improve buttons do not show.

## The tools

The times and peaks below come from an M5 Pro with 48 GB. "Peak" is the number that the app plans with.

| Group | Tool | What it makes | Time | Peak |
|---|---|---|---|---|
| Image | MOSS-Image | Photoreal people and scenes | about 4 min at 768x1344, 8 min at 1088x1920 | 22 GB |
| Image | FLUX.3 Klein | Fast images, good text in images | about 40 s | 14 GB |
| Image | Klein Edit (references) | A new image from 1-10 reference images | about 1-4 min | 14 GB (not measured with many references) |
| Voice | Fish Drama 3 | Expressive speech and dialogue | about 2x real time | 39 GB |
| Voice | Transcribe | Text from speech | 40 s of speech in about 2 s | 3 GB |
| Music | ACE-Step 1.5 | Music, instrumental or with lyrics | two 20 s takes in about 30 s | 23 GB |
| Video | Text to Video | A video with sound from a prompt | 5 s at 704x1280 in about 3 min, at 576x1024 in about 2 min | 35 GB |
| Video | Image to Video | A video from a start image | 4 s in about 2 min | 35 GB |
| Video | Voice to Video (talking) | A person from an image speaks your audio | 7 s in about 3.5 min | 35 GB |
| Video | Extend Video | More seconds after a clip | 5 s more in about 2.5 min | 35 GB |
| Edit | Mix (video + music) | Music under a video's own sound | a few seconds | under 1 GB (ffmpeg only) |

### MOSS-Image

Photoreal, aesthetic people and scenes. It is weak at text in images.

| Field | Default | Notes |
|---|---|---|
| Prompt | | One paragraph of about 100-200 words, in English. A counter shows "about N of 400 tokens". |
| Size | 9:16 (768x1344) | Also 9:16 large (1088x1920), 4:5 (896x1152), 1:1 (1024x1024), 16:9 (1344x768). |
| Seed | random | |
| Negative prompt (advanced) | `nsfw, worst quality, bad anatomy, watermark, jpeg artifact` | |
| Steps (advanced) | 50 | 10 to 100. The model's guide recommends 50 or more. |
| Guidance (CFG) (advanced) | 4.0 | 1 to 8. Use 3-6: higher for short prompts. |

Prompt tips:

- Describe the subject, outfit, pose, framing, lens, light, place and style. Skip empty quality tags.
- Keep the prompt under about 400 tokens. A longer prompt gives a black image (mflux bug 920).
- For a full-body 9:16 image with small faces, use 1088x1920.
- For text in the image, use FLUX.3 Klein.
- For the same character in many images, keep the seed and the words that describe the person. Change only the pose,
  framing and place.

### FLUX.3 Klein

Fast images. It is good at text in images and at following the prompt.

| Field | Default | Notes |
|---|---|---|
| Prompt | | Put any text to render in double quotes, for example a sign that reads "PUSH". |
| Size | 9:16 (768x1344) | The same sizes as MOSS-Image. |
| Seed | random | |
| Steps (advanced) | 16 | 4 to 50. |

Prompt tips:

- Write in this order: medium, subject and action, place, light (source, direction, colour), framing (shot size,
  angle, lens).
- Write a full caption, not a one-line command.
- Klein has no negative prompt. Describe what you want instead.

### Klein Edit (references)

A new image from 1-10 reference images: the same person in a new place, or objects combined.

| Field | Default | Notes |
|---|---|---|
| Reference images | | 1 to 10 images. |
| Prompt | | Name the references `<ref_image_0>`, `<ref_image_1>` and so on, in upload order. |
| Size | 9:16 (768x1344) | The same sizes as MOSS-Image. |
| Seed | random | |

Prompt tips:

- Put the background first.
- Give each reference one role. Say what must stay the same.
- The model shrinks each reference to about 1 megapixel. A small face in a full-body image loses its identity. Add a
  head-and-shoulders reference for faces.

### Fish Drama 3

Expressive speech and dialogue. Speech recognition (Qwen3-ASR) checks each take.

| Field | Default | Notes |
|---|---|---|
| Script | | The text to speak. See the tips below. |
| Takes | 2 | 1 to 4. Each take uses another seed. Keep the best. |
| Voice reference, speaker 0 (advanced) | | An audio file: 10-20 s, one clean voice, 44.1 kHz or better. |
| Transcript of reference 0 (advanced) | | Required when you give the reference. |
| Voice reference, speaker 1 (advanced) | | The same, for the second speaker. |
| Transcript of reference 1 (advanced) | | Required when you give the reference. |
| Temperature (advanced) | 1.0 | 0.3 to 1.5. |
| Top p (advanced) | 0.9 | 0.1 to 1.0. |
| Top k (advanced) | 30 | 5 to 200. |

The output is `take1.wav`, `take2.wav` and so on. The note of each take says "checked ok" or "CHECK FAILED" with the
reason. The check finds a wrong length, a silence of 3 s or more, or more than 15% of the words not heard. A take that
fails the check is made once more with another seed.

Prompt tips:

- Mark the speakers with `<|speaker:0|>` and `<|speaker:1|>`. Write the whole exchange as one text.
- Use 2-4 short turns of 5-20 words each, under about 300 bytes per take.
- Put cues in brackets where they happen, if possible mid-sentence: `[sighs]`, `[nervous laugh]`. A cue lasts until
  the next one.
- Make pauses with "...", a dash, a comma or a new short sentence.
- Do not start a take with a one-word sentence or a cue. The take often stops there.
- Do not ask for `[long pause]` or screams. The model can get stuck in silence or noise.
- The app removes line breaks.
- Keep temperature 1.0 and top p 0.9. Low values make silent takes more likely.
- For two cloned voices, pick clearly different voices. If speaker 1 drifts, swap the two references.

### Transcribe

Speech to text with Qwen3-ASR 0.6B. It writes one line of text for about every 30 s, with its time.

| Field | Default | Notes |
|---|---|---|
| Audio or video | | The file's sound track is transcribed. Music under the voice makes the text worse. |
| Language | Detect (per 30 s part) | Or one of 30 languages. |

The output is `transcript.txt`: a `Language:` line, then one line per part with its time, for example
`[0:00-0:28] ...`. It has no times for single words.

### ACE-Step 1.5

Music from a description, instrumental or with lyrics.

| Field | Default | Notes |
|---|---|---|
| Description | | Genre, mood, instruments, tempo feel and use. |
| Instrumental | on | Turn it off for a song with lyrics. |
| Lyrics | | Sung only when Instrumental is off. Use `[verse]` and `[chorus]` tags. |
| Duration (s) | 30 | 10 to 240. |
| BPM (advanced) | automatic | 30 to 300. |
| Key (advanced) | automatic | For example `D minor`. |
| Takes (advanced) | 2 | 1 to 4. |
| Seed (advanced) | random | |

Lyrics need Instrumental off:

- When you type or paste lyrics while Instrumental is on, the form turns Instrumental off. A note says so. Press Undo
  in the note to turn it on again. After Undo, or when you turn Instrumental on yourself, the form leaves it on until
  you clear the lyrics.
- "Use in ACE-Step 1.5 › Lyrics" in a chat also turns Instrumental off. Undo in its note puts the form back.
- With Instrumental on and text in Lyrics, Generate marks the Lyrics field. The server also refuses such a job, so
  the app never ignores lyrics.

The output is `take1.wav`, `take2.wav` and so on. Each note gives the seed, the key and the BPM.

Example description: `tense escape room thriller underscore, ticking clock`.

### Text to Video

LTX-2.5 makes a video with sound from a prompt.

| Field | Default | Notes |
|---|---|---|
| Prompt | | 4-8 sentences. See the tips below. |
| Size | 9:16 (704x1280) | Also 9:16 fast (576x1024), about 2x faster with softer faces; 16:9 (1024x576); 1:1 (768x768). |
| Length (s) | 5 | 1 to 10. |
| Generate sound | on | |
| Seed | random | |
| Final-quality decoder (advanced) | off | Slightly cleaner skin and faces, about 2.5x slower in total. |

The output is `video.mp4` at 24 frames a second.

Prompt tips for all LTX-2.5 tools:

- Write one paragraph of 4-8 sentences in the present tense: shot, place and light, action with real verbs,
  character, camera and sound.
- Show emotion with physical cues, not with words such as "sad".
- Say how the frame looks at the end, relative to the subject. "Static frame, locked-off camera, no zoom" stops a
  drifting zoom.
- Name every sound. Put dialogue in quotes. Plan about 2.5 words a second.
- Use the same words for a character in every shot.
- Faces need pixels. When the face matters, frame a medium shot or closer, or use 704x1280.
- Make the clip a little longer and trim the end. The last second can flash or blur.

### Image to Video

LTX-2.5 animates a start image.

| Field | Default | Notes |
|---|---|---|
| Start image | | The app scales and crops it to the video size. Use the same aspect ratio. |
| Prompt | | Describe what changes, not what the image shows. |
| Size, Length (s), Generate sound, Seed, Final-quality decoder | | As in Text to Video. |

To make a video from an image:

1. Make an image with MOSS-Image or FLUX.3 Klein in the same aspect ratio as the video, for example 9:16.
2. On the finished job, press "Send to" and pick Image to Video. Create opens with the image in "Start image".
3. Describe the motion and the sound, for example: `She turns her head toward the window and smiles. Rain taps on
   the glass. Static frame, locked-off camera, no zoom.`
4. Press Generate.

A prompt that describes another place makes the model cut or fade.

### Voice to Video (talking)

A person from an image speaks your audio, with the lips in sync. The clip is as long as the audio.

| Field | Default | Notes |
|---|---|---|
| Person image | | Front-facing and sharp. The face must be large: waist-up works best. |
| Voice audio | | One speaker, a clean voice, 1.5-20 s. |
| Who and how | | Who speaks, where, and the exact words in quotes. |
| Size | 9:16 fast (576x1024) | The video sizes. |
| Added camera/face wording (advanced) | a fixed text | The app adds this text to your prompt: a static camera, natural lips, sharp eyes. |
| Seed | random | |

To make a talking video:

1. Make the voice with Fish Drama 3. Keep one speaker per clip.
2. Make the person image with MOSS-Image, framed like the final shot.
3. On the voice job, press "Send to" and pick Voice to Video. Do the same with the image.
4. In "Who and how", write who speaks and the exact words. For example: `A medium shot of a woman in a navy uniform
   in a dim museum gallery, facing the lens. She speaks calmly, saying: "We close in ten minutes."`
5. Press Generate.
6. Try 2-3 seeds and keep the best.

The face should be at least 150-200 pixels wide in the video. A smaller face gets blurred eyes.

### Extend Video

LTX-2.5 continues a clip. It picks up the clip's last second (picture and sound) and adds new seconds. The result is
the whole clip plus the new part.

| Field | Default | Notes |
|---|---|---|
| Video | | A clip from the video tools. Both sides must be a multiple of 64. |
| What happens next | | 4-8 sentences. Repeat the words for the person, outfit and place. |
| New seconds | 5 | 1 to 8. For more, extend the result again. |
| Use the clip up to (s) (advanced) | the whole clip | The new part starts there. |
| Seed | random | |

To extend a clip:

1. On a finished video job, press "Send to" and pick Extend Video.
2. Describe only what happens next. Say how the frame ends, but do not ask the subject to fill the frame. That made
   the camera push in.
3. To carry a voice into the new part, set "Use the clip up to (s)" just after the spoken line. A silent cut loses
   the voice.
4. Press Generate.

The clip must be at least about 1.1 s long. The longest step we measured was 8 s, at a peak of about 35 GB.

### Mix (video + music)

Puts music under a video's own sound with ffmpeg. It needs no model. The picture is copied, not encoded again.

| Field | Default | Notes |
|---|---|---|
| Video | | Its own sound (the voice) stays. |
| Music | | Looped when it is shorter than the video, cut when it is longer. |
| Music under the voice (dB) | 14 | 3 to 30. 10 is lively, 14 is normal, 20 is a quiet background. |
| Duck under speech | on | The music goes down a few more dB while someone speaks. |
| Fade in (s) (advanced) | 0.5 | |
| Fade out (s) (advanced) | 1.5 | |
| Start the music at (s) (advanced) | 0 | Skips the start of the music track. |

The output is `mixed.mp4`. The final mix is at -14 LUFS. When the video has no sound, the music goes to -16 LUFS
before the final mix.

To put music under a video:

1. Make the music with ACE-Step 1.5, as long as the video or longer.
2. On the video job, press "Send to" and pick Mix. Do the same with the music take.
3. Press Generate.

## Use outputs in other tools

"Send to" on a job card or in the Library puts an output into a file field of another tool:

| Output | Send to |
|---|---|
| Image | Image to Video, Voice to Video (talking), Klein Edit (references) |
| Audio | Voice to Video (talking), Mix (video + music), Fish Drama 3 (voice reference of speaker 0 or 1) |
| Video | Extend Video, Mix (video + music) |

"Use these settings" fills the tool's form with the settings of a job. It does not bring back the files: add them
again.
