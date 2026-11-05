"""The Models screen: what each tool needs, what is installed, whether it fits this Mac, and in-app weight downloads.

Weights download with huggingface_hub at a pinned commit, into the path config.toml gives them, or into the Hugging
Face cache for the models that mflux and mlx-audio load by name. Runtimes (model environments and checkouts) stay
manual: their entry carries the README commands. One download runs at a time, in a child process
(`python -m app.models <id>`), so a cancel stops it at once. Finished files stay and the next download skips them;
partial files are deleted. Progress is counted against the file sizes the Hub lists for that commit. The child reports
the bytes it has received and written (report lines on stdout); the bytes on disk are the fallback without a report.
Xet writes a file in large blocks, so the disk alone can stand still for a minute while data arrives.

More downloads wait in a queue. When a download ends (done, failed or cancelled), the download thread starts the next
one. The lock guards `state`, `queue`, `history` and `batch`; request threads and download threads both change them.
"""

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

import httpx2
from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    LocalEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from huggingface_hub.hf_api import RepoFile
from huggingface_hub.utils import filter_repo_objects

from app import tools
from app.config import DEFAULTS, PATHS, ROOT

HF_HUB = tools.HF_HUB
# macOS and the app itself keep about 8 GB in use. A model fits when its peak plus that reserve is within the total
# memory; above it macOS swaps and the job gets much slower or fails (Fish Drama 3 at 39 GB on our 48 GB Mac is the
# tightest case we ran).
RESERVE_GB = 8.0
# The download speed is the bytes added over at least this many seconds of progress samples (the UI polls every 2 s),
# so one slow or fast poll does not swing it. Below MIN_SPAN_S of samples there is no speed yet.
SPEED_WINDOW_S = 10.0
MIN_SPAN_S = 1.5
# A download whose bytes have not grown for this long is "stalled"; the UI says so instead of showing a speed.
STALL_S = 60.0
# The child reports its byte counts on stdout as lines that start with REPORT_TAG, then JSON {"bytes": written,
# "transfer": received from the network}, at most every REPORT_EVERY_S. Every other line is plain text, and the last
# one is the error the user sees when the child fails.
REPORT_TAG = "@progress "
REPORT_EVERY_S = 0.5
# The queue keeps this much disk free: macOS needs room for swap and updates.
DISK_MARGIN_GB = 10.0
clock = time.monotonic  # replaced in tests


@dataclass(frozen=True)
class Model:
    """One installable component: weights to download (repo set) or a runtime to install by hand (install set)."""

    id: str
    label: str
    tools: tuple[str, ...]  # tool ids in app/tools.py that need it
    path_key: (
        str | None
    )  # the [paths] key in config.toml; None = the Hugging Face cache
    files: tuple[
        str, ...
    ]  # what must exist under the install path to count as installed
    subdir: str = ""  # folder under the path_key's path
    repo: str | None = None
    revision: str | None = None
    size_gb: float | None = (
        None  # download size in GB of 10^9 bytes, as the Hub shows it
    )
    licence: str | None = None
    licence_url: str | None = None
    peak_gb: float | None = (
        None  # peak memory of a job that loads it (README tool table)
    )
    gated: bool = False
    allow: tuple[str, ...] = ()  # only the repo files the runtime reads; empty = all
    ignore: tuple[str, ...] = ()
    features: tuple[str, ...] = ()  # app features outside the tool list that use it
    needs: str | None = None  # the manual entry to install first
    # the form choice (field, value) that makes a job load it instead of its tools' other models; None = the default
    choice: tuple[str, str] | None = None
    install: str | None = None  # the README commands of a manual entry
    # not needed by any tool or feature: the setup card does not pick it or count it as missing
    optional: bool = False

    @property
    def manual(self) -> bool:
        return self.repo is None


