"""The Models screen: what each tool needs, what is installed, whether it fits this Mac, and in-app weight downloads.

Weights download with huggingface_hub at a pinned commit, into the path config.toml gives them, or into the Hugging
Face cache for the models that mflux and mlx-audio load by name. Runtimes (model environments and checkouts) stay
manual: their entry carries the README commands. One download runs at a time, in a child process
(`python -m app.models <id>`), so a cancel stops it at once. Finished files stay and the next download skips them;
partial files are deleted. Progress is the bytes on disk against the file sizes the Hub lists for that commit.
"""

import errno
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from app.config import PATHS, ROOT

HF_HUB = tools.HF_HUB
# macOS and the app itself keep about 8 GB in use. A model fits when its peak plus that reserve is within the total
# memory; above it macOS swaps and the job gets much slower or fails (Fish Drama 3 at 39 GB on our 48 GB Mac is the
# tightest case we ran).
RESERVE_GB = 8.0
# The download speed is the bytes added over at least this many seconds of progress samples (the UI polls every 2 s),
# so one slow or fast poll does not swing it. Below MIN_SPAN_S of samples there is no speed yet.
SPEED_WINDOW_S = 10.0
MIN_SPAN_S = 1.5
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
    install: str | None = None  # the README commands of a manual entry

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
        tools=("voice.drama3",),
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
    # the speech check runs inside the Drama 3 job, so its peak is that job's; the runner loads it by name
    Model(
        id="qwen3-asr-0.6b-8bit",
        label="Qwen3-ASR 0.6B (speech check)",
        tools=("voice.drama3",),
        path_key=None,
        files=("config.json", "model.safetensors", "vocab.json"),
        repo="mlx-community/Qwen3-ASR-0.6B-8bit",
        revision="89e96d92ba34aca20b3e29fb10cc284097d1219f",
        size_gb=1.0,
        licence="Apache-2.0",
        licence_url="https://huggingface.co/mlx-community/Qwen3-ASR-0.6B-8bit",
        peak_gb=39.0,
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
        licence="LTX-2 Community License",
        licence_url="https://huggingface.co/dgrauet/ltx-2.5-mlx-q8/blob/746ca9aacb697d2c739f544d68b584214dedcc75/LICENSE",
        peak_gb=35.0,
        gated=True,
        ignore=("transformer-dev.safetensors",),
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
        licence_url="https://huggingface.co/mlx-community/Qwen3.8-27B-4bit",
        peak_gb=18.0,
        features=("Prompt help", "Chat"),
    ),
]
BY_ID = {m.id: m for m in MODELS}

lock = threading.Lock()
# the current or last download: id, status (running, done, failed, cancelled), error, sizes and the child process
state: dict[str, Any] = {}
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


def tool_peak(tool_id: str) -> float:
    """The peak memory in GB of a job of this tool (0 for tools that load no model)."""
    return max((m.peak_gb or 0.0 for m in MODELS if tool_id in m.tools), default=0.0)


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


def start(m: Model) -> None:
    """Start downloading m in the background. Raises ValueError (shown to the user) when it cannot start."""
    if m.manual:
        raise ValueError(
            f"{m.label} is installed with the commands shown here (README.md)."
        )
    if installed(m):
        raise ValueError(f"{m.label} is already installed.")
    if m.needs and not installed(BY_ID[m.needs]):
        raise ValueError(f"Install {BY_ID[m.needs].label} first.")
    with lock:
        if state.get("status") == "running":
            raise ValueError(
                f"{BY_ID[state['id']].label} is downloading. Wait for it or cancel it."
            )
        state.clear()
        state.update(
            id=m.id, status="running", error=None, sizes=None, proc=None, samples=[]
        )
    threading.Thread(target=run, args=(m,), daemon=True).start()


def fail(message: str) -> None:
    with lock:
        if state["status"] == "running":
            state["status"], state["error"] = "failed", message


