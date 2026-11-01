"""Where the models are installed: read from config.toml next to start.py (copy config.example.toml to start).

The code holds no machine-specific paths. Any path left out of config.toml takes the default below (the layout the
README's install steps make). A tool whose files are missing is refused when a job is submitted, with the path it
looked for; the other tools keep working.
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
}


@dataclass(frozen=True)
class Paths:
    mflux_bin: Path  # the mflux checkout's .venv/bin (MOSS-Image, FLUX.3 Klein)
    ltx_bin: Path  # the LTX environment's bin: ltx-2-mlx and python
    ltx_model: Path  # LTX-2.5 weights folder
    drama_python: Path  # python of the Fish Drama 3 environment (mlx-audio)
    drama_model: Path  # Fish Drama 3 weights folder
    ace_dir: Path  # the ACE-Step 1.5 checkout (its .venv and checkpoints inside)


def load(file: Path = FILE) -> Paths:
    data = tomllib.loads(file.read_text()) if file.exists() else {}
    paths = data.get("paths", {})
    unknown = sorted(set(paths) - set(DEFAULTS))
    if unknown:
        raise SystemExit(
            f"{file.name}: unknown key(s) in [paths]: {', '.join(unknown)}. Known keys: {', '.join(DEFAULTS)}."
        )
    # a relative path counts from the repository folder, whatever folder the app was started from
    return Paths(
        **{
            k: ROOT / Path(str(paths.get(k, v))).expanduser()
            for k, v in DEFAULTS.items()
        }
    )


PATHS = load()
