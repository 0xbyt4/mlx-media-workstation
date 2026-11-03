"""The [agent] section of config.toml: the sandbox interpreter and the web search sources. Read once, at start.

- python: the interpreter run_python uses. Empty (the default) = the app's own base interpreter with "-I -S": the
  standard library only, nothing from any site-packages. Set it to a separate environment made for the sandbox (for
  example ~/mlx-media/venv-sandbox/bin/python with numpy, pandas, matplotlib) to have those packages; it then runs with
  "-I" (no user site folder, no PYTHON* variables) and the sandbox may read that environment's folder.
- searxng_url: the base URL of a SearXNG instance the user runs (with the JSON format on); empty = not used.
- wikipedia_language: the Wikipedia that web_search asks ("en", "de", "tr"...).
- marginalia: whether web_search also asks Marginalia Search with the shared key "public".
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app import config

DEFAULTS: dict[str, str | bool] = {
    "python": "",
    "searxng_url": "",
    "wikipedia_language": "en",
    "marginalia": True,
}
LANGUAGE = re.compile(r"[a-z]{2,3}(-[a-z]+)?")


@dataclass(frozen=True)
class Settings:
    python: Path | None = (
        None  # None = the app's base interpreter, standard library only
    )
    searxng_url: str = ""
    wikipedia_language: str = "en"
    marginalia: bool = True


def check(key: str, value: object) -> str | None:
    """The problem with an [agent] value, or None when it is valid."""
    if key == "marginalia":
        return None if type(value) is bool else "Use true or false."
    if not isinstance(value, str):
        return "Use a string."
    if key == "searxng_url" and value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "Use an http:// or https:// URL."
    if key == "wikipedia_language" and not LANGUAGE.fullmatch(value):
        return "Use a Wikipedia language code such as en."
    return None


def load(file: Path = config.FILE) -> Settings:
    values = config.section(file, "agent", DEFAULTS)
    bad = [f"{k} ({error})" for k, v in values.items() if (error := check(k, v))]
    if bad:
        raise SystemExit(f"{file.name}: bad value in [agent]: {'; '.join(bad)}")
    merged = {**DEFAULTS, **values}
    python = str(merged["python"]).strip()
    return Settings(
        python=config.ROOT / Path(python).expanduser() if python else None,
        searxng_url=str(merged["searxng_url"]).rstrip("/"),
        wikipedia_language=str(merged["wikipedia_language"]),
        marginalia=bool(merged["marginalia"]),
    )
