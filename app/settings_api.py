"""The Settings screen's endpoints: the model paths and the prompt help provider in config.toml, and facts about the app.

The app reads config.toml once, at start (app/config.py), so a change here takes effect after a restart. A write
changes only the lines of the keys it sets: comments, blank lines, other keys and other sections stay as they are.
The new text is parsed again before it replaces the file, and the replace is atomic (a temp file, then os.replace).
Nothing here holds a secret. Writes pass the server's same-origin check.
"""

import functools
import os
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import assist, config, llm, models, tools

router = APIRouter(prefix="/api/settings")
lock = threading.Lock()

REPO_URL = "https://github.com/0xbyt4/mlx-media-workstation"
LABELS = {
    "mflux_bin": "mflux environment",
    "ltx_bin": "LTX-2.5 environment",
    "ltx_model": "LTX-2.5 weights",
    "drama_python": "Fish Drama 3 python",
    "drama_model": "Fish Drama 3 weights",
    "ace_dir": "ACE-Step 1.5 checkout",
    "qwen_python": "Local LLM python",
    "qwen_model": "Local LLM weights",
}
# features that use a path although no entry in app/models.py names it (app/llm.py starts the LLM with qwen_python)
FEATURES = {"qwen_python": llm.MODEL.features}
PROVIDER_TEXT = {
    "local": (
        "Local LLM",
        (
            "Runs on this Mac and sends nothing anywhere. It loads when you press Improve or send a chat "
            "message, and unloads when a job needs the memory."
        ),
    ),
}
# characters that a TOML basic string cannot hold as they are: quote, backslash and control characters
BAD_CHARS = re.compile(r'["\\\x00-\x1f\x7f]')
MAX_PATH = 1024


class Change(BaseModel):
    paths: dict[str, str] = {}
    assist: dict[str, str] = {}


def resolve(value: str) -> Path:
    """The path the app uses for a config value: "~" expanded, a relative path counted from the repository."""
    return config.ROOT / Path(value).expanduser()


def read_file(file: Path) -> tuple[str | None, dict[str, Any], str | None]:
    """The file's text (None when missing), its parsed data and the parse error."""
    if not file.exists():
        return None, {}, None
    text = file.read_bytes().decode()  # keeps CRLF line ends as they are
    try:
        return text, tomllib.loads(text), None
    except tomllib.TOMLDecodeError as e:
        return text, {}, f"{file.name} is not valid TOML: {e}"


def used_by(key: str) -> dict[str, list[str]]:
    entries = [m for m in models.MODELS if m.path_key == key]
    tool_ids = {t for m in entries for t in m.tools}
    return {
        "models": [m.label for m in entries],
        "tools": [t["label"] for t in tools.TOOLS if t["id"] in tool_ids],
        "features": sorted(
            {f for m in entries for f in m.features} | set(FEATURES.get(key, ()))
        ),
    }


