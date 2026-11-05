# Chat

Chat lets you talk with the local LLM: Qwen3.8 27B, 4-bit. The model runs on this Mac and uploads nothing.

![A chat answer with a "Use in Text to Video" card](../images/chat.png)

The model needs about 26 GB of memory: its peak of about 18 GB plus 8 GB for macOS. It loads with the first message,
which reads about 16 GB from disk. It unloads after 10 minutes without use. See
[Settings > Jobs](settings.md#jobs) to change this time.

## Start a chat

1. Open Chat (Cmd-3).
2. Press "New chat". To pick the preset at once, open the menu next to it.
3. On the empty chat, pick a preset: General, Agent or Workstation.
4. Type your message.
5. Press Return to send. Press Shift-Return for a new line.

A preset sets what the chat can do: its system prompt and its tools. A new chat starts with the preset of your last new
chat.

The chat also fixes its instructions for the model at the first message: the tools it shows the model, the date and
whether this Mac has a network. Then the model does not read the whole chat again before each answer, so answers
start sooner. A chat keeps its preset and its tools. To use other tools, a new date or a network that came back,
start a new chat: the Tools menu of a started chat offers "Start a new chat with these tools". A folder you grant
later reaches the model as a short note with your next message.

| Preset | In short | What it does |
|---|---|---|
| General | No tools, fastest | A plain assistant for any question. It does not know the app's tools. It has no internet access. |
| Agent | Files, code, web | A general assistant that can use tools: web search, Python in a sandbox, files in a folder you grant. See [Agent mode](agent-mode.md). |
| Workstation | Media jobs | Knows the app's tools and every field of their forms. It queues a job when you ask it to make something, after you approve it. When it writes a text for a tool, the answer shows a "Use in" button. |

The chat profile has no Workstation preset.

### Change the preset or the tools of a chat

The model gets the chat's tools with the first message. The app keeps them for the whole chat, so the model can reuse
what it already read (its prompt cache). A change in the middle of a chat would make it read the whole chat again.

- On an empty chat, the preset and the tools change at once.
- After the first message, the chat keeps its preset and its tools.

To use other tools or another preset, start a new chat with them:

1. Open the preset menu in the chat header, or open the Tools menu under the message box.
2. Pick another preset ("New Agent chat"), or change the switches in the Tools menu.
3. In the Tools menu, press "Start a new chat with these tools". Press Undo to keep the tools as they are.

The new chat gets the chat's folder and thinking level. It does not get the messages or the "Allow in this chat"
approvals. The chat before stays as it is, unless it is a temporary chat.

In a chat that has messages, these changes still apply at once: "Revoke" of an "Allow in this chat" approval, and the
grant or revoke of the folder.

The Tools menu shows the tools in two groups:

- **Always loaded**: the model sees these tools in every step.
- **Found by search**: the model finds these tools with a search when it needs one. Until then they take no room in its
  prompt.

### Temporary chat

A temporary chat is not kept.

1. Open the menu next to "New chat".
2. Pick "Temporary chat".

You can also turn on "Temporary chat" on an empty chat, under the presets.

- The chat header and the chat list show a clock mark and "Temporary".
- The app deletes the chat when you leave it: when you open another chat or start a new one. An answer that still runs
  stops first.
- When the app could not delete it (you closed the window, or the app quit), it deletes the chat when the app starts
  again.
- Memory and the model's search of past chats skip a temporary chat. "Search chats" in the chat list still finds
  it until the app deletes it.
- A new chat that you start from a temporary chat (another preset or other tools) is temporary too.

## Let the chat make it

In a Workstation chat, the model can run the media tools itself. It has only the Workstation tools: it lists the
tools, queues and cancels jobs, reads their status, waits for a job, searches the Library, transcribes speech and
reads the text in a Library image (OCR). It has no web, file, Python or shell tools.

1. Ask for a result, for example: `Make a 30 second calm synth-pop song about rain, with lyrics.`
2. The model fills the tool's fields and calls "Queue a media job". An approval card shows the tool and its fields.
3. Press "Allow once", or "Allow in this chat" to let it queue jobs without asking in this chat.

The job card in the answer shows the job's progress and its outputs. A job that needs the chat model's memory, such
as a video, starts after the answer ends.

- When your request is unclear, or you want to read the text first, the model writes the prompt with a "Use in"
  button and queues nothing. Ask it for the prompt only when you want to change it before you run it.
- The Tools menu under the message box turns each tool on or off and ends an "Allow in this chat". After the first
  message, other tools start a new chat (see [Change the preset or the tools of a chat](#change-the-preset-or-the-tools-of-a-chat)).
- The approval rules are the ones of Agent mode: see [Agent mode](agent-mode.md).

## Use a prompt from the chat in a tool

1. Start a chat with the Workstation preset.
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
- A scanned PDF has no text layer, so the chat cannot read it as a document. Read it with Read Text (OCR) on the
  Create screen (see [Create](create.md#read-text-ocr)), or attach the pages as images.
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
- Before the LLM unloads, it saves what it read of the last chat (its prompt cache) to
  `outputs/llm-cache.safetensors`. This takes about 1 second. The job waits for the save for at most 10 seconds.
- A message that you send while such a job runs waits. The chat shows "Waiting for memory". The answer starts when
  the job ends, before the next queued job.
- When no model job is queued, the LLM loads again in the background, so the next answer does not wait for the
  load. It does this only when it was in use before the job: its last request was within the idle time (10 minutes by
  default).
- The LLM reads the saved prompt cache back, so the next answer in that chat reads only your new message. Without
  the saved cache, the LLM reads the whole chat again: about 1 second for each 500 tokens.

The app keeps the saved prompt cache of one chat only, and uses it one time. It deletes the cache when you delete
that chat, when the LLM weights change and when the cache does not match the chat. A cache that does not match never
changes an answer: the LLM then reads the whole chat again.

## Manage chats

- To find a chat, type in "Search chats".
- A chat whose answer waits for your approval shows a lock and "Waits for your approval" in the chat list. Open it
  to answer the card.
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