MODELS: list[Model] = [
    Model(
        id="mflux",
        label="mflux (image runtime)",
        tools=("image.moss", "image.klein", "image.klein_edit"),
        path_key="mflux_bin",
        files=(
            "mflux-generate-moss-image",
            "mflux-generate-flux3",
            "mflux-generate-flux3-edit",
        ),
        install="""git clone https://github.com/mflux-community/mflux.git ~/mlx-media/mflux
cd ~/mlx-media/mflux
git checkout a3b21fcaa88726b2267bd42bfc2e2d722f7d5a47
uv sync --frozen --no-dev --python /opt/homebrew/bin/python3.12""",
    ),
    # mflux loads these two from the Hugging Face cache at the commits it pins (ModelRevisions in the tested
    # mflux commit), and reads only the component folders
    Model(
        id="moss-image-sft",
        label="MOSS-Image SFT",
        tools=("image.moss",),
        path_key=None,
        files=(
            "text_encoder/model-00001-of-00004.safetensors",
            "text_encoder/model-00002-of-00004.safetensors",
            "text_encoder/model-00003-of-00004.safetensors",
            "text_encoder/model-00004-of-00004.safetensors",
            "tokenizer/tokenizer.json",
            "transformer/diffusion_pytorch_model-00001-of-00003.safetensors",
            "transformer/diffusion_pytorch_model-00002-of-00003.safetensors",
            "transformer/diffusion_pytorch_model-00003-of-00003.safetensors",
            "vae/diffusion_pytorch_model.safetensors",
        ),
        repo="OpenMOSS-Team/MOSS-Image-SFT",
        revision="f972bd4b4b9e3852133cc5da954d87bc32cd8fb9",
        size_gb=27.2,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT/blob/f972bd4b4b9e3852133cc5da954d87bc32cd8fb9/LICENSE",
        peak_gb=22.0,
        allow=("text_encoder/*", "tokenizer/*", "transformer/*", "vae/*"),
    ),
    Model(
        id="flux-3-image-klein",
        label="FLUX.3 Klein",
        tools=("image.klein", "image.klein_edit"),
        path_key=None,
        files=(
            "text_encoder/model-00001-of-00002.safetensors",
            "text_encoder/model-00002-of-00002.safetensors",
            "text_encoder/tokenizer.json",
            "transformer/diffusion_pytorch_model-00001-of-00002.safetensors",
            "transformer/diffusion_pytorch_model-00002-of-00002.safetensors",
            "vae/diffusion_pytorch_model.safetensors",
        ),
        repo="black-forest-labs/flux-3-image-klein",
        revision="8454017f9ebaada30c998fccd9f67a80c7580f68",
        size_gb=20.3,
        licence="FLUX Kommunity License",
        licence_url="https://huggingface.co/black-forest-labs/flux-3-image-klein/blob/8454017f9ebaada30c998fccd9f67a80c7580f68/LICENSE.md",
        peak_gb=14.0,
        gated=True,
        allow=("text_encoder/*", "transformer/*", "vae/*"),
    ),
    Model(
        id="drama-venv",
        label="Fish Drama 3 environment (mlx-audio)",
        tools=("voice.drama3", "voice.transcribe"),
        path_key="drama_python",
        files=(),
        install="""uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-drama
uv pip install --python ~/mlx-media/venv-drama/bin/python "mlx-audio==0.6.1" scipy""",
    ),
    Model(
        id="fish-audio-drama-3-4bit",
        label="Fish Audio Drama 3 (4-bit)",
        tools=("voice.drama3",),
        path_key="drama_model",
        files=(
            "codec.safetensors",
            "config.json",
            "model-00001.safetensors",
            "model-00002.safetensors",
            "model-00003.safetensors",
            "model-00004.safetensors",
            "model-00005.safetensors",
            "model-00006.safetensors",
            "tokenizer.json",
        ),
        repo="mlx-community/fish-audio-drama-3-4bit",
        revision="1ac1b2108b63c2ef5bade463a121569363e293ce",
        size_gb=23.8,
        licence="Fish Audio Community License",
        licence_url="https://huggingface.co/mlx-community/fish-audio-drama-3-4bit/blob/1ac1b2108b63c2ef5bade463a121569363e293ce/LICENSE",
        peak_gb=39.0,
    ),
    # the speech check of the Drama 3 job and the Transcribe tool; the runners load it by name. Its own peak (2.6 GB
    # for 40 s of speech, measured 2026-11-03); a Drama 3 job's peak is the Drama model's
    Model(
        id="qwen3-asr-0.6b-8bit",
        label="Qwen3-ASR 0.6B (speech check)",
        tools=("voice.drama3", "voice.transcribe"),
        path_key=None,
        files=("config.json", "model.safetensors", "vocab.json"),
        repo="mlx-community/Qwen3-ASR-0.6B-8bit",
        revision="89e96d92ba34aca20b3e29fb10cc284097d1219f",
        size_gb=1.0,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/mlx-community/Qwen3-ASR-0.6B-8bit",
        peak_gb=3.0,
    ),
    # the Transcribe tool's "Nemotron" model: NVIDIA's NVFP4 checkpoint, with the CTC head (word times) and the
    # speaker encoder. mlx-audio loads it from its folder. Peak: 26.7 GB
    # measured (/usr/bin/time -l, 55 s of speech with word times), plus 2 GB
    Model(
        id="nemotron-3.5-transcribe-nvfp4",
        label="Nemotron 3.5 Transcribe (NVFP4)",
        tools=("voice.transcribe",),
        path_key="nemotron_model",
        files=(
            "config.json",
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
            "model-ctc-bf16.safetensors",
            "model-mtp-fp8.safetensors",
            "model.safetensors.index.json",
            "perception.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
        repo="nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4",
        revision="66c6adc4020179567074d0399cda2a4eeb2bb347",
        size_gb=21.6,
        licence="OpenMDW-1.1",
        licence_url="https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-NVFP4/blob/66c6adc4020179567074d0399cda2a4eeb2bb347/LICENSE",
        peak_gb=29.0,
        needs="qwen-venv",
        choice=("model", "nemotron"),
    ),
    Model(
        id="ace-step-checkout",
        label="ACE-Step 1.5 checkout",
        tools=("music.ace",),
        path_key="ace_dir",
        files=(".venv/bin/python",),
        install="""git clone https://github.com/ACE-Step/ACE-Step-1.5.git ~/mlx-media/ace-step
cd ~/mlx-media/ace-step
git checkout ca1e85fe9430179831e6bc6be790c332190a3866
UV_PYTHON_DOWNLOADS=never uv sync --frozen --python /opt/homebrew/bin/python3.12""",
    ),
    # the weights go inside the checkout, and git clone refuses a folder that is not empty: checkout first
    Model(
        id="ace-step-1.5",
        label="ACE-Step 1.5 turbo",
        tools=("music.ace",),
        path_key="ace_dir",
        subdir="checkpoints",
        files=(
            "Qwen3-Embedding-0.6B/model.safetensors",
            "acestep-5Hz-lm-1.7B/model.safetensors",
            "acestep-v15-turbo/model.safetensors",
            "config.json",
            "vae/diffusion_pytorch_model.safetensors",
        ),
        repo="ACE-Step/Ace-Step1.5",
        revision="19671f406d603126926c1b7e2adc169acbcade22",
        size_gb=10.1,
        licence="MIT",
        licence_url="https://huggingface.co/ACE-Step/Ace-Step1.5",
        peak_gb=23.0,
        needs="ace-step-checkout",
    ),
    Model(
        id="ltx-2-mlx",
        label="ltx-2-mlx (video runtime)",
        tools=("video.t2v", "video.i2v", "video.a2v", "video.extend"),
        path_key="ltx_bin",
        files=("ltx-2-mlx", "python"),
        install="""uv venv --python /opt/homebrew/bin/python3.14 ~/mlx-media/venv-ltx
uv pip install --python ~/mlx-media/venv-ltx/bin/python \\
  "ltx-core-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-core-mlx" \\
  "ltx-pipelines-mlx @ git+https://github.com/dgrauet/ltx-2-mlx@b82b97e3d000e01361f8cfcb745244b15b89a521#subdirectory=packages/ltx-pipelines-mlx\"""",
    ),
    # the tools run only the distilled transformer; the dev transformer would add 20 GB
    Model(
        id="ltx-2.5-mlx-q8",
        label="LTX-2.5 (q8)",
        tools=("video.t2v", "video.i2v", "video.a2v", "video.extend"),
        path_key="ltx_model",
        files=(
            "audio_vae.safetensors",
            "connector.safetensors",
            "duration_head.safetensors",
            "ltx-2.5-22b-distilled-lora-450-bf16.safetensors",
            "spatial_upscaler_x2_v1_0.safetensors",
            "temporal_upscaler_x2_v1_0.safetensors",
            "text_encoder.safetensors",
            "transformer-distilled.safetensors",
            "vae_decoder_av.safetensors",
            "vae_decoder_conv.safetensors",
            "vae_encoder_av.safetensors",
            "vae_encoder_conv.safetensors",
            "vocoder.safetensors",
        ),
        repo="dgrauet/ltx-2.5-mlx-q8",
        revision="746ca9aacb697d2c739f544d68b584214dedcc75",
        size_gb=54.1,
        licence="LTX-2.x Community License Agreement",
        licence_url="https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x",
        peak_gb=35.0,
        gated=True,
        ignore=("transformer-dev.safetensors",),
    ),
    Model(
        id="qwen-venv",
        label="Local LLM environment (mlx-vlm)",
        tools=("doc.ocr",),
        path_key="qwen_python",
        files=(),
        install="""uv venv --python /opt/homebrew/bin/python3.12 ~/mlx-media/venv-qwen
uv pip install --python ~/mlx-media/venv-qwen/bin/python "mlx-vlm==0.7.11" "mlx==0.32.4" "transformers==5.22.1" "mlx-audio==0.6.4\"""",
        features=("Prompt help", "Chat"),
    ),
    # peak not measured yet: the 16.1 GB of weights plus a short-context KV cache
    Model(
        id="qwen3.8-27b-4bit",
        label="Qwen3.8 27B (4-bit)",
        tools=(),
        path_key="qwen_model",
        files=(
            "config.json",
            "model-00001-of-00003.safetensors",
            "model-00002-of-00003.safetensors",
            "model-00003-of-00003.safetensors",
            "tokenizer.json",
        ),
        repo="mlx-community/Qwen3.8-27B-4bit",
        revision="10c35caafbb80f7dc6a7a432cdd11af10a6d4818",
        size_gb=16.1,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/LICENSE",
        peak_gb=18.0,
        features=("Prompt help", "Chat"),
    ),
    # Read Text runs it with mlx-vlm in the local LLM environment; the repo's document_crosspage.py and assets are
    # not downloaded (the runner holds its two prompts)
    Model(
        id="ovisocr3",
        label="OvisOCR3 (0.8B, OCR)",
        tools=("doc.ocr",),
        path_key="ocr_model",
        files=(
            "chat_template.jinja",
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
        repo="ATH-MaaS/OvisOCR3",
        revision="17f255e2c8ae6d18d5d5edb0a474bd4ed39aef90",
        size_gb=1.7,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/ATH-MaaS/OvisOCR3/blob/17f255e2c8ae6d18d5d5edb0a474bd4ed39aef90/LICENSE",
        peak_gb=6.0,  # measured 5.7 GB on a 2481x3508 page, 5.0 GB at 1240x1753
        allow=("*.safetensors", "*.json", "merges.txt", "chat_template.jinja"),
        needs="qwen-venv",
    ),
    # optional: speculative decoding for the local LLM ([app] llm_draft); peak_gb is the memory it adds to the LLM's
    Model(
        id="qwen3.8-27b-dflash2",
        label="Qwen3.8 27B DFlash2 draft",
        tools=(),
        path_key="qwen_draft",
        files=("config.json", "model.safetensors"),
        repo="z-lab/Qwen3.8-27B-DFlash2",
        revision="50307d4c4cde6860d4eee73e2547cd786fe8e8a4",
        size_gb=3.85,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2",
        peak_gb=3.0,
        allow=("config.json", "model.safetensors"),
        features=("Chat",),
        optional=True,
    ),
]
BY_ID = {m.id: m for m in MODELS}

lock = threading.Lock()
# the current or last download: id, status (running, done, failed, cancelled), error, sizes, the child process, the
# bytes on disk when the child started (base) and the child's last report
state: dict[str, Any] = {}
# the model ids that wait to download, in order
queue: list[str] = []
# the last result of earlier downloads: model id -> {status, error, sizes}
history: dict[str, dict[str, Any]] = {}
# every model id of the current run of downloads (running, queued and finished), in order; a new run starts when
# models are queued while nothing downloads. batch_id counts the runs, so the UI can tell a new one.
batch: list[str] = []
batch_id = 0
# true while a download thread runs; it may still clean up after a cancel, so the next download waits for it
worker = False
sizes_cache: dict[
    str, dict[str, int]
] = {}  # model id -> {file: bytes}, fetched once from the Hub


def repo_cache(m: Model) -> Path:
    return HF_HUB / f"models--{m.repo.replace('/', '--')}"


def target(m: Model) -> Path:
    """Where the component is installed: its configured path, or its snapshot in the Hugging Face cache."""
    if m.path_key is None:
        return repo_cache(m) / "snapshots" / m.revision
    return getattr(PATHS, m.path_key) / m.subdir


def install_target(m: Model) -> Path | None:
    """Where the install commands of a manual entry put it. The commands are fixed, so this is the default path."""
    if not m.manual or m.path_key is None:
        return None
    return ROOT / Path(DEFAULTS[m.path_key]).expanduser() / m.subdir


def installed(m: Model) -> bool:
    root = target(m)
    return root.exists() and all((root / f).exists() for f in m.files)


def partial_dir(m: Model) -> Path:
    """Where huggingface_hub writes the files it is still downloading (*.incomplete)."""
    if m.path_key is None:
        return repo_cache(m) / "blobs"
    return target(m) / ".cache" / "huggingface" / "download"


def on_disk(m: Model, sizes: dict[str, int]) -> int:
    """Bytes of this download on disk: the finished files plus the partial ones."""
    root = target(m)
    done = sum((root / f).stat().st_size for f in sizes if (root / f).is_file())
    partial = 0
    for p in partial_dir(m).rglob("*.incomplete"):
        try:
            partial += p.stat().st_size
        except FileNotFoundError:  # finished and moved into place since the listing
            pass
    return done + partial


def percent(done: int, total: int) -> float:
    return round(100 * min(done, total) / total, 1)


def clean_partial(m: Model) -> None:
    """Delete partial files: huggingface_hub names them per process, so a new download never resumes them."""
    for p in partial_dir(m).rglob("*.incomplete"):
        p.unlink(missing_ok=True)


def existing(path: Path) -> Path:
    return next(p for p in (path, *path.parents) if p.exists())


def disk_free(m: Model) -> int:
    return shutil.disk_usage(existing(target(m))).free


def mem_total_gb() -> float:
    out = subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
    ).stdout
    return round(int(out or 0) / 2**30, 1)


def fits(peak_gb: float, mem_gb: float) -> bool:
    return peak_gb + RESERVE_GB <= mem_gb


def job_models(tool_id: str, params: dict[str, Any] | None = None) -> list[Model]:
    """The models that a job of this tool with these form values loads: the models its values pick by a choice, else
    the tool's models without a choice (also without values)."""
    mine = [m for m in MODELS if tool_id in m.tools]
    picked = [
        m
        for m in mine
        if m.choice and str((params or {}).get(m.choice[0])) == m.choice[1]
    ]
    return picked or [m for m in mine if m.choice is None]


def tool_peak(tool_id: str, params: dict[str, Any] | None = None) -> float:
    """The peak memory in GB of a job of this tool with these form values (0 for tools that load no model)."""
    return max((m.peak_gb or 0.0 for m in job_models(tool_id, params)), default=0.0)


def remote_sizes(m: Model) -> dict[str, int]:
    """The files this download takes and their sizes at the pinned commit (asked once per model)."""
    if m.id not in sizes_cache:
        files = [
            f
            for f in HfApi().list_repo_tree(m.repo, revision=m.revision, recursive=True)
            if isinstance(f, RepoFile)
        ]
        kept = filter_repo_objects(
            files,
            allow_patterns=list(m.allow) or None,
            ignore_patterns=list(m.ignore) or None,
            key=lambda f: f.path,
        )
        sizes_cache[m.id] = {f.path: f.size for f in kept}
    return sizes_cache[m.id]


def explain(e: BaseException, m: Model) -> str:
    """The message the user sees for a failed download."""
    if isinstance(e, RevisionNotFoundError):
        return f"The pinned revision {m.revision[:12]} of {m.repo} is no longer on Hugging Face."
    if isinstance(e, (GatedRepoError, RepositoryNotFoundError)) or (
        isinstance(e, HfHubHTTPError) and e.response.status_code in (401, 403)
    ):
        return (
            f"Hugging Face refused {m.repo}: the model is gated or you are not logged in. Accept its licence on "
            f"https://huggingface.co/{m.repo}, run `.venv/bin/hf auth login` in a terminal in the app folder, then try again."
        )
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return f"The disk is full. {m.label} needs about {m.size_gb} GB; free some space and try again."
    if isinstance(e, (httpx2.TransportError, LocalEntryNotFoundError, ConnectionError)):
        return (
            "Could not reach Hugging Face. Check the network connection and try again."
        )
    return f"Download failed: {type(e).__name__}: {e}"


def check(m: Model) -> None:
    """Raise ValueError (shown to the user) when m cannot be downloaded at all."""
    if m.manual:
        raise ValueError(
            f"{m.label} is installed with the commands shown here (README.md)."
        )
    if installed(m):
        raise ValueError(f"{m.label} is already installed.")


def start(m: Model) -> str:
    """Download m now, or after the downloads before it. Returns "running" or "queued"."""
    check(m)
    add([m.id])
    with lock:
        return "queued" if m.id in queue else "running"


def busy_ids() -> set[str]:
    """The running and the queued model ids (call under the lock)."""
    running = {state["id"]} if state.get("status") == "running" else set()
    return running | set(queue)


def order(ids: list[str], busy: set[str]) -> list[str]:
    """ids with each entry after the entry it needs. Raises ValueError when a need is missing (call under the lock)."""
    wanted = set(ids)
    out: list[str] = []

    def visit(i: str, path: tuple[str, ...]) -> None:
        if i in out:
            return
        m = BY_ID[i]
        need = BY_ID[m.needs] if m.needs else None
        if need and not installed(need):
            if need.id in path:
                raise ValueError(f"{m.label} and {need.label} need each other.")
            if need.manual:
                raise ValueError(
                    f"Install {need.label} first: {m.label} needs it. Its commands are on its card."
                )
            if need.id in wanted:
                visit(need.id, (*path, i))
            elif need.id not in busy:
                raise ValueError(
                    f"{m.label} needs {need.label}: add it to the downloads too."
                )
        out.append(i)

    for i in ids:
        visit(i, ())
    return out


def left_bytes(m: Model, got: int = 0) -> int:
    """The bytes m still has to download: the Hub sizes minus what is on disk (or got, the reported bytes, when that is
    more), or the catalog size before the Hub was asked."""
    sizes = sizes_cache.get(m.id)
    if sizes:
        return max(0, sum(sizes.values()) - max(on_disk(m, sizes), got))
    return int((m.size_gb or 0) * 1e9)


def reported(m: Model) -> int:
    """The bytes of m's running download by the child's last report: the bytes on disk when it started plus the larger
    of the bytes written and received. 0 without a report (call under the lock)."""
    report = state.get("report")
    if state.get("id") != m.id or state.get("status") != "running" or not report:
        return 0
    return state["base"] + max(report.values())


def parse_report(line: str) -> dict[str, int] | None:
    """The counts of a report line, or None when the line is malformed."""
    try:
        counts = json.loads(line[len(REPORT_TAG) :])
        return {k: int(counts[k]) for k in ("bytes", "transfer")}
    except (ValueError, TypeError, KeyError):
        return None


def disk_of(m: Model) -> int:
    return existing(target(m)).stat().st_dev


def check_disk(ids: list[str]) -> None:
    """Refuse when the downloads of ids (running and queued ones included) leave less than DISK_MARGIN_GB free on a
    disk (call under the lock)."""
    need: dict[int, int] = {}
    first: dict[int, Model] = {}
    for i in ids:
        m = BY_ID[i]
        d = disk_of(m)
        need[d] = need.get(d, 0) + left_bytes(m, reported(m))
        first.setdefault(d, m)
    for d, n in need.items():
        m = first[d]
        free = disk_free(m)
        if n > free - DISK_MARGIN_GB * 1e9:
            raise ValueError(
                f"Not enough disk space: these downloads need about {n / 1e9:.1f} GB on the disk of "
                f"{existing(target(m))}, and {free / 1e9:.1f} GB is free. Workstation keeps "
                f"{DISK_MARGIN_GB:.0f} GB free for macOS. Pick fewer models or free some space."
            )


def add(ids: list[str]) -> dict[str, list[str]]:
    """Queue the downloads of ids in this order (each after the entry it needs) and start the first one when nothing
    runs. Installed, running and queued ids are skipped. Raises ValueError, and queues nothing, when an id is unknown or
    manual, a need is missing, or the disk is too small."""
    global batch_id, worker
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise ValueError(f"No such model: {', '.join(unknown)}.")
    manual = [BY_ID[i].label for i in ids if BY_ID[i].manual]
    if manual:
        raise ValueError(
            f"Install these with the commands on their cards (README.md): {', '.join(manual)}."
        )
    done = {i for i in ids if installed(BY_ID[i])}
    with lock:
        busy = busy_ids()
        fresh = list(dict.fromkeys(i for i in ids if i not in done and i not in busy))
        new = order(fresh, busy)
        running = state.get("status") == "running"
        if new:
            check_disk(([state["id"]] if running else []) + queue + new)
        if new and not worker and not queue:
            batch.clear()
            batch_id += 1
        for i in new:
            history.pop(i, None)
        queue.extend(new)
        batch.extend(i for i in new if i not in batch)  # a retry keeps its place
        first = None
        if not worker:
            first = advance()
            worker = first is not None
    if first is not None:
        threading.Thread(target=run, args=(first,), daemon=True).start()
    return {"added": new, "skipped": [i for i in ids if i not in new]}


def advance() -> Model | None:
    """Start the next queued download that can start; returns it (call under the lock, start its thread after)."""
    while queue:
        m = BY_ID[queue.pop(0)]
        if installed(m):  # installed by hand meanwhile
            continue
        need = BY_ID[m.needs] if m.needs else None
        if need and not installed(need):
            history[m.id] = {
                "status": "failed",
                "error": f"{need.label} is not installed, and {m.label} needs it.",
                "sizes": None,
            }
            continue
        if state.get("id"):
            history[state["id"]] = {k: state[k] for k in ("status", "error", "sizes")}
        history.pop(m.id, None)
        state.clear()
        state.update(
            id=m.id,
            status="running",
            error=None,
            sizes=None,
            proc=None,
            samples=[],
            base=0,
            report=None,
        )
        return m
    return None


def fail(message: str) -> None:
    with lock:
        if state["status"] == "running":
            state["status"], state["error"] = "failed", message


def run(m: Model) -> None:
    """The download thread: it downloads m, then starts the next queued download."""
    global worker
    try:
        download(m)
    finally:
        with lock:
            if state.get("id") == m.id:
                state["proc"] = None
                if state["status"] == "running":  # an error download() did not expect
                    state["status"], state["error"] = (
                        "failed",
                        "The download stopped with an unexpected error.",
                    )
            nxt = advance()
            worker = nxt is not None
        if nxt is not None:
            threading.Thread(target=run, args=(nxt,), daemon=True).start()


def download(m: Model) -> None:
    """File sizes from the Hub, a disk space check, then the child process."""
    try:
        sizes = remote_sizes(m)
    except Exception as e:  # noqa: BLE001 - any failure belongs to this download
        fail(explain(e, m))
        return
    clean_partial(m)  # left by an app that closed or crashed mid-download
    base = on_disk(m, sizes)
    need, free = sum(sizes.values()) - base, disk_free(m)
    if need > free:
        fail(
            f"Not enough disk space: {m.label} needs {need / 1e9:.1f} GB more, "
            f"and {free / 1e9:.1f} GB is free."
        )
        return
    with lock:  # check and start under the lock, so a cancel cannot slip in between
        if state["status"] != "running":
            return
        state["sizes"], state["base"] = sizes, base
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.models", m.id],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
            start_new_session=True,
        )
        state["proc"] = proc
    lines = []
    for line in proc.stdout:  # ends when the child exits or is killed
        if not line.startswith(REPORT_TAG):
            lines.append(line.strip())
            continue
        counts = parse_report(line)
        with lock:
            if counts and state.get("id") == m.id:
                state["report"] = counts
    proc.wait()
    clean_partial(m)
    missing = [f for f in m.files if not (target(m) / f).exists()]
    with lock:
        state["proc"] = None
        if state["status"] != "running":  # cancelled
            return
        if proc.returncode != 0:
            text = [line for line in lines if line]
            state["status"] = "failed"
            state["error"] = text[-1] if text else f"Exit code {proc.returncode}."
        elif missing:
            state["status"] = "failed"
            state["error"] = f"The download finished without {', '.join(missing)}."
        else:
            state["status"] = "done"


