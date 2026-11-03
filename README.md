# MLX Media Workstation

Workstation is a local app for open image, voice, music and video models on Apple Silicon Macs. You pick a tool, fill
in a form and press Generate. A local language model helps you write prompts and answers questions in a chat. Every
model runs on your Mac with [MLX](https://github.com/ml-explore/mlx). The models upload nothing and download nothing
at run time. Only the web tools of an Agent chat send data off the Mac, and only after you approve the call.

![The Create screen with the Text to Video form](docs/images/create.png)

The app does not include the models. You install each model you want: the Models screen downloads the weights, and
you make the model environments with the commands in [Install the models](#install-the-models). The app runs each
model as a separate process. A tool whose model is not installed refuses its jobs and names the missing path; the
other tools keep working.

## The app

The sidebar has six sections. Cmd-1 to Cmd-6 open them. The chat profile shows only Chat, Models and Settings
(Cmd-1 to Cmd-3); see [Chat only](#chat-only).

- **Create**: pick a tool, fill in its form and press Generate. The "On this Mac" card shows the tool's peak memory,
  whether it fits this Mac, the expected time and the wait in the queue. "Improve" under a prompt field asks the local
  LLM to rewrite your draft for that tool. Undo brings your own text back.
- **Jobs**: the running job with its progress, the queue, and the finished jobs with their results. You can move a
  queued job up or down. "Use these settings" fills the form again. "Send to" puts an output into a file field of another tool.
- **Chat**: talk with the local LLM. You can attach up to 4 images to a message and turn on thinking. A new chat is in
  Workstation, General or Agent mode (the chat profile has no Workstation mode). In Workstation mode the model knows
  the app's tools, and when it writes a prompt for a tool, the answer shows a "Use in" card that copies the text into
  that tool's form. General mode is a plain assistant. In Agent mode the model can call tools: search the web, read a
  web page, read files in a folder you grant, run Python in a sandbox, search the Library and queue jobs. A call that
  sends data off the Mac, queues a job or runs code shows an approval card first. The Tools menu turns tools on or off
  and grants the folder (`docs/AGENT.md`). The mode is fixed after the first message.
- **Library**: every output of the finished jobs, with search and filters. A card opens a larger preview with "Use
  these settings", "Send to", "Show in Finder", download and delete.
- **Models**: what each tool needs, what is installed, the download size, the peak memory and whether it fits this
  Mac. It downloads weights one at a time from a queue. When models are missing, a setup card takes you through three
  steps: pick the models, accept the licences of the gated ones, and download.
- **Settings**: the profile, the install paths in `config.toml`, the prompt help provider, the job options, the
  agent's sandbox Python and web search sources, and facts about the app. A path or agent change takes effect after a
  restart. The profile and the job options apply at once: keep the Mac awake while jobs run, show a macOS
  notification when a job is done or failed, and unload the local LLM after a chosen idle time.

| Jobs | Chat |
|---|---|
| ![The Jobs screen with a finished Text to Video job](docs/images/jobs.png) | ![A chat answer with a "Use in Text to Video" card](docs/images/chat.png) |

| Models | Library |
|---|---|
| ![The Models screen with the setup card](docs/images/models.png) | ![The Library screen with images, audio and a video](docs/images/library.png) |

## Tools

| Group | Tool | Model | Time and peak memory (M5 Pro, 48 GB) |
|---|---|---|---|
| Image | MOSS-Image | MOSS-Image SFT (mflux, 8-bit) | about 4 min at 768x1344; 20-22 GB |
| Image | FLUX.3 Klein | FLUX.3 Klein (mflux, 8-bit) | about 40 s; 12-14 GB |
| Image | Klein Edit | FLUX.3 Klein with 1-10 reference images | about 1-4 min |
| Voice | Fish Drama 3 | Fish Audio Drama 3 (mlx-audio, 4-bit); Qwen3-ASR checks each take | about 2x real time; 30-39 GB |
| Music | ACE-Step 1.5 | ACE-Step 1.5 turbo (MLX DiT and LM) | two 20 s takes in about 30 s; about 23 GB |
| Video | Text to Video | LTX-2.5 distilled (ltx-2-mlx, q8), with sound | 5 s at 704x1280 in about 3 min, at 576x1024 in about 2 min; 25-29 GB |
| Video | Image to Video | LTX-2.5 from a start image | 4 s in about 2 min |
| Video | Voice to Video | LTX-2.5 audio-to-video: a person from an image speaks the audio | 7 s in about 3.5 min; about 28 GB |
| Video | Extend Video | LTX-2.5 extend: continues a clip from its last second | 5 s more in about 2.5 min; 29-35 GB |
| Edit | Mix | ffmpeg only: music under a video's own sound, ducked, -14 LUFS | a few seconds |

The app plans with one peak per model: the highest peak of the tools that use it. So the UI shows 35 GB for every
LTX-2.5 tool and 39 GB for Fish Drama 3 and its speech check, which can be more than the range in this table.

Prompt help ("Improve") and Chat use the local LLM: Qwen3.8 27B, 4-bit (`mlx-community/Qwen3.8-27B-4bit`, run with
[mlx-vlm](https://github.com/Blaizzy/mlx-vlm)). It is a download of about 16 GB and peaks at about 17-18 GB. It loads
on the first request and unloads after 10 minutes without use (Settings > Jobs changes this).

`docs/PROMPTING.md` has the prompt rules that worked for each model.

## Memory

Jobs run one at a time, because each model needs most of the memory of a 48 GB Mac. We tested only on a 48 GB M5
Pro.

The app keeps 8 GB for macOS and other apps. A model fits a Mac when its peak memory plus this 8 GB reserve is at
most the Mac's memory. For example, the local LLM (peak 18 GB) needs a Mac with about 26 GB. On a Mac with less
memory than a tool needs, macOS swaps, and the job gets much slower or fails.

The local LLM shares the memory with the jobs:

- If a job fits next to the LLM, both stay in memory.
- If a job does not fit next to it, the answers in progress finish first (for at most 10 minutes). Then the LLM
  unloads and the job starts.
- A prompt help or chat request sent while such a job runs waits. It runs when the job ends, before the next job
  starts.

## Requirements

- An Apple Silicon Mac. We built and tested the app on macOS 26 with an M5 Pro and 48 GB of memory.
- [Homebrew](https://brew.sh) with `ffmpeg`, `python@3.12`, `python@3.14` and `hf` (the Hugging Face command line):
  `brew install ffmpeg python@3.12 python@3.14 hf`.
- [uv](https://docs.astral.sh/uv/) to make the Python environments: `brew install uv`.
- About 160 GB of free disk space for all the models and their environments, less if you install only some of them.
  The Models screen also keeps 10 GB free for macOS when it downloads.

## Start the app

1. Clone the repository and make the app's own environment:

   ```sh
   git clone https://github.com/0xbyt4/mlx-media-workstation.git
   cd mlx-media-workstation
   uv venv --python /opt/homebrew/bin/python3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ```

2. Copy `config.example.toml` to `config.toml`. If you install the models in a folder other than `~/mlx-media`,
   change the paths in `config.toml` (a relative path counts from the repository folder). You can also change them
   later on the Settings screen.
3. Start the app:

   ```sh
   .venv/bin/python start.py            # opens the app window
   .venv/bin/python start.py --browser  # server only: open the link it prints in a browser
   .venv/bin/python start.py --chat     # the chat profile: Chat, Models and Settings only
   ```

4. Install one or more models (next section).

The app uses port 7870. If the port is in use, `start.py` asks the app on that port for its profile:

- If the app there shows the other profile, `start.py` opens only a window on that app, in the profile you asked
  for. It does not start a second server, so the local LLM loads only once. Closing this window does not stop the
  app. With `--browser`, `start.py` prints the link of that window and stops.
- In all other cases (the same profile, or a port that another program uses), `start.py` says so and stops.

Closing the app's own window, or Ctrl-C in `--browser` mode, cancels the queue and stops every model process. A
window that `start.py` opened on a running app then shows "Server offline".

To start the app from Finder or the Dock, build `Workstation.app` in the repository folder and keep it there:

```sh
osacompile -o Workstation.app docs/launcher.applescript
```

## Chat only

You can use Workstation only to chat with the local LLM, without the image, voice, music and video tools. The chat
profile shows Chat, Models and Settings, and opens on Chat. It needs:

- A Mac with about 26 GB of memory or more: the LLM's peak of about 18 GB plus the 8 GB reserve.
- About 16 GB of disk for the Qwen3.8 27B weights, plus the two Python environments.
- Homebrew `python@3.12` and `uv`. The chat does not use `ffmpeg`.

Do these steps:

1. Make the app's environment and `config.toml` (steps 1 and 2 of [Start the app](#start-the-app)).
2. Make the LLM environment `venv-qwen` (step 1 of [Local LLM](#local-llm-qwen38-27b-mlx-vlm)).
3. Start the app in the chat profile:

   ```sh
   .venv/bin/python start.py --chat
   ```

4. On the Models screen, download the Qwen3.8 27B weights. In the chat profile, the setup card shows only the LLM
   environment and its weights. You can also download the weights in a terminal (step 2 of
   [Local LLM](#local-llm-qwen38-27b-mlx-vlm)).

To start in the chat profile without `--chat`, set `profile = "chat"` in the `[app]` section of `config.toml`, or
choose "Chat only" in Settings > Profile. The Settings choice applies at once and saves `profile` in `config.toml`.
`--chat` always starts the chat profile, whatever `config.toml` says. Settings > Profile names the saved profile,
so you can see which profile the next start without `--chat` uses.

To start the chat from Finder or the Dock, build `Workstation Chat.app` in the repository folder and keep it there:

```sh
osacompile -o "Workstation Chat.app" docs/launcher-chat.applescript
```

If `Workstation.app` is already open, `Workstation Chat.app` opens a chat window on it instead of a second app
(see [Start the app](#start-the-app)).

## Install the models

Each model has two parts: an environment (or a code checkout) that runs it, and its weights. The Models screen
downloads the weights. You make the environments with the commands below; the Models screen shows the same commands
on the cards of the image, voice, music and video runtimes. The steps put everything under `~/mlx-media`, the
default in `config.example.toml`. The versions and revisions are the ones we tested; newer versions can change
command-line options.

Read each model's licence before you use its output (see [Model licences](#model-licences)).

### Gated models

FLUX.3 Klein and LTX-2.5 are gated on Hugging Face. Do these steps once before you download them:

1. Open the model page, log in to Hugging Face and accept the licence:
   [FLUX.3 Klein](https://huggingface.co/black-forest-labs/flux-3-image-klein) and
   [LTX-2.5 (q8)](https://huggingface.co/dgrauet/ltx-2.5-mlx-q8).
2. In a terminal in the app folder, log in with a Hugging Face access token:

   ```sh
   .venv/bin/hf auth login
   ```

The app never asks for your token and never stores it. The Models screen and the `hf` command from Homebrew use the
same login.

### Download the weights in the app

1. Open the Models screen.
2. Pick the models in the setup card, or press Download on a model's card.
3. For a gated model, tick "I accepted the licence on Hugging Face".

Downloads run one at a time, in the order of the queue. Each download pins the same revision as the commands below.
The weights of MOSS-Image, FLUX.3 Klein and Qwen3-ASR go into the Hugging Face cache (`~/.cache/huggingface/hub`).
The other weights go to the paths in `config.toml`.

The sections below also give the `hf download` command for each model, if you prefer to download in a terminal.

### Images: MOSS-Image and FLUX.3 Klein (mflux)

1. Get mflux at the tested commit (MOSS support was only on its main branch then) and make its environment:

   ```sh
   git clone https://github.com/mflux-community/mflux.git ~/mlx-media/mflux
   cd ~/mlx-media/mflux
   git checkout a3b21fcaa88726b2267bd42bfc2e2d722f7d5a47
   uv sync --frozen --no-dev --python /opt/homebrew/bin/python3.12
   ```

2. Download the weights into the Hugging Face cache (MOSS-Image about 27.2 GB, FLUX.3 Klein about 20.3 GB). mflux
   loads these revisions and reads only these folders. FLUX.3 Klein is gated.

   ```sh
   hf download OpenMOSS-Team/MOSS-Image-SFT --revision f972bd4b4b9e3852133cc5da954d87bc32cd8fb9 \
     --include "text_encoder/*" --include "tokenizer/*" --include "transformer/*" --include "vae/*"
   hf download black-forest-labs/flux-3-image-klein --revision 8454017f9ebaada30c998fccd9f67a80c7580f68 \
     --include "text_encoder/*" --include "transformer/*" --include "vae/*"
   ```

### Voice: Fish Audio Drama 3 (mlx-audio)

1. Make the environment:

   ```sh
   uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-drama
   uv pip install --python ~/mlx-media/venv-drama/bin/python "mlx-audio==0.6.1" scipy
   ```

2. Download the weights (about 23.8 GB) and the speech check Qwen3-ASR (about 1.0 GB):

   ```sh
   hf download mlx-community/fish-audio-drama-3-4bit --revision 1ac1b2108b63c2ef5bade463a121569363e293ce \
     --local-dir ~/mlx-media/models/fish-audio-drama-3-4bit
   hf download mlx-community/Qwen3-ASR-0.6B-8bit --revision 89e96d92ba34aca20b3e29fb10cc284097d1219f
   ```

3. Point the cache's `main` ref at that Qwen3-ASR revision. mlx-audio loads Qwen3-ASR by name, and a download by
   revision writes no ref. (The Models screen writes it for you.)

   ```sh
   ref=~/.cache/huggingface/hub/models--mlx-community--Qwen3-ASR-0.6B-8bit/refs
   mkdir -p "$ref" && printf %s 89e96d92ba34aca20b3e29fb10cc284097d1219f > "$ref/main"
   ```

The 4-bit build fits a 48 GB Mac; the 8-bit build peaked at about 49 GB. Qwen3-ASR checks every take.

### Music: ACE-Step 1.5

1. Get ACE-Step at the tested commit and make its environment:

   ```sh
   git clone https://github.com/ACE-Step/ACE-Step-1.5.git ~/mlx-media/ace-step
   cd ~/mlx-media/ace-step
   git checkout ca1e85fe9430179831e6bc6be790c332190a3866
   UV_PYTHON_DOWNLOADS=never uv sync --frozen --python /opt/homebrew/bin/python3.12
   ```

2. Download the weights (about 10.1 GB) into the checkout's `checkpoints` folder. Make the checkout first, because
   `git clone` refuses a folder that is not empty.

   ```sh
   hf download ACE-Step/Ace-Step1.5 --revision 19671f406d603126926c1b7e2adc169acbcade22 \
     --local-dir ~/mlx-media/ace-step/checkpoints
   ```

Do not use ACE-Step's own `start_*_macos.sh` scripts: they update the checkout with git and start a web server.

### Video: LTX-2.5 (ltx-2-mlx)

1. Make the environment:

   ```sh
   uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-ltx
   uv pip install --python ~/mlx-media/venv-ltx/bin/python \
     "ltx-core-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-core-mlx" \
     "ltx-pipelines-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-pipelines-mlx"
   ```

2. Download the weights (about 54.1 GB; gated). The tools use only the distilled transformer, so leave out the dev
   transformer (20 GB more):

   ```sh
   hf download dgrauet/ltx-2.5-mlx-q8 --revision 746ca9aacb697d2c739f544d68b584214dedcc75 \
     --exclude "transformer-dev.safetensors" --local-dir ~/mlx-media/models/ltx-2.5-mlx-q8
   ```

Do not install the PyPI package named `mlx-video` for this: it is a different project.

### Local LLM: Qwen3.8 27B (mlx-vlm)

Prompt help and Chat need this model. The app starts it with the python in `qwen_python` (`config.toml`) and loads
the weights from `qwen_model`.

1. Make the environment:

   ```sh
   uv venv --python /opt/homebrew/bin/python3.12 ~/mlx-media/venv-qwen
   uv pip install --python ~/mlx-media/venv-qwen/bin/python "mlx-vlm==0.7.11" "mlx==0.32.4" "transformers==5.22.1"
   ```

2. Download the weights (about 16.1 GB):

   ```sh
   hf download mlx-community/Qwen3.8-27B-4bit --revision 10c35caafbb80f7dc6a7a432cdd11af10a6d4818 \
     --local-dir ~/mlx-media/models/qwen3.8-27b-4bit
   ```

The model needs about 26 GB of memory (its peak plus the 8 GB reserve). On a Mac with less memory, the "Improve"
buttons stay hidden and Chat says that the Mac has too little memory.

## Model licences

The MIT licence of this repository covers only its own code. Each model has its own licence, and some licences limit
commercial use. A short summary follows, checked on 2026-11-03. It is not legal advice: read the full text before
you use a model or its output.

| Model | Licence | Commercial use | Source |
|---|---|---|---|
| MOSS-Image SFT | Apache-2.0 | Allowed. | [LICENSE](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT/blob/f972bd4b4b9e3852133cc5da954d87bc32cd8fb9/LICENSE) |
| FLUX.3 Klein | FLUX Kommunity License v1.0 | Output generation only, for users with less than US$5M gross annual revenue (affiliates included). Other commercial use needs a licence from Black Forest Labs. Do not use the outputs to improve a competing model. The full licence text is gated; we read only the model card. | [Model card](https://huggingface.co/black-forest-labs/flux-3-image-klein) |
| Fish Audio Drama 3 | Fish Audio Community License v1.0 | Allowed while your annual revenue is less than US$500,000. No hosted text-to-speech service. Do not imitate a real person's voice without consent, do not remove the watermark, and do not train other speech models on the output. A product that uses it shows "Built with Fish Audio". | [LICENSE](https://huggingface.co/mlx-community/fish-audio-drama-3-4bit/blob/1ac1b2108b63c2ef5bade463a121569363e293ce/LICENSE) |
| Qwen3-ASR 0.6B | Apache-2.0 | Allowed. | [Model card](https://huggingface.co/mlx-community/Qwen3-ASR-0.6B-8bit) |
| ACE-Step 1.5 | MIT | Allowed. | [Model card](https://huggingface.co/ACE-Step/Ace-Step1.5) |
| LTX-2.5 | LTX-2.x Community License Agreement | Free for entities with less than US$10M annual revenue. At or above that, commercial use needs a paid licence from Lightricks. Keep the watermarks and AI disclosures, and follow the AI laws that apply to you. | [LICENSE-2_x](https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x) |
| Qwen3.8 27B | Apache-2.0 | Allowed. | [LICENSE of the base model](https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/LICENSE) |

The Fish Drama 3 tool is Built with Fish Audio.

The code that runs the models is MIT-licensed: mflux, mlx-audio, ACE-Step 1.5, ltx-2-mlx, mlx-vlm and MLX.

## Files and logs

- Each job goes to `outputs/<job id>/`: `job.json` (the settings and the state), `job.log` (the model's output) and
  the result files. The app reads these folders at start.
- Each chat goes to `outputs/chats/<chat id>/`: `chat.json` and the images sent in it.
- The local LLM writes its log to `outputs/llm.log`.
- Started from `Workstation.app` or `Workstation Chat.app`, the app writes its log to `outputs/app.log`. Started
  from a terminal, it writes the log to the terminal.

## How it works

- `start.py` starts the server on `127.0.0.1:7870` and opens a native window (pywebview, the system WebKit). The
  window opens a sign-in link with this run's token, so other programs and users on the Mac cannot use the API.
- `app/server.py`: the API (`docs/API.md`), the job queue, the output files and the memory display.
- `app/tools.py`: the tools, their form fields and defaults, and the commands each job runs.
- `app/models.py` and `app/models_api.py`: the model catalog (repositories, pinned revisions, sizes, peaks and
  licences) and the download queue.
- `app/llm.py` and `app/llm_worker.py`: the local LLM. The worker runs in the LLM environment and keeps the model
  loaded between requests.
- `app/assist.py` (prompt help), `app/chat.py` (Chat), `app/library.py` (Library) and `app/settings_api.py`
  (Settings).
- `app/agent/`: the tools, the approval policy and the Python sandbox for the Chat's agent mode (`docs/AGENT.md`).
- `app/config.py`: reads `config.toml` (where the models are, the prompt help provider and the job options).
- `app/macos.py`: keeps the Mac awake while jobs run (`caffeinate`) and shows notifications (`osascript`).
- `app/runners/`: small scripts that run inside a model's own environment: Fish Drama 3 and ACE-Step (they have no
  command-line tool), and an LTX extend wrapper that lowers its memory peak.
- `web/`: the interface (plain HTML, CSS and JavaScript modules; no build step, no network requests).
  `web/README.md` describes the files.

Each model runs as its own process group from its own environment, with `HF_HUB_OFFLINE=1`. A finished or cancelled
job gives all its memory back. The LLM worker is a process too, and it unloads the model by exiting.

## Development

The tests run no model and need no network. Install the development tools into the app's environment, then run the
checks:

```sh
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
```

`.github/workflows/ci.yml` runs the same checks on macOS, but the workflow is turned off on GitHub. Run the checks
locally before you push.

## Security

- The server listens on `127.0.0.1` only. It checks the Host and Origin headers, so other web pages in your browser
  cannot send jobs to it.
- Uploaded files get fixed names, and the server sends back only media files.
- The app has no accounts. It connects to the network for the downloads that you start on the Models screen, and
  in an Agent chat for the web tools after you allow the call.
- Agent mode runs model-written Python only in a sandbox without network or access to your files, and only after
  you approve each run. `docs/AGENT.md` lists what the sandbox does not protect.

## Licence

MIT, see [LICENSE](LICENSE).