def run(m: Model) -> None:
    """The download thread: file sizes from the Hub, a disk space check, then the child process."""
    try:
        sizes = remote_sizes(m)
    except Exception as e:  # noqa: BLE001 - any failure belongs to this download
        fail(explain(e, m))
        return
    clean_partial(m)  # left by an app that closed or crashed mid-download
    need, free = sum(sizes.values()) - on_disk(m, sizes), disk_free(m)
    if need > free:
        fail(
            f"Not enough disk space: {m.label} needs {need / 1e9:.1f} GB more, "
            f"and {free / 1e9:.1f} GB is free."
        )
        return
    with lock:  # check and start under the lock, so a cancel cannot slip in between
        if state["status"] != "running":
            return
        state["sizes"] = sizes
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
    out, _ = proc.communicate()
    clean_partial(m)
    missing = [f for f in m.files if not (target(m) / f).exists()]
    with lock:
        state["proc"] = None
        if state["status"] != "running":  # cancelled
            return
        if proc.returncode != 0:
            lines = out.strip().splitlines()
            state["status"] = "failed"
            state["error"] = lines[-1] if lines else f"Exit code {proc.returncode}."
        elif missing:
            state["status"] = "failed"
            state["error"] = f"The download finished without {', '.join(missing)}."
        else:
            state["status"] = "done"


def cancel(m: Model) -> str:
    """Stop m's download; returns the download's status. Raises ValueError if m is not downloading."""
    with lock:
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


def stop() -> None:
    """Cancel the running download (the app is closing)."""
    with lock:
        running = state.get("id") if state.get("status") == "running" else None
    if running:
        cancel(BY_ID[running])


def progress(m: Model) -> dict[str, Any] | None:
    with lock:
        if state.get("id") != m.id:
            return None
        status, error, sizes = state["status"], state["error"], state["sizes"]
    total = sum(sizes.values()) if sizes else 0
    done = min(on_disk(m, sizes), total) if sizes else 0
    speed = rate(m, done) if status == "running" and total else None
    return {
        "status": status,
        "error": error,
        "done_gb": round(done / 1e9, 2),
        "total_gb": round(total / 1e9, 2) if total else None,
        "percent": percent(done, total) if total else 0.0,
        "speed_mbs": None if speed is None else round(speed / 1e6, 1),
        "eta_s": round((total - done) / speed) if speed else None,
    }


def rate(m: Model, done: int) -> float | None:
    """Bytes per second of m's running download, from the bytes on disk at each progress call.

    The speed spans the samples of the last SPEED_WINDOW_S seconds plus one older sample, so it is an average over at
    least that window once the download has run that long. None until the samples span MIN_SPAN_S.
    """
    with lock:
        if state.get("id") != m.id:
            return None
        now = clock()
        samples = state.setdefault("samples", [])
        samples.append((now, done))
        while len(samples) > 2 and samples[1][0] <= now - SPEED_WINDOW_S:
            samples.pop(0)
        t0, b0 = samples[0]
    if now - t0 < MIN_SPAN_S:
        return None
    return max(0, done - b0) / (now - t0)


def view(m: Model, mem_gb: float) -> dict[str, Any]:
    return {
        "id": m.id,
        "label": m.label,
        "kind": "manual" if m.manual else "download",
        "used_by": [tools.BY_ID[t]["label"] for t in m.tools] + list(m.features),
        "tools": list(m.tools),
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
        "install": m.install,
        "download": progress(m),
    }


def catalog() -> dict[str, Any]:
    mem_gb = mem_total_gb()
    return {
        "machine": {"mem_total_gb": mem_gb, "mem_reserve_gb": RESERVE_GB},
        "models": [view(m, mem_gb) for m in MODELS],
    }


def fetch(m: Model) -> None:
    """Download m's weights at the pinned commit (runs in the child process)."""
    where = {"cache_dir": HF_HUB} if m.path_key is None else {"local_dir": target(m)}
    snapshot_download(
        m.repo,
        revision=m.revision,
        allow_patterns=list(m.allow) or None,
        ignore_patterns=list(m.ignore) or None,
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
