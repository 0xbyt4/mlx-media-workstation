# Troubleshooting

Find your symptom below and do the steps. The logs help in most cases:

- A job: "Technical details" on the job card, or `outputs/<job id>/job.log`.
- The local LLM: `outputs/llm.log`.
- The app, started from `Workstation.app` or `Workstation Chat.app`: `outputs/app.log`. Started from a terminal, the
  log is in the terminal.

## A tool says "Not installed"

The tool list shows "Not installed", the Generate button says "Install first", or a job is refused with "Not
installed: <path>".

1. Open Models (Cmd-5).
2. Find the card of the tool's runtime or weights. It shows "Not installed".
3. For a runtime, run the commands on its card in Terminal. Then press "Check again".
4. For weights, press Download.

If the model is installed in another folder:

1. Open Settings > Model paths.
2. Enter the folder where the model is. The row shows "Not found" until the path is right.
3. Press Save.
4. Restart the app.

## A model file is missing

A job fails with the title "A model file is missing". A file of the weights is not in the folder or in the Hugging
Face cache.

1. Open Models.
2. Find the model of the tool. Press Download again. Files that are complete are not downloaded again.
3. On the failed job, press "Use these settings".
4. Add the files again, then press Generate.

## Out of memory

A job fails with the title "macOS stopped the job: memory may have run out". Or a job is much slower than the time
in the tool's description. The memory meter in the sidebar shows swap.

1. Quit other apps that use a lot of memory.
2. In Chat or Settings > Local LLM, press Unload to free the LLM's memory.
3. On the failed job, press "Use these settings".
4. Pick a smaller size, a shorter length or fewer takes.
5. Press Generate.

Read the pill on the "On this Mac" card before you start. "Needs N GB" means that the tool needs a larger Mac. It can
still run, but it can take many times longer or stop. See [Models](models.md#read-the-memory-fit).

## A job fails

1. Open Jobs.
2. Read the title and the fix steps on the failed job.
3. Open "Technical details" and read the end of the log.
4. Fix the cause, press "Use these settings", add the files again and press Generate.

Some jobs are refused before they start. The message shows at the top of the form, under "The job was not started":

| Message | Fix |
|---|---|
| Add the transcript of voice reference 0 (or 1). | Fish Drama 3: type the exact words of the reference audio in its transcript field. |
| The file has no sound track. | Transcribe: pick a file with sound. |
| The voice audio must be 1.5-20.0 s long. | Voice to Video: cut the voice to 1.5-20 s. |
| The video is WxH; LTX extends only sizes that are a multiple of 64 on both sides. | Extend Video: use a clip from the video tools, or crop it. |
| The clip must be at least 1.1 s long. | Extend Video: use a longer clip, or a later "Use the clip up to (s)". |
| The music file has no audible sound. | Mix: pick another music file, or another "Start the music at (s)". |
| The video file has no picture, or it could not be read. | Mix: pick a video file. An audio file does not work as the video. |
| The music file has no sound, or it could not be read. | Mix: pick an audio file, for example WAV or MP3. |
| Could not prepare the input file: ... | The file is damaged or of a type that ffmpeg cannot read. Convert it, for example to PNG, WAV or MP4. |
| file too large (max 200 MB) | Use a smaller file. |

A Fish Drama 3 take with the note "CHECK FAILED" is not an error. The speech check found a problem in the take. Use
another take, or generate again.

## The LLM does not answer

Find what Chat or the Create form shows:

| What you see | What to do |
|---|---|
| "Qwen3.8 27B (4-bit) is not installed." | Install the "Local LLM environment (mlx-vlm)" and the "Qwen3.8 27B (4-bit)" weights in Models. |
| "... needs about 18 GB of memory plus 8 GB for the system." | This Mac has too little memory for the LLM. Chat and Improve cannot run here. |
| No Improve button under the prompt fields | The LLM is not installed, or this Mac has less than about 26 GB of memory. |
| "Waiting for memory" | A media job uses the memory. The answer starts when that job ends. Cancel the job, or wait. |
| A long wait before the first word | The first request loads about 16 GB from disk. A high thinking level also thinks before it answers. Pick a lower level for a faster answer. |
| "The model stopped unexpectedly. See outputs/llm.log." | Read the end of `outputs/llm.log`. Send the message again. |
| "The model did not load. See outputs/llm.log." | Read `outputs/llm.log`. Check the paths `qwen_python` and `qwen_model` in Settings > Model paths. |
| The answer stops with a time limit note | An answer can take at most 10 minutes. Pick a lower thinking level, or ask for a shorter answer. |

## The window does not open

| What you see | What to do |
|---|---|
| "Port 7870 is in use: the Workstation is probably already open" | The app runs already. Find its window. If no window is open, wait a few seconds: the app may still be closing. |
| A notification "Workstation is already open." | Find the open window. |
| A notification "Already open, or still closing: try again in a few seconds." | Wait a few seconds and open the app again. If it happens again, another program may use port 7870. |
| A notification "config.toml has an error; see the log." or "config.toml: ..." in the terminal | Open `config.toml` and fix the line that the log names. To start again from the defaults, copy `config.example.toml` to `config.toml`. |
| "The server did not start." | Read `outputs/app.log` or the terminal. |
| `Workstation.app` does nothing | Keep the app in the app folder: it runs the repository that it sits in. Read `outputs/app.log`. Build it again with `scripts/build_apps.sh`. |
| "Server offline" in the window | The app stopped. Start it again. |

To see every message, start the app from a terminal: `.venv/bin/python start.py`.

## A gated model does not download

The download fails with "Hugging Face refused ...: the model is gated or you are not logged in."

1. Open the model page and log in to Hugging Face:
   [FLUX.3 Klein](https://huggingface.co/black-forest-labs/flux-3-image-klein) or
   [LTX-2.5 (q8)](https://huggingface.co/dgrauet/ltx-2.5-mlx-q8).
2. Accept the licence on that page.
3. In Terminal, in the app folder, run `.venv/bin/hf auth login` once.
4. In Models, press Download again.

The login must be for the Hugging Face account that accepted the licence.

## Another download fails

| Message | What to do |
|---|---|
| "Could not reach Hugging Face. Check the network connection and try again." | Check the network. Press Download again. |
| "The disk is full. ..." | Free disk space. The app also keeps 10 GB free for macOS. |
| "The pinned revision ... is no longer on Hugging Face." | The tested revision was removed. Check the project's issues for a new version. |

Files that finished stay. The next download skips them.

## A setting has no effect

The app reads the model paths, the prompt help provider and the Agent settings only when it starts. Restart the app
after you change them, in Settings or in `config.toml`.

## Python or Shell is not in the Tools menu

The sandbox does not work on this Mac, or the Sandbox Python is not valid. Settings > Agent shows the reason. The
Sandbox Python must exist, and no other user may be able to change it or its folders. See
[AGENT.md](../AGENT.md#the-interpreter).

## The Tools menu refuses a folder

The app refuses system folders, your home folder itself, `~/Library`, program folders, temporary folders, the app's
own folder and hidden folders. Grant a subfolder, such as a project folder in Documents. See
[Agent mode](agent-mode.md#grant-a-folder).
