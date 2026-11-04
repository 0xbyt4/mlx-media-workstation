"""The endpoints of Settings > MCP servers (app/mcp_client.py, docs/API.md "MCP servers").

Every write needs the Origin of the app's own page (chat.same_origin): a server entry names a program the app runs.
No answer holds a secret: an environment variable or a bearer token shows only as "set" or "not set".
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import chat, mcp_client
from app.agent.mcp_tools import MAX_DESCRIPTION, MAX_DIRECT, parameters, tool_name
from app.agent.registry import visible

router = APIRouter(prefix="/api/mcp")
MAX_SHOWN = (
    MAX_DESCRIPTION  # characters of a tool's description: as much as the model gets
)


class ServerIn(BaseModel):
    name: str | None = None  # only when adding
    kind: str | None = None  # only when adding: "stdio" or "remote"
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env: list[str] | None = None  # names; the values go in `secrets`
    url: str | None = None
    exposure: str | None = None
    enabled: bool | None = None
    # name -> value for the Keychain; "" or null removes the value, a name not sent keeps it
    secrets: dict[str, str | None] = {}


class Approve(BaseModel):
    approval: str  # the hash the page showed with the command


class ToolsIn(BaseModel):
    approve: dict[str, str] = {}  # tool -> the definition hash the page showed
    revoke: list[str] = []


def hints(d: dict[str, Any]) -> dict[str, Any]:
    a = d.get("annotations") or {}
    keys = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    return {k: a[k] for k in keys if isinstance(a.get(k), bool)}


def schema_text(d: dict[str, Any]) -> str | None:
    params = parameters(d)
    return None if params is None else json.dumps(params, ensure_ascii=False, indent=1)


def tools_view(
    entry: dict[str, Any], live: dict[str, Any] | None
) -> list[dict[str, Any]]:
    saved = entry.get("tools") or {}
    out = []
    for name in sorted(set(saved) | set(live or {})):
        d = (live or {}).get(name) or saved[name]["definition"]
        text = visible(str(d.get("description") or ""))
        changed = (
            name in saved
            and live is not None
            and name in live
            and live[name] != saved[name]["definition"]
        )
        out.append(
            {
                "name": name,
                "label": visible(name),
                "full_name": tool_name(entry["name"], name),
                "title": visible(str(d.get("title") or "")),
                "description": text
                if len(text) <= MAX_SHOWN
                else text[: MAX_SHOWN - 3] + "...",
                "hints": hints(d),
                # the input schema as the model gets it (None: too large, so the tool is not offered)
                "schema": schema_text(d),
                "state": mcp_client.tool_state(entry, name, live),
                # the description the user approved, when the server changed the tool since
                "approved_description": visible(
                    str(saved[name]["definition"].get("description") or "")
                )[:MAX_SHOWN]
                if changed
                else None,
                # the schema the user approved, when the server changed the tool since (also a change of the
                # schema alone, which the descriptions do not show)
                "approved_schema": schema_text(saved[name]["definition"])
                if changed
                else None,
                # what an approval of the current definition sends back
                "hash": mcp_client.definition_hash(live[name])
                if live and name in live
                else None,
            }
        )
    return out


def view(entry: dict[str, Any]) -> dict[str, Any]:
    conn = mcp_client.manager().peek(entry["name"])
    live = conn.live if conn else None
    stdio = entry["kind"] == "stdio"
    out: dict[str, Any] = {
        "name": entry["name"],
        "kind": entry["kind"],
        "enabled": bool(entry.get("enabled")),
        "exposure": entry.get("exposure", "deferred"),
        "approval": mcp_client.approval(entry),
        "approved": mcp_client.approved(entry),
        "warnings": mcp_client.warnings(entry),
        "state": conn.state if conn else "stopped",
        "error": conn.error if conn else None,
        "tools": tools_view(entry, live),
        "listed": live
        is not None,  # the tool states compare with the server's own list
    }
    if stdio:
        out |= {
            "command": entry["command"],
            "args": entry["args"],
            "cwd": entry["cwd"],
            "env": [
                {"name": n, "set": n in entry.get("secrets", [])} for n in entry["env"]
            ],
            "command_line": mcp_client.command_line(entry),
            "log": f"outputs/mcp/{entry['name']}.log",
        }
    else:
        out |= {
            "url": entry["url"],
            "token_set": mcp_client.TOKEN in entry.get("secrets", []),
        }
    return out


def fields_error(e: mcp_client.ConfigError) -> HTTPException:
    return HTTPException(
        400, {"message": "Some values are not valid.", "fields": e.fields}
    )


def entry_or_404(data: dict[str, Any], name: str) -> dict[str, Any]:
    entry = mcp_client.find(data, name)
    if entry is None:
        raise HTTPException(404, "No such MCP server")
    return entry


@router.get("/servers")
def list_servers() -> dict[str, Any]:
    with mcp_client.lock:
        data = mcp_client.load()
    return {
        "servers": [view(s) for s in data["servers"]],
        "max_servers": mcp_client.MAX_SERVERS,
        "max_direct": MAX_DIRECT,
        "keychain_service": mcp_client.KEYCHAIN_SERVICE,
    }


@router.post("/servers")
def add_server(body: ServerIn, request: Request) -> dict[str, Any]:
    chat.same_origin(request)
    with mcp_client.lock:
        data = mcp_client.load()
        if len(data["servers"]) >= mcp_client.MAX_SERVERS:
            raise HTTPException(
                409, f"At most {mcp_client.MAX_SERVERS} MCP servers. Remove one first."
            )
        if body.name and mcp_client.find(data, body.name):
            raise HTTPException(
                409,
                {
                    "message": "Name in use.",
                    "fields": {"name": "A server has this name."},
                },
            )
        raw = body.model_dump(exclude={"secrets", "enabled"}, exclude_none=True)
        try:
            entry = mcp_client.checked(raw)
            mcp_client.update_secrets(entry, body.secrets)
        except mcp_client.ConfigError as e:
            raise fields_error(e) from None
        except OSError as e:
            raise HTTPException(500, str(e)) from None
        data["servers"].append(entry)
        mcp_client.save(data)
    return view(entry)


@router.put("/servers/{name}")
def edit_server(name: str, body: ServerIn, request: Request) -> dict[str, Any]:
    """Change the given fields only. A change of the command line or URL needs a new approval; the server stops."""
    chat.same_origin(request)
    with mcp_client.lock:
        data = mcp_client.load()
        old = entry_or_404(data, name)
        raw = old | body.model_dump(
            exclude={"secrets", "enabled", "name", "kind"}, exclude_none=True
        )
        try:
            entry = mcp_client.checked(raw, old)
            mcp_client.update_secrets(entry, body.secrets)
        except mcp_client.ConfigError as e:
            raise fields_error(e) from None
        except OSError as e:
            raise HTTPException(500, str(e)) from None
        if body.enabled is not None:
            entry["enabled"] = body.enabled
        restart = (
            mcp_client.approval(entry) != mcp_client.approval(old)
            or not entry["enabled"]
            or bool(body.secrets)
        )
        data["servers"] = [entry if s is old else s for s in data["servers"]]
        mcp_client.save(data)
    if restart:
        mcp_client.manager().stop(name)
    return view(entry)


@router.delete("/servers/{name}")
def remove_server(name: str, request: Request) -> dict[str, Any]:
    chat.same_origin(request)
    with mcp_client.lock:
        data = mcp_client.load()
        entry = entry_or_404(data, name)
        if entry.get("secrets"):
            mcp_client.keychain.delete(name)
        data["servers"] = [s for s in data["servers"] if s is not entry]
        mcp_client.save(data)
    mcp_client.manager().forget(name)
    return {"removed": name}


@router.post("/servers/{name}/approve")
def approve_server(name: str, body: Approve, request: Request) -> dict[str, Any]:
    """Approve the command line (or URL) the page showed: its hash must still be the current one."""
    chat.same_origin(request)
    with mcp_client.lock:
        data = mcp_client.load()
        entry = entry_or_404(data, name)
        if body.approval != mcp_client.approval(entry):
            raise HTTPException(
                409, "The command changed after the page showed it. Read it again."
            )
        if entry["kind"] == "stdio" and mcp_client.resolve(entry["command"]) is None:
            raise HTTPException(
                409,
                f"The program {entry['command']} was not found. Enter its absolute path, then approve it.",
            )
        entry["approved"] = body.approval
        mcp_client.save(data)
    return view(entry)


@router.post("/servers/{name}/test")
def test_server(name: str, request: Request) -> dict[str, Any]:
    """Start the server again and list its tools. Works also when the server is off for chats."""
    chat.same_origin(request)
    with mcp_client.lock:
        entry = entry_or_404(mcp_client.load(), name)
    if not mcp_client.approved(entry):
        raise HTTPException(409, "Approve the command first.")
    mcp_client.manager().test(entry)
    return view(entry)


@router.post("/servers/{name}/tools")
def approve_tools(name: str, body: ToolsIn, request: Request) -> dict[str, Any]:
    """Approve tools at the definition hash the page showed (the server's current list), or revoke them."""
    chat.same_origin(request)
    live = mcp_client.manager().live(name)
    with mcp_client.lock:
        data = mcp_client.load()
        entry = entry_or_404(data, name)
        tools = dict(entry.get("tools") or {})
        for tool, digest in body.approve.items():
            if live is None or tool not in live:
                raise HTTPException(
                    409, f"The server does not list {tool} now. Test the connection."
                )
            if mcp_client.definition_hash(live[tool]) != digest:
                raise HTTPException(
                    409,
                    f"The definition of {tool} changed after the page showed it. Read it again.",
                )
            tools[tool] = {"hash": digest, "definition": live[tool]}
        for tool in body.revoke:
            tools.pop(tool, None)
        entry["tools"] = tools
        mcp_client.save(data)
    return view(entry)
