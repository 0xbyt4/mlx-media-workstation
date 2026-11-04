"""The endpoints of Settings > Skills (app/skills.py): list the skills, show a SKILL.md, turn a skill on or off, set
the user's skills folder, import a skill folder or .zip from this Mac (check first, then confirm) and delete an
imported skill. There is no install from the network.

A change needs the Origin of the app's own page (chat.same_origin), as the settings and the approvals do. The import
check copies the skill into outputs/skills/.staging/ first, so the files the user reads are the files that the
confirm moves into outputs/skills/.
"""

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import chat, skills
from app.agent.extensions import tokens

router = APIRouter(prefix="/api/skills")


def row(s: skills.Skill, state: dict[str, Any]) -> dict[str, Any]:
    tools, net = skills.needs(s)
    return {
        "name": s.name,
        "source": s.source,
        "path": str(s.path),
        "description": s.description,
        "license": s.license,
        "compatibility": s.compatibility,
        "metadata": s.metadata,
        "allowed_tools": list(s.allowed_tools),
        "needs": tools + (["network"] if net else []),
        "manual": s.manual,
        "files": list(s.files),
        "scripts": s.scripts(),
        "enabled": skills.is_on(s, state),
        "error": s.error,
        "warnings": list(s.warnings),
        "tokens": tokens(s.body),
    }


def snapshot() -> dict[str, Any]:
    state = skills.load_state()
    found = skills.discover(state)
    on = [s for s in found if skills.is_on(s, state) and not s.manual]
    section = skills.catalog(on)
    return {
        "skills": [row(s, state) for s in found],
        "folder": state["folder"],
        "user_dir": str(skills.USER_DIR),
        "app_dir": str(skills.APP_DIR),
        "budget": skills.BUDGET,
        "catalog_tokens": tokens(section.text) if section else 0,
    }


def get(name: str, source: str) -> skills.Skill:
    found = next(
        (s for s in skills.discover() if s.name == name and s.source == source), None
    )
    if found is None:
        raise HTTPException(404, "No such skill.")
    return found


@router.get("")
def list_skills() -> dict[str, Any]:
    return snapshot()


@router.get("/{source}/{name}")
def view(source: str, name: str) -> dict[str, Any]:
    """The SKILL.md text of a skill, as it is on disk (also for a broken one)."""
    s = get(name, source)
    try:
        size = (s.path / "SKILL.md").stat().st_size
        if size > skills.MAX_SKILL_MD:
            raise HTTPException(400, "SKILL.md is too large to show.")
        text = (s.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HTTPException(404, "The skill has no readable SKILL.md.") from None
    return {"name": s.name, "source": s.source, "text": text, "files": list(s.files)}


class Switch(BaseModel):
    enabled: bool


@router.put("/{source}/{name}")
def switch(source: str, name: str, body: Switch, request: Request) -> dict[str, Any]:
    chat.same_origin(request)
    get(name, source)
    try:
        skills.switch(name, body.enabled)
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None
    return snapshot()


@router.delete("/user/{name}")
def delete(name: str, request: Request) -> dict[str, Any]:
    chat.same_origin(request)
    try:
        skills.delete(name)
    except skills.SkillError as e:
        raise HTTPException(404, str(e)) from None
    return snapshot()


class Folder(BaseModel):
    folder: str | None = None


@router.put("/folder")
def set_folder(body: Folder, request: Request) -> dict[str, Any]:
    """The user's skills folder; null or "" = none."""
    chat.same_origin(request)
    value = (body.folder or "").strip()
    if value:
        value = check_folder(value)
    skills.set_folder(value or None)
    return snapshot()


def check_folder(text: str) -> str:
    """The real path of a skills folder; HTTP 400 for a relative path, a missing folder, a top-level folder or the
    app's own skill folders. The app only reads this folder (~/.agents/skills, a hidden folder, is fine)."""
    raw = Path(text).expanduser()
    if not raw.is_absolute():
        raise HTTPException(
            400, "Enter the full path of a folder, such as /Users/you/.agents/skills."
        )
    try:
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, f"There is no folder {raw}.") from None
    if not path.is_dir():
        raise HTTPException(400, f"{path} is a file. Choose a folder.")
    if path.parent == Path("/") or path == Path("/"):
        raise HTTPException(
            400, f"{path} is too broad. Choose the folder that holds the skills."
        )
    own = [skills.USER_DIR.resolve(), skills.APP_DIR.resolve()]
    if any(path == p or p in path.parents or path in p.parents for p in own):
        raise HTTPException(
            400, "The app reads its own skill folders already. Choose another folder."
        )
    return str(path)


class Source(BaseModel):
    path: str


class Token(BaseModel):
    token: str


@router.post("/import/check")
async def check(body: Source, request: Request) -> dict[str, Any]:
    """Copy a skill folder or .zip into the staging folder and return what the user must see before the import:
    the files, the scripts, the findings of the scan and the SKILL.md."""
    chat.same_origin(request)
    path = Path(body.path.strip()).expanduser()
    try:  # copying takes a moment: off the event loop
        return await asyncio.to_thread(skills.stage, path)
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(400, f"Cannot read it: {e.strerror or e}") from None


@router.post("/import")
def confirm(body: Token, request: Request) -> dict[str, Any]:
    """Import the checked skill after the user confirmed it; it is on for new chats."""
    chat.same_origin(request)
    try:
        name = skills.confirm(body.token)
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None
    return {"name": name, **snapshot()}