def cancel(m: Model) -> str:
    """Stop m's download, or take it out of the queue; returns its status. Raises ValueError if m is neither."""
    with lock:
        if m.id in queue:
            queue.remove(m.id)
            history[m.id] = {
                "status": "cancelled",
                "error": "Cancelled.",
                "sizes": None,
            }
            return "cancelled"
        if state.get("id") != m.id:
            raise ValueError(f"{m.label} is not downloading.")
        if state["status"] != "running":
            return state["status"]
        state["status"], state["error"] = "cancelled", "Cancelled."
        proc = state["proc"]
    if proc is not None:
        try:
            # nothing in the child needs a clean exit; the download thread deletes the partial files
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:  # it ended on its own just now
            pass
    return "cancelled"


def clear_queue() -> list[str]:
    """Take every queued download out of the queue (the running one goes on); returns their ids."""
    with lock:
        gone = list(queue)
        queue.clear()
        for i in gone:
            history[i] = {"status": "cancelled", "error": "Cancelled.", "sizes": None}
    return gone


def stop() -> None:
    """Empty the queue and cancel the running download (the app is closing)."""
    clear_queue()
    with lock:
        running = state.get("id") if state.get("status") == "running" else None
    if running:
        cancel(BY_ID[running])


def progress(m: Model) -> dict[str, Any] | None:
    with lock:
        if m.id in queue:
            return {
                "status": "queued",
                "position": queue.index(m.id) + 1,
                "error": None,
                "done_gb": 0.0,
                "total_gb": None,
                "percent": 0.0,
                "speed_mbs": None,
                "eta_s": None,
                "idle_s": None,
                "stalled": False,
            }
        rec = state if state.get("id") == m.id else history.get(m.id)
        if rec is None:
            return None
        status, error, sizes = rec["status"], rec["error"], rec["sizes"]
        got = reported(m)
    total = sum(sizes.values()) if sizes else 0
    done = min(max(on_disk(m, sizes), got), total) if sizes else 0
    speed, idle = rate(m, done) if status == "running" and total else (None, None)
    return {
        "status": status,
        "position": None,
        "error": error,
        "done_gb": round(done / 1e9, 2),
        "total_gb": round(total / 1e9, 2) if total else None,
        "percent": percent(done, total) if total else 0.0,
        "speed_mbs": None if speed is None else round(speed / 1e6, 2),
        "eta_s": round((total - done) / speed) if speed else None,
        "idle_s": None if idle is None else int(idle),
        "stalled": idle is not None and idle >= STALL_S,
    }