@functools.cache
def version() -> dict[str, str | None]:
    """The app version from pyproject.toml, and the git commit when git and a checkout are there."""
    try:
        data = tomllib.loads((config.ROOT / "pyproject.toml").read_text())
        number = data["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        number = None
    commit = None
    git = shutil.which("git")
    if git and (config.ROOT / ".git").exists():
        try:
            r = subprocess.run(
                [git, "-C", str(config.ROOT), "log", "-1", "--format=%h %cs"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            commit = (r.stdout.strip() or None) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            commit = None
    return {"version": number, "commit": commit}


def snapshot() -> dict[str, Any]:
    file = config.FILE
    _, data, error = read_file(file)
    file_paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    file_assist = data.get("assist", {}) if isinstance(data.get("assist"), dict) else {}
    rows = []
    for key, default in config.DEFAULTS.items():
        value = file_paths.get(key)
        value = value if isinstance(value, str) else default
        path = resolve(value)
        active = getattr(config.PATHS, key)
        rows.append(
            {
                "key": key,
                "label": LABELS[key],
                "value": value,
                "default": default,
                "in_file": key in file_paths,
                "path": str(path),
                "exists": path.exists(),
                "active": str(active),
                "used_by": used_by(key),
            }
        )
    provider = file_assist.get("provider")
    provider = (
        provider if isinstance(provider, str) else config.ASSIST_DEFAULTS["provider"]
    )
    providers = []
    for name, (_, info) in assist.PROVIDERS.items():
        label, text = PROVIDER_TEXT.get(name, (name, ""))
        providers.append({"id": name, "label": label, "description": text, **info()})
    pending = provider != config.ASSIST.provider or any(
        r["path"] != r["active"] for r in rows
    )
    return {
        "config": {"path": str(file), "exists": file.exists(), "error": error},
        "paths": rows,
        "assist": {
            "provider": provider,
            "active": config.ASSIST.provider,
            "default": config.ASSIST_DEFAULTS["provider"],
            "providers": providers,
        },
        "llm": {"idle_stop_s": llm.IDLE_STOP},
        "about": {
            **version(),
            "root": str(config.ROOT),
            "outputs": str(config.ROOT / "outputs"),
            "readme": str(config.ROOT / "README.md"),
            "repo_url": REPO_URL,
        },
        "restart_required": pending,
    }


# ---------- writing config.toml ----------

HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?$")
TABLE = re.compile(r"""^\s*\[\[?\s*[A-Za-z0-9_."' -]+\s*\]\]?\s*(#.*)?$""")
COMMENT = re.compile(r"^\s*(#.*)?$")


def key_line(key: str) -> re.Pattern[str]:
    """A `key = "value"` line: indent, key (bare or quoted), equals, a one-line string, then the rest (comment)."""
    k = re.escape(key)
    return re.compile(
        rf"""^(\s*)(?:{k}|"{k}"|'{k}')(\s*=\s*)(?:"(?:[^"\\\n]|\\.)*"|'[^'\n]*')(.*)$"""
    )


def check_value(value: str) -> str:
    """The value to write; raises ValueError with the message for the field."""
    v = value.strip()
    if not v:
        raise ValueError("Enter a path, or press Use default.")
    if len(v) > MAX_PATH:
        raise ValueError(f"The path is longer than {MAX_PATH} characters.")
    if BAD_CHARS.search(v):
        raise ValueError(
            "The path cannot contain a double quote, a backslash or a line break."
        )
    return v


def set_keys(text: str, section: str, values: dict[str, str]) -> str:
    """text with section.key = "value" for each item: a key that is there gets its line replaced (indent and trailing
    comment kept), a missing key goes after the section's last key, a missing section goes at the end."""
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if (m := HEADER.match(line.rstrip("\r\n"))) and m.group(1) == section
        ),
        None,
    )
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"[{section}]\n")
        lines += [f'{k} = "{v}"\n' for k, v in values.items()]
        return "".join(lines)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if TABLE.match(lines[i].rstrip("\r\n"))
        ),
        len(lines),
    )
    missing = {}
    for k, v in values.items():
        pattern = key_line(k)
        for i in range(start + 1, end):
            body = lines[i].rstrip("\r\n")
            m = pattern.match(body)
            if m:
                newline = lines[i][len(body) :]
                lines[i] = f'{m.group(1)}{k}{m.group(2)}"{v}"{m.group(3)}{newline}'
                break
        else:
            missing[k] = v
    if missing:
        last = start
        for i in range(start + 1, end):
            if not COMMENT.match(lines[i]):
                last = i
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines[last + 1 : last + 1] = [f'{k} = "{v}"\n' for k, v in missing.items()]
    return "".join(lines)


def write_atomic(file: Path, text: str) -> None:
    target = file.resolve()
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    fd, tmp = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(change: Change) -> list[str]:
    """Write the changed keys to config.toml; returns the "section.key" names that changed."""
    errors: dict[str, str] = {}
    paths: dict[str, str] = {}
    for key, value in change.paths.items():
        if key not in config.DEFAULTS:
            errors[f"paths.{key}"] = "Unknown setting."
            continue
        try:
            paths[key] = check_value(value)
        except ValueError as e:
            errors[f"paths.{key}"] = str(e)
    helper: dict[str, str] = {}
    for key, value in change.assist.items():
        if key not in config.ASSIST_DEFAULTS:
            errors[f"assist.{key}"] = "Unknown setting."
        elif key == "provider" and value not in assist.PROVIDERS:
            errors[f"assist.{key}"] = (
                f"Unknown provider. Known: {', '.join(assist.PROVIDERS)}."
            )
        else:
            helper[key] = value
    if errors:
        raise HTTPException(
            400, {"message": "Some values are not valid.", "fields": errors}
        )

    with lock:
        file = config.FILE
        text, data, error = read_file(file)
        if error:
            raise HTTPException(409, f"{error}. Fix the file by hand, then try again.")
        text = text or ""
        changed = []
        new = text
        for section, values in (("paths", paths), ("assist", helper)):
            old = data.get(section, {})
            if not isinstance(old, dict):
                raise HTTPException(
                    409, f"[{section}] in {file.name} is not a table. Fix it by hand."
                )
            todo = {k: v for k, v in values.items() if old.get(k) != v}
            if todo:
                new = set_keys(new, section, todo)
                changed += [f"{section}.{k}" for k in todo]
        if not changed:
            return []
        # the new file must read back as the old data plus exactly these changes
        expected = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
        for section, values in (("paths", paths), ("assist", helper)):
            if values:
                expected.setdefault(section, {}).update(values)
        try:
            ok = tomllib.loads(new) == expected
        except tomllib.TOMLDecodeError:
            ok = False
        if not ok:
            raise HTTPException(
                409,
                f"{file.name} has a form this screen cannot edit safely. Change it by hand: {file}",
            )
        write_atomic(file, new)
        return changed


@router.get("")
def get_settings() -> dict[str, Any]:
    return snapshot()


@router.put("")
def put_settings(change: Change) -> dict[str, Any]:
    changed = update(change)
    return {**snapshot(), "changed": changed}
