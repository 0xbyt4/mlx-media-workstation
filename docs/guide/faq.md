# FAQ

## Does Workstation send my data anywhere?

No, with one exception. The models run on this Mac and upload nothing. The server listens on `127.0.0.1` only. The app
uses the network for the downloads that you start on the Models screen. In an Agent chat, Web search and Read a web
page use it after you approve the call. See [Agent mode](agent-mode.md).

## Does it work on a Mac with less than 48 GB?

Partly. We tested only on a 48 GB M5 Pro. Each tool shows whether it fits your Mac: its peak plus 8 GB for macOS
must be at most the Mac's memory. For example, FLUX.3 Klein (peak 14 GB) fits a 24 GB Mac, and the local LLM (peak
18 GB) needs about 26 GB. A tool that does not fit can still run, but macOS swaps, and the job gets much slower or
stops. See [Models](models.md#read-the-memory-fit).

## Does it work on an Intel Mac?

No. The models run with MLX, which needs Apple Silicon.

## Why does only one job run at a time?

Each model needs most of the memory of a 48 GB Mac. Two jobs at once would make macOS swap. The queue runs the jobs
in order. You can reorder it in Jobs.

## Why did Chat stop answering while a job ran?

The LLM and the job share the memory. When a job does not fit next to the LLM, the LLM unloads, and a new message
waits until the job ends. See [Chat](chat.md#when-a-job-runs).

## Can I use my own models?

Not from the app. The tools are fixed in `app/tools.py`, and each one runs a pinned model revision.

## Can I use the outputs commercially?

That depends on the model's licence, not on this app. Some licences limit commercial use, for example by your
revenue. Read the summary on each model card and the table in the [README](../../README.md#model-licences). The
summary is not legal advice.

## Where are my outputs?

In `outputs/<job id>/` in the app folder. The Library shows all of them. "Show in Finder" opens the file's folder.
See [Getting started](getting-started.md#where-your-files-are).

## How do I get the same result again?

Use the same seed and the same settings. An empty seed is random, so type a seed when you want to repeat a result.

1. On the finished job, press "Use these settings". The form gets the job's settings, the seed too.
2. Add the files again.
3. Press Generate.

When the seed was empty, the job had a random seed. Fish Drama 3 and ACE-Step 1.5 write the seed of each take in its
note. To try that take again, type its seed in the Seed field and set Takes to 1. The result can differ from the
first run, because that run had no seed of its own.

## Why does a video take so long?

LTX-2.5 makes 5 s of 704x1280 video in about 3 minutes on an M5 Pro. For a faster test, pick "9:16 fast (576x1024)"
and a shorter length. The "Final-quality decoder" makes a job about 2.5x slower.

## Why is my MOSS-Image result black?

The prompt is too long. Keep it under about 400 tokens: the counter under the field shows the count. See
[Create](create.md#moss-image).

## How do I make a longer video?

Make a clip, then extend it with Extend Video. Each step adds at most 8 s. Extend the result again for more. See
[Create](create.md#extend-video).

## Can the agent delete or overwrite my files?

The file tools cannot delete a file. "Write a file" never overwrites one. "Change a file" asks before every call,
shows the change, and keeps a copy of the old file in the chat's work folder. Shell can only read the granted folder.
See [Agent mode](agent-mode.md).

## How do I use the app in a browser?

Run `.venv/bin/python start.py --browser` and open the link that it prints. Prefer the app window: in a browser, the
session cookie goes to every server on `127.0.0.1`.

## How do I update or uninstall the app?

See [Update the app](getting-started.md#update-the-app) and [Uninstall the app](getting-started.md#uninstall-the-app).