def rate(m: Model, done: int) -> tuple[float | None, float | None]:
    """The bytes per second of m's running download and the seconds since its bytes last grew, from the bytes done at
    each progress call.

    The speed spans the samples of the last SPEED_WINDOW_S seconds plus one older sample, so it is an average over at
    least that window once the download has run that long. It is None until the samples span MIN_SPAN_S, and 0 only
    when no byte arrived in the whole window.
    """
    with lock:
        if state.get("id") != m.id:
            return None, None
        now = clock()
        samples = state.setdefault("samples", [])
        samples.append((now, done))
        while len(samples) > 2 and samples[1][0] <= now - SPEED_WINDOW_S:
            samples.pop(0)
        t0, b0 = samples[0]
        grew = state.get("grew")  # (when the bytes last grew, the bytes then)
        if grew is None or done > grew[1]:
            state["grew"] = grew = (now, done)
    idle = now - grew[0]
    if now - t0 < MIN_SPAN_S:
        return None, idle
    return max(0, done - b0) / (now - t0), idle


def view(m: Model, mem_gb: float) -> dict[str, Any]:
    return {
        "id": m.id,
        "label": m.label,
        "kind": "manual" if m.manual else "download",
        "used_by": [tools.BY_ID[t]["label"] for t in m.tools] + list(m.features),
        "tools": list(m.tools),
        "features": list(m.features),
        "repo": m.repo,
        "revision": m.revision,
        "size_gb": m.size_gb,
        "licence": m.licence,
        "licence_url": m.licence_url,
        "peak_gb": m.peak_gb,
        "gated": m.gated,
        "path_key": m.path_key,
        "path": str(target(m)),
        "installed": installed(m),
        "fits": None if m.peak_gb is None else fits(m.peak_gb, mem_gb),
        "disk_free_gb": None if m.manual else round(disk_free(m) / 1e9, 1),
        "needs": m.needs,
        "choice": list(m.choice) if m.choice else None,
        "optional": m.optional,
        "install": m.install,
        "install_path": str(p) if (p := install_target(m)) else None,
        "download": progress(m),
    }


