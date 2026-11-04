# Chat

Chat lets you talk with the local LLM: Qwen3.8 27B, 4-bit. The model runs on this Mac and uploads nothing.

![A chat answer with a "Use in Text to Video" card](../images/chat.png)

The model needs about 26 GB of memory: its peak of about 18 GB plus 8 GB for macOS. It loads with the first message,
which reads about 16 GB from disk. It unloads after 10 minutes without use. See
[Settings > Jobs](settings.md#jobs) to change this time.

## Start a chat

1. Open Chat (Cmd-3).
2. Press "New chat". To pick the mode at once, open the "New chat in a mode" menu next to it.
3. On the empty chat, pick a mode: Workstation, General or Agent.
4. Type your message.
5. Press Return to send. Press Shift-Return for a new line.

The mode is fixed after the first message. A new chat starts in the mode of your last new chat.

| Mode | What it does |
|---|---|
| Workstation | Knows the app's tools and every field of their forms. It queues a job when you ask it to make something, after you approve it. When it writes a text for a tool, the answer shows a "Use in" button. |
| General | A plain assistant for any question. It does not know the app's tools. It has no internet access. |
| Agent | A general assistant that can use tools: web search, Python in a sandbox, files in a folder you grant. See [Agent mode](agent-mode.md). |

The chat profile has no Workstation mode.

## Let the chat make it

In a Workstation chat, the model can run the media tools itself. It has only the Workstation tools: it lists the
tools, queues and cancels jobs, reads their status, waits for a job, searches the Library and transcribes speech. It
has no web, file, Python or shell tools.

1. Ask for a result, for example: `Make a 30 second calm synth-pop song about rain, with lyrics.`
2. The model fills the tool's fields and calls "Queue a media job". An approval card shows the tool and its fields.
3. Press "Allow once", or "Allow in this chat" to let it queue jobs without asking in this chat.

The job card in the answer shows the job's progress and its outputs. A job that needs the chat model's memory, such
as a video, starts after the answer ends.

- When your request is unclear, or you want to read the text first, the model writes the prompt with a "Use in"
  button and queues nothing. Ask it for the prompt only when you want to change it before you run it.
- The Tools menu under the message box turns each tool on or off and ends an "Allow in this chat".
- The approval rules are the ones of Agent mode: see [Agent mode](agent-mode.md).

## Use a prompt from the chat in a tool

1. Start a chat in Workstation mode.
2. Ask for a prompt, for example: `Write a Text to Video prompt for a 5 second shot of a lighthouse at night.`
3. Under the prompt in the answer, press "Use in Text to Video".

Create opens that tool with the text in its field. Read it, then press Generate. Lyrics for ACE-Step 1.5 also turn
its Instrumental switch off, so the song is sung.

## Ask about images, documents and audio

1. Press "Attach" under the message box, or drop or paste the files on the chat.
2. Type your question.
3. Press Return.

The model reads images itself. Of a document it reads the text, and of an audio file a transcript.

| You attach | Types | Max size |
|---|---|---|
| Images | PNG, JPEG, WebP, HEIC, HEIF | 20 MB |
| Text and code | TXT, Markdown, CSV, TSV, JSON, XML, YAML, logs, subtitles and source code | 50 MB |
| Documents | PDF, Word (DOC, DOCX), RTF, OpenDocument text (ODT), HTML | 50 MB |
| Spreadsheets | Excel (XLSX), OpenDocument (ODS) | 50 MB |
| Audio | WAV, MP3, M4A, AAC, FLAC, OGG, AIFF | 200 MB |

- A message takes up to 6 files. Up to 4 of them can be images.
- The app converts a HEIC or HEIF photo (an iPhone photo) to JPEG with the macOS tool `sips`. The model and the chat
  get the JPEG.
- Each file shows as a chip with its state: "Reading...", "Transcribing...", "Read in full", "Transcribed", "Cut to
  fit" or "Failed" with the reason.
- The model sees a part of a long file only. The chip then says what the model got, for example "the first 29,850 of
  95,123 characters (pages 1 to 12 of 40)". To send another part, copy it into the message.
- Under a sent file, the file button downloads the text that the model read. The download button downloads the file.
- A scanned PDF has no text, and the app does not read text in pictures. Attach the pages as images instead.
- Pages, Numbers and Keynote files cannot be read. Export them as PDF, Word or Excel first.

An audio file is transcribed before the answer starts. The Transcribe tool (Qwen3-ASR) does this as a media job, so
it shows in Jobs too. The chat shows "Transcribing" with the progress. Stop cancels the job. When a transcription
fails, the model still answers and knows that the file has no text. Regenerate tries the transcription again.

The chat profile has no Transcribe tool, so it does not take audio. Speech recognition must be installed in Models.

In an agent chat, a document or audio file counts as untrusted content: after it, every call that sends data off
this Mac or queues a job asks you first. See [Agent mode](agent-mode.md).

## Pick a thinking level

The model can think before it answers. A higher level thinks longer and answers better, but slower.

1. Press "Thinking" under the message box.
2. Pick a level.

| Level | Thinks for at most |
|---|---|
| Off | No thinking: the fastest answer. |
| Low | About 1 minute. |
| Medium | About 3 minutes. |
| High | About 6 minutes. |
| XHigh | About 8 minutes. |
| Max | No thinking limit, up to the answer's time limit. |

- A chat keeps the last level that you used in it.
- A new chat starts with the level in [Settings > Local LLM](settings.md#local-llm) ("Thinking in a new chat").
- The times assume about 17 tokens a second, as on an M5 Pro. A slower Mac thinks for fewer words in that time.
- In an agent answer, the limit applies to each step. Steps after the first get 40% of it.

The thought shows in a block above the answer, folded. Click "Thinking" to open it. The token budgets are in
[API.md](../API.md#thinking-levels).

## Control an answer

- **Stop**: the Send button changes to Stop while the model answers. Press it to end the answer. The app keeps the
  text so far.
- **Regenerate**: under the last answer. It answers the last message again.
- **Copy**: under an answer, and on each code block. You can also select any text in the window and press Cmd-C.
- **Jump to latest**: when you scroll up while the model answers, the chat stops following the answer. Press "Jump
  to latest" over the message box to go back to the end. Scroll to the end to follow the answer again.

An answer can take at most 10 minutes. At the limit it stops and shows a note. The time that the answer waits for
memory does not count.

## When a job runs

The LLM and a media job share the memory.

- When a job fits next to the LLM, both stay in memory.
- When a job does not fit, the answers that run finish first (for at most 10 minutes). Then the LLM unloads and the
  job starts.
- A message that you send while such a job runs waits. The chat shows "Waiting for memory". The answer starts when
  the job ends, before the next queued job.
- When the queue is empty again, the LLM loads again in the background, so the next answer does not wait for the
  load. It does this only when it was in use before the job: its last request was within the idle time (10 minutes by
  default). The first answer after the job still reads the whole chat again, which takes about 1 second for each 500
  tokens.

## Manage chats

- To find a chat, type in "Search chats".
- To delete a chat, press its delete button, then press Delete. This deletes the chat and its images and files.
- To free the LLM's memory now, press the unload button in the chat header. The model loads again with the next
  message.

A chat is saved in `outputs/chats/<chat id>/`. The model sees at most 32,768 tokens of a chat. When a long chat is
full, the oldest turns go out of the model's view. They stay on the page.

## Chat only

The chat profile shows only Chat, Models and Settings. Use it on a Mac with only the local LLM installed.

To start the chat profile, do one of these:

- Run `.venv/bin/python start.py --chat`.
- Open `Workstation Chat.app`.
- Choose "Chat only" in [Settings > Profile](settings.md#profile). The change applies at once and is saved for the
  next start.

To set up a Mac only for chat:

1. Make the app's environment and `config.toml`. See [Getting started](getting-started.md#install-the-app).
2. Make the LLM environment. Run the commands that the Models screen shows on the "Local LLM environment (mlx-vlm)"
   card.
3. Start the app with `--chat`.
4. On the Models screen, download "Qwen3.8 27B (4-bit)" (about 16.1 GB).

The chat profile does not need `ffmpeg`. In the chat profile, Settings shows "Memory" in place of "Jobs".
