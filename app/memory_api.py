"""The Settings screen's memory endpoints (app/memory.py, app/chat_search.py): read the settings and the stores,
change them, clear the memory, build the chat search index again.

A change needs the Origin header of the app's own page (chat.same_origin): memory goes into every later chat's system
prompt. A store write carries the version the page read, so it never overwrites a change the agent made meanwhile.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import chat, chat_search, memory

router = APIRouter(prefix="/api/memory")


def view() -> dict[str, Any]:
    stores = memory.read()
    return {
        "settings": memory.load_settings(),
        "version": memory.version(stores),
        "max_entry": memory.MAX_ENTRY,
        "stores": [
            {
                "id": s,
                "label": memory.LABELS[s],
                "entries": stores[s],
                "used": memory.used(stores[s]),
                "cap": memory.CAPS[s],
            }
            for s in memory.STORES
        ],
        "search": chat_search.status(),
    }


@router.get("")
def get_memory() -> dict[str, Any]:
    return view()


class SettingsChange(BaseModel):
    enabled: bool | None = None
    ask: bool | None = None
    general: bool | None = None


@router.put("/settings")
def put_settings(body: SettingsChange, request: Request) -> dict[str, Any]:
    """Change some of the memory settings. They apply at once; a chat keeps the memory section it started with."""
    chat.same_origin(request)
    memory.save_settings(body.model_dump(exclude_none=True))
    return view()


class StoreChange(BaseModel):
    entries: list[str]
    version: str


@router.put("/stores/{store}")
def put_store(store: str, body: StoreChange, request: Request) -> dict[str, Any]:
    """Replace a store's entries (edit or delete entries). 409 when the memory changed since the page read it."""
    chat.same_origin(request)
    if store not in memory.STORES:
        raise HTTPException(404, f"No memory store {store!r}.")
    try:
        memory.set_store(store, body.entries, body.version)
    except memory.Changed as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return view()


@router.post("/clear")
def clear(request: Request) -> dict[str, Any]:
    """Delete every entry of both stores."""
    chat.same_origin(request)
    memory.clear()
    return view()


@router.post("/index")
def rebuild_index(request: Request) -> dict[str, Any]:
    """Build the chat search index again from outputs/chats."""
    chat.same_origin(request)
    if not chat_search.available():
        raise HTTPException(
            503, "Chat search needs SQLite with FTS5, which this Python does not have."
        )
    chat_search.rebuild()
    return view()
