"""Where the models are installed, and which prompt helper to use: read from config.toml next to start.py (copy
config.example.toml to start).

The code holds no machine-specific paths. Any path left out of config.toml takes the default below (the layout the
README's install steps make). A tool whose files are missing is refused when a job is submitted, with the path it
looked for; the other tools keep working. The [assist] section picks the prompt help provider (app/assist.py).

The [app] section holds the switches of the Settings screen. Unlike the paths, they apply while the app runs: a change
through PUT /api/settings replaces APP at once (app/settings_api.py). `profile` picks what the UI shows: "full" (every
section) or "chat" (Chat, Models and Settings only). `start.py --chat` sets it to "chat" in APP for that run, without
writing the file. `chat_effort` is the thinking level of a new chat (app/effort.py).
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.effort import DEFAULT as DEFAULT_EFFORT
from app.effort import LEVELS

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
APP_DEFAULTS: dict[str, bool | int | str] = {
    "keep_awake": True,
    "notify": True,
    "llm_idle_minutes": 10,
    "profile": "full",
    "chat_effort": DEFAULT_EFFORT,
}
IDLE_CHOICES = (5, 10, 30, 60, 0)  # the minutes the Settings screen offers; 0 = never
MAX_IDLE = 24 * 60
PROFILES = ("full", "chat")  # every section; Chat, Models and Settings only


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


@dataclass(frozen=True)
class App:
    keep_awake: (
        bool  # hold off system sleep while a job is queued or running (caffeinate -i)
    )
    notify: bool  # a macOS notification when a job is done or failed
    llm_idle_minutes: int  # minutes without use before the local LLM unloads; 0 = never
    profile: str = "full"  # what the UI shows: "full" or "chat" (PROFILES)
    # the thinking level of a new chat (app/effort.py LEVELS)
    chat_effort: str = DEFAULT_EFFORT


def section(file: Path, name: str, known: Mapping[str, object]) -> dict[str, object]:
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


def check_app(key: str, value: object) -> str | None:
    """The problem with an [app] value, or None when it is valid."""
    if key == "llm_idle_minutes":
        if type(value) is not int or not 0 <= value <= MAX_IDLE:
            return f"Enter whole minutes from 0 (never) to {MAX_IDLE}."
    elif key == "profile":
        if value not in PROFILES:
            return f"Use one of: {', '.join(PROFILES)}."
    elif key == "chat_effort":
        if value not in LEVELS:
            return f"Use one of: {', '.join(LEVELS)}."
    elif type(value) is not bool:
        return "Use true or false."
    return None


def load_app(file: Path = FILE) -> App:
    values = section(file, "app", APP_DEFAULTS)
    bad = [f"{k} ({error})" for k, v in values.items() if (error := check_app(k, v))]
    if bad:
        raise SystemExit(f"{file.name}: bad value in [app]: {'; '.join(bad)}")
    return App(**{k: values.get(k, v) for k, v in APP_DEFAULTS.items()})


PATHS = load()
ASSIST = load_assist()
APP = load_app()
