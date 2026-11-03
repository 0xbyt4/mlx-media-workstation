"""The [agent] section of config.toml: the sandbox interpreter and the web search sources. Read once, at start: the
registry (app/agent/__init__.py) gets these values when it is built, so a change from the Settings screen takes effect
after a restart.

- python: the interpreter run_python uses. Empty (the default) = the app's own base interpreter with "-I -S": the
  standard library only, nothing from any site-packages. Set it to a separate environment made for the sandbox (for
  example ~/mlx-media/venv-sandbox/bin/python with numpy, pandas, matplotlib) to have those packages; it then runs with
  "-I" (no user site folder, no PYTHON* variables) and the sandbox may read that environment's folder.
- searxng_url: the base URL of a SearXNG instance the user runs (with the JSON format on); empty = not used.
- wikipedia_language: the Wikipedia that web_search asks ("en", "de", "tr"...).
- marginalia: whether web_search also asks Marginalia Search with the shared key "public".
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app import config
from app.agent import sandbox

DEFAULTS: dict[str, str | bool] = {
    "python": "",
    "searxng_url": "",
    "wikipedia_language": "en",
    "marginalia": True,
}
LANGUAGE = re.compile(r"[a-z]{2,3}(-[a-z]+)?")
# the Wikipedia languages the Settings screen offers
LANGUAGES = ("en", "tr", "de", "fr", "es", "it", "ja", "zh")
# characters that a TOML basic string cannot hold as they are: quote, backslash and control characters
BAD_CHARS = re.compile(r'["\\\x00-\x1f\x7f]')
MAX_TEXT = 1024


@dataclass(frozen=True)
class Settings:
    python: Path | None = (
        None  # None = the app's base interpreter, standard library only
    )
    searxng_url: str = ""
    wikipedia_language: str = "en"
    marginalia: bool = True


def check_url(value: str) -> str | None:
    """The problem with a SearXNG base URL: http(s), a host, no user info, no query or fragment."""
    try:
        parts = urlsplit(value)
        # reading the port raises ValueError for one that is not a number from 0 to 65535
        _ = parts.port
    except ValueError:
        return "Use an http:// or https:// URL."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Use an http:// or https:// URL."
    if parts.username is not None or parts.password is not None:
        return "Leave the user name and password out of the URL."
    if parts.query or parts.fragment or "?" in value or "#" in value:
        return "Use the base URL, without ? or #."
    return None


def check(key: str, value: object) -> str | None:
    """The problem with an [agent] value, or None when it is valid."""
    if key == "marginalia":
        return None if type(value) is bool else "Use true or false."
    if not isinstance(value, str):
        return "Use a string."
    if key == "searxng_url" and value:
        return check_url(value)
    if key == "wikipedia_language" and not LANGUAGE.fullmatch(value):
        return "Use a Wikipedia language code such as en."
    return None


def interpreter(value: str) -> Path | None:
    """The interpreter a python value names: "~" expanded, a relative path counted from the repository."""
    value = value.strip()
    return config.ROOT / Path(value).expanduser() if value else None


def clean(key: str, value: object) -> str | bool:
    """A value from the Settings screen, ready to write; raises ValueError with the message for the field. Stricter
    than check: the python must be an absolute path to an executable file that is there now, and no other user may
    be able to change it (sandbox.unsafe_interpreter: the app runs it outside the sandbox once)."""
    if key not in DEFAULTS:
        raise ValueError("Unknown setting.")
    if isinstance(value, str):
        value = value.strip()
        if len(value) > MAX_TEXT:
            raise ValueError(f"The value is longer than {MAX_TEXT} characters.")
        if BAD_CHARS.search(value):
            raise ValueError(
                "The value cannot contain a double quote, a backslash or a line break."
            )
    if error := check(key, value):
        raise ValueError(error)
    if key == "python" and value:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("Use an absolute path, for example ~/env/bin/python.")
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"No executable file at {path}.")
        if error := sandbox.unsafe_interpreter(path):
            raise ValueError(error)
    return value if isinstance(value, bool) else str(value)


def from_values(values: dict[str, object]) -> Settings:
    """Settings from valid [agent] values; a missing key takes its default."""
    merged = {**DEFAULTS, **values}
    return Settings(
        python=interpreter(str(merged["python"])),
        searxng_url=str(merged["searxng_url"]).rstrip("/"),
        wikipedia_language=str(merged["wikipedia_language"]),
        marginalia=bool(merged["marginalia"]),
    )


def load(file: Path = config.FILE) -> Settings:
    values = config.section(file, "agent", DEFAULTS)
    bad = [f"{k} ({error})" for k, v in values.items() if (error := check(k, v))]
    if bad:
        raise SystemExit(f"{file.name}: bad value in [agent]: {'; '.join(bad)}")
    return from_values(values)