def summary() -> dict[str, Any]:
    """The queue: the running and queued ids, the GB they still have to download, and the current run of downloads."""
    with lock:
        running = state["id"] if state.get("status") == "running" else None
        queued, run_ids, run_no = list(queue), list(batch), batch_id
        got = reported(BY_ID[running]) if running else 0
    waiting = ([running] if running else []) + queued
    left = sum(left_bytes(BY_ID[i], got if i == running else 0) for i in waiting)
    return {
        "running": running,
        "queued": queued,
        "count": len(waiting),
        "left_gb": round(left / 1e9, 1),
        "batch": run_ids,
        "batch_id": run_no,
        "disk_margin_gb": DISK_MARGIN_GB,
    }


def catalog() -> dict[str, Any]:
    mem_gb = mem_total_gb()
    return {
        "machine": {"mem_total_gb": mem_gb, "mem_reserve_gb": RESERVE_GB},
        "models": [view(m, mem_gb) for m in MODELS],
        "queue": summary(),
    }


class Report:
    """The progress bar class (tqdm_class) of the child's snapshot_download: it draws nothing and prints report lines.

    snapshot_download makes three bars of this class: "Downloading bytes" (bytes received from the network), the
    reconstruction bar (bytes written to the files) and a bar that counts finished files. The first two hold the bytes
    of the files this run downloads, so files that were on disk already are not in them. Xet updates the received
    bytes steadily but writes in large blocks; plain HTTP downloads update both together.
    """

    lock = threading.Lock()  # the bars are updated from the download threads
    counts: ClassVar[dict[str, int]] = {"bytes": 0, "transfer": 0}
    sent = float("-inf")

    def __init__(self, *args, desc: str = "", total=None, initial=0, unit="it", **kw):
        self.total, self.n = total, initial or 0
        self.kind = None  # the file count is not reported
        if unit == "B":
            self.kind = "transfer" if desc.startswith("Downloading bytes") else "bytes"

    def update(self, n: float | None = 1) -> None:
        with Report.lock:
            self.n += n or 0
            if self.kind is None:
                return
            Report.counts[self.kind] = int(self.n)
            now = clock()
            if now - Report.sent < REPORT_EVERY_S:
                return
            Report.sent = now
            print(REPORT_TAG + json.dumps(Report.counts), flush=True)

    @property
    def format_dict(self) -> dict[str, Any]:
        return {"rate": None}

    def set_description(self, desc: str | None = None, refresh: bool = True) -> None:
        pass

    set_description_str = set_description

    def set_postfix_str(self, s: str = "", refresh: bool = True) -> None:
        pass

    def refresh(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        pass


def fetch(m: Model) -> None:
    """Download m's weights at the pinned commit (runs in the child process)."""
    where = {"cache_dir": HF_HUB} if m.path_key is None else {"local_dir": target(m)}
    snapshot_download(
        m.repo,
        revision=m.revision,
        allow_patterns=list(m.allow) or None,
        ignore_patterns=list(m.ignore) or None,
        tqdm_class=Report,
        **where,
    )
    if m.path_key is None:
        # a download by commit writes no ref, and a runtime that loads the model by name offline reads refs/main
        ref = repo_cache(m) / "refs" / "main"
        if not ref.exists():
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(m.revision)


if __name__ == "__main__":
    model = BY_ID[sys.argv[1]]
    try:
        fetch(model)
    except Exception as e:  # noqa: BLE001 - the parent shows this last line to the user
        print(explain(e, model))
        sys.exit(1)
