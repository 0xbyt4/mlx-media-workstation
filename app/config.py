"""Where the models are installed, and which prompt helper to use: read from config.toml next to start.py (copy
config.example.toml to start).

The code holds no machine-specific paths. Any path left out of config.toml takes the default below (the layout the
README's install steps make). A tool whose files are missing is refused when a job is submitted, with the path it
looked for; the other tools keep working. The [assist] section picks the prompt help provider (app/assist.py).
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "config.toml"

DEFAULTS = {
    "mflux_bin": "~/mlx-media/mflux/.venv/bin",
    "ltx_bin": "~/mlx-media/venv-ltx/bin",
    "ltx_model": "~/mlx-media/models/ltx-2.5-mlx-q8",
    "drama_python": "~/mlx-media/venv-drama/bin/python",
    "drama_model": "~/mlx-media/models/fish-audio-drama-3-4bit",
    "ace_dir": "~/mlx-media/ace-step",
    "qwen_python": "~/mlx-media/venv-qwen/bin/python",
    "qwen_model": "~/mlx-media/models/qwen3.8-27b-4bit",
}
ASSIST_DEFAULTS = {"provider": "local"}


@dataclass(frozen=True)
class Paths:
    mflux_bin: Path  # the mflux checkout's .venv/bin (MOSS-Image, FLUX.3 Klein)
    ltx_bin: Path  # the LTX environment's bin: ltx-2-mlx and python
    ltx_model: Path  # LTX-2.5 weights folder
    drama_python: Path  # python of the Fish Drama 3 environment (mlx-audio)
    drama_model: Path  # Fish Drama 3 weights folder
    ace_dir: Path  # the ACE-Step 1.5 checkout (its .venv and checkpoints inside)
    qwen_python: Path  # python of the local LLM environment (prompt help and chat)
    qwen_model: Path  # Qwen3.8 27B 4-bit weights folder


@dataclass(frozen=True)
class Assist:
    provider: str  # who writes prompt help: "local" (the local LLM)


def section(file: Path, name: str, known: dict[str, str]) -> dict[str, object]:
    data = tomllib.loads(file.read_text()) if file.exists() else {}
    values = data.get(name, {})
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise SystemExit(
            f"{file.name}: unknown key(s) in [{name}]: {', '.join(unknown)}. Known keys: {', '.join(known)}."
        )
    return values


def load(file: Path = FILE) -> Paths:
    paths = section(file, "paths", DEFAULTS)
    # a relative path counts from the repository folder, whatever folder the app was started from
    return Paths(
        **{
            k: ROOT / Path(str(paths.get(k, v))).expanduser()
            for k, v in DEFAULTS.items()
        }
    )


def load_assist(file: Path = FILE) -> Assist:
    values = section(file, "assist", ASSIST_DEFAULTS)
    return Assist(**{k: str(values.get(k, v)) for k, v in ASSIST_DEFAULTS.items()})


PATHS = load()
ASSIST = load_assist()
