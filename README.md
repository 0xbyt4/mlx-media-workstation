# MLX Media Workstation

A local app for open image, voice, music and video models on Apple Silicon Macs. You pick a tool, fill in a form and
press Generate. Every model runs on your Mac with [MLX](https://github.com/ml-explore/mlx). Nothing is uploaded, and
the models download nothing at run time.

The app does not include the models. You install each model you want (see [Install the models](#install-the-models)),
and the app runs it as a separate process. A tool whose model is not installed refuses its jobs and names the missing
path; the other tools keep working.

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

Jobs run one at a time, because each model needs most of the memory of a 48 GB Mac. If a Mac has less memory than a
tool's peak, macOS swaps, and the job gets much slower or fails. We tested only on a 48 GB M5 Pro.

`docs/PROMPTING.md` has the prompt rules that worked for each model.

## Requirements

- An Apple Silicon Mac. We built and tested the app on macOS 26 with an M5 Pro and 48 GB of memory.
- [Homebrew](https://brew.sh) with `ffmpeg`, `python@3.12`, `python@3.14` and `hf` (the Hugging Face command line):
  `brew install ffmpeg python@3.12 python@3.14 hf`.
- [uv](https://docs.astral.sh/uv/) to make the Python environments: `brew install uv`.
- About 120 GB of free disk space for all the models, less if you install only some of them.

## Start the app

1. Clone the repository and make the app's own environment:

   ```sh
   git clone https://github.com/0xbyt4/mlx-media-workstation.git
   cd mlx-media-workstation
   uv venv --python /opt/homebrew/bin/python3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ```

2. Install one or more models (next section).
3. Copy `config.example.toml` to `config.toml`. If you installed the models in a folder other than `~/mlx-media`,
   change the paths in `config.toml` (a relative path counts from the repository folder).
4. Start the app:

   ```sh
   .venv/bin/python start.py            # opens the app window
   .venv/bin/python start.py --browser  # server only: open http://127.0.0.1:7870 in a browser
   ```

To start the app from Finder or the Dock, build `Workstation.app` in the repository folder and keep it there:

```sh
osacompile -o Workstation.app docs/launcher.applescript
```

Each job goes to `outputs/<job id>/`. Started from `Workstation.app`, the app writes its log to `outputs/app.log`;
started from a terminal, it writes the log there.

## Install the models

These steps put everything under `~/mlx-media`, the default in `config.example.toml`. The versions are the ones we
tested; newer versions can change command-line options. Some weights are gated on Hugging Face: accept the licence on
the model page first, then log in with `hf auth login`.

Read each model's licence before you use its output (see [Model licences](#model-licences)).

### Images: MOSS-Image and FLUX.3 Klein (mflux)

1. Get mflux at the tested commit (MOSS support was only on its main branch then) and make its environment:

   ```sh
   git clone https://github.com/mflux-community/mflux.git ~/mlx-media/mflux
   cd ~/mlx-media/mflux
   git checkout a3b21fcaa88726b2267bd42bfc2e2d722f7d5a47
   uv sync --frozen --no-dev --python /opt/homebrew/bin/python3.12
   ```

2. Download the weights into the Hugging Face cache. FLUX.3 Klein is gated (FLUX Kommunity License):

   ```sh
   hf download OpenMOSS-Team/MOSS-Image-SFT
   hf download black-forest-labs/flux-3-image-klein
   ```

   mflux pins its own revisions of these models. If a job reports a missing file, run the command from the first
   line of the job's log once in a terminal, without `HF_HUB_OFFLINE=1`, so mflux can fetch the file.

### Voice: Fish Audio Drama 3 (mlx-audio)

```sh
uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-drama
uv pip install --python ~/mlx-media/venv-drama/bin/python "mlx-audio==0.6.1" scipy
hf download mlx-community/fish-audio-drama-3-4bit --revision 1ac1b2108b63c2ef5bade463a121569363e293ce \
  --local-dir ~/mlx-media/models/fish-audio-drama-3-4bit
hf download mlx-community/Qwen3-ASR-0.6B-8bit
```

The 4-bit build fits a 48 GB Mac; the 8-bit build peaked at about 49 GB. Qwen3-ASR checks every take.

### Music: ACE-Step 1.5

```sh
git clone https://github.com/ACE-Step/ACE-Step-1.5.git ~/mlx-media/ace-step
cd ~/mlx-media/ace-step
git checkout ca1e85fe9430179831e6bc6be790c332190a3866
UV_PYTHON_DOWNLOADS=never uv sync --frozen --python /opt/homebrew/bin/python3.12
hf download ACE-Step/Ace-Step1.5 --revision 19671f406d603126926c1b7e2adc169acbcade22 --local-dir checkpoints
```

Do not use ACE-Step's own `start_*_macos.sh` scripts: they update the checkout with git and start a web server.

### Video: LTX-2.5 (ltx-2-mlx)

```sh
uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-ltx
uv pip install --python ~/mlx-media/venv-ltx/bin/python \
  "ltx-core-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-core-mlx" \
  "ltx-pipelines-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-pipelines-mlx"
hf download dgrauet/ltx-2.5-mlx-q8 --revision 746ca9aacb697d2c739f544d68b584214dedcc75 \
  --local-dir ~/mlx-media/models/ltx-2.5-mlx-q8
```

The weights are gated (LTX-2 Community License) and about 50 GB. Do not install the PyPI package named `mlx-video`
for this: it is a different project.

## Model licences

The MIT licence of this repository covers only its own code. Each model has its own licence, and some licences limit
commercial use. A short summary follows; read the full text on each model page:

| Model | Licence |
|---|---|
| MOSS-Image | Apache-2.0 |
| FLUX.3 Klein | FLUX Kommunity License: commercial output under US$5M revenue, review outputs, AI disclosure where the law asks |
| Fish Audio Drama 3 | Fish Audio Community License: commercial use under US$500k revenue, no imitation of real people, keep its watermark |
| ACE-Step 1.5 | MIT |
| LTX-2.5 | LTX-2 Community License: free under US$10M revenue |

## How it works

- `start.py` starts the server on `127.0.0.1:7870` and opens a native window (pywebview, the system WebKit).
- `app/server.py`: the API (`docs/API.md`), the job queue, the output files and the memory display.
- `app/tools.py`: the tools, their form fields and defaults, and the commands each job runs.
- `app/config.py`: reads `config.toml` (where the models are).
- `app/runners/`: small scripts that run inside a model's own environment: Fish Drama 3 and ACE-Step (they have no
  command-line tool), and an LTX extend wrapper that lowers its memory peak.
- `web/`: the interface (plain HTML, CSS and JavaScript; no build step, no network requests).

Each model runs as its own process group from its own environment, with `HF_HUB_OFFLINE=1`. A finished or cancelled
job gives all its memory back.

## Development

The tests run no model and need no network. Install the development tools into the app's environment, then run the
checks:

```sh
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check
.venv/bin/ruff format --check
.venv/bin/python -m pytest
```

GitHub Actions runs the same checks on macOS for each push to `main` and each pull request
(`.github/workflows/ci.yml`).

## Security

- The server listens on `127.0.0.1` only. It checks the Host and Origin headers, so other web pages in your browser
  cannot send jobs to it.
- Uploaded files get fixed names, and the server sends back only media files.
- The app has no accounts and sends no data anywhere.

## Licence

MIT, see [LICENSE](LICENSE).
