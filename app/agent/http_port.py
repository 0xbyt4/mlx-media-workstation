"""A WorkstationPort over HTTP, for the MCP server (app/mcp_server.py): the running app's job queue, seen from another
process on this Mac.

It talks only to the app on 127.0.0.1, as the app's own page does: the session cookie holds the token from
outputs/.server-token (read by app/session_token.py: a regular file of this user that only this user can read), and
every request sends the app's Origin. The app writes a new token at each start, so the token is read again for each
request (and once more after an HTTP 401): a restarted app works without a new client setup. No proxy and no redirect
is followed, so the token goes only to 127.0.0.1. Nothing here prints or logs the token.

The app keeps its own rules: create_job sends the form that POST /api/jobs takes, with client=mcp (the app's agent
checks; the job's "chat" is MCP), and the queue runs it when its memory is free. No chat turn holds a memory lease for
this client, so a job that needs the chat model's memory unloads the model by the queue's rules (llm.claim) and runs:
wait_job waits for it and never answers "needs_memory".
"""

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from app import session_token
from app.agent.ports import InputFile
from app.agent.registry import ToolError
from app.config import ROOT

PORT = 7870  # the app's port (start.py)
TOKEN_NAME = ".server-token"  # in the outputs folder (app/server.py)
TIMEOUT = 30.0  # seconds of one request
POLL = 1.0  # seconds between two looks at a job that wait_job waits for
CHUNK = 1024 * 1024
ENDED = ("done", "failed", "cancelled")
START_HELP = "Start Workstation (Workstation.app, or `.venv/bin/python start.py --browser` in its folder), then try again."


class AppUnavailable(ToolError):
    """The app does not run on the port, or does not take this checkout's token."""


class AppError(ToolError):
    """The app answered with an error that the port does not turn into its own answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"The app answered HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the redirect comes back as an HTTP error


def safe_name(name: str) -> str:
    """The file name for the form: its base name with only letters, digits, ".", "-" and "_" (the app keeps only a
    media suffix of it, and names it in a refusal)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))[-100:] or "input"


class HttpWorkstation:
    """The app on 127.0.0.1:`port`; `outputs` is its outputs folder (the token file and the jobs' files)."""

    def __init__(self, port: int = PORT, outputs: Path = ROOT / "outputs") -> None:
        self.port = port
        self.outputs = outputs
        self.base = f"http://127.0.0.1:{port}"
        # no proxy from the environment, no redirects: the request goes to 127.0.0.1 only
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def file_url(self, job_id: str, name: str) -> str:
        """The app's address of an output (it needs the session, as in a signed-in browser)."""
        q = urllib.parse.quote
        return f"{self.base}/files/{q(job_id, safe='')}/{q(name, safe='')}"

    def _token(self) -> str:
        token = session_token.read(self.outputs / TOKEN_NAME)
        if token is None:
            raise AppUnavailable(f"Workstation is not running. {START_HELP}")
        return token

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Callable[[], Iterator[bytes]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """The JSON answer of one request. HTTP errors other than 401 raise AppError; no answer, or a token the app
        does not take, raises AppUnavailable."""
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        sent: str | None = None
        while True:
            token = self._token()
            if (
                token == sent
            ):  # HTTP 401 with this token, and the file holds no newer one
                raise AppUnavailable(
                    f"The app on {self.base} did not take the token in outputs/{TOKEN_NAME}: another program may "
                    f"use the port, or the app is still starting. {START_HELP}"
                )
            sent = token
            req = urllib.request.Request(
                url,
                data=body() if body is not None else None,
                method=method,
                headers={
                    **(headers or {}),
                    "Cookie": f"workstation_{self.port}={token}",
                    "Origin": self.base,
                    "Accept": "application/json",
                },
            )
            try:
                with self.opener.open(req, timeout=TIMEOUT) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    continue  # the app may have started again with a new token: read it once more
                raise AppError(e.code, self._detail(e)) from None
            except (OSError, ValueError):  # refused, reset, timed out, or not JSON
                raise AppUnavailable(
                    f"Workstation does not answer on {self.base}. {START_HELP}"
                ) from None

    @staticmethod
    def _detail(e: urllib.error.HTTPError) -> str:
        try:
            detail = json.loads(e.read()).get("detail")
        except (OSError, ValueError, AttributeError):
            detail = None
        if isinstance(detail, list):  # a FastAPI validation error
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        return str(detail or e.reason)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/tools", {"installed": "true"})

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        try:
            return self.request(
                "GET", "/api/jobs/" + urllib.parse.quote(job_id, safe="")
            )
        except AppError as e:
            if e.status == 404:
                return None
            raise

    def list_jobs(self, limit: int) -> list[dict[str, Any]]:
        return self.request("GET", "/api/jobs", {"limit": limit})

    def wait_job(
        self, job_id: str, seconds: float, abort: threading.Event
    ) -> dict[str, Any] | None:
        end = time.monotonic() + seconds
        while True:
            job = self.job_status(job_id)
            left = end - time.monotonic()
            if job is None or job.get("status") in ENDED or left <= 0:
                return job
            if abort.wait(min(POLL, left)):
                return self.job_status(job_id)

    def cancel_job(self, job_id: str) -> str:
        path = "/api/jobs/" + urllib.parse.quote(job_id, safe="") + "/cancel"
        try:
            return self.request("POST", path)["status"]
        except AppError as e:
            if e.status == 404:
                raise ValueError(e.detail) from None
            raise

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        return self.request("GET", "/api/library/search", {"q": query, "limit": limit})

    def output_file(self, job_id: str, name: str) -> Path | None:
        """A listed output of a done job, in this checkout's outputs folder: a regular file, not a link, directly in
        the job's folder."""
        job = self.job_status(job_id)
        if job is None or job.get("status") != "done" or job.get("id") != job_id:
            return None
        if name not in [o.get("name") for o in job.get("outputs") or []]:
            return None
        folder = self.outputs / job_id
        path = folder / name
        if path.is_symlink() or not path.is_file():
            return None
        if os.path.realpath(path.parent) != os.path.realpath(folder):
            return None
        return path

    def create_job(
        self,
        tool: str,
        fields: dict[str, str],
        inputs: dict[str, list[InputFile]] | None = None,
        chat: str | None = None,
    ) -> str:
        """POST /api/jobs as a form, with the input files copied to a private temporary folder first (InputFile
        writes to a path) and sent from there. `chat` goes as the form's client (the app takes only MCP)."""
        values = [("tool", tool), *fields.items()]
        if chat is not None:
            values.append(("client", chat))
        with tempfile.TemporaryDirectory(prefix="workstation-mcp-") as tmp:
            files: list[tuple[str, str, Path]] = []
            for name, given in (inputs or {}).items():
                for inp in given:
                    path = Path(tmp) / str(len(files))
                    inp.write(path)
                    files.append((name, safe_name(inp.name), path))
            boundary = uuid.uuid4().hex
            body, length = multipart(boundary, values, files)
            try:
                answer = self.request(
                    "POST",
                    "/api/jobs",
                    body=body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Content-Length": str(length),
                    },
                )
            except AppError as e:
                if e.status in (400, 413, 503):
                    raise ValueError(e.detail) from None
                raise
        return str(answer["id"])

    def system_status(self) -> dict[str, Any]:
        return self.request("GET", "/api/status")


def multipart(
    boundary: str, values: list[tuple[str, str]], files: list[tuple[str, str, Path]]
) -> tuple[Callable[[], Iterator[bytes]], int]:
    """A multipart/form-data body as a function that gives its parts (the files are read in chunks, not held in
    memory), and its length in bytes."""

    def head(name: str, filename: str | None = None) -> bytes:
        quoted = urllib.parse.quote(name, safe="")
        line = f'Content-Disposition: form-data; name="{quoted}"'
        if filename is not None:
            line += f'; filename="{filename}"\r\nContent-Type: application/octet-stream'
        return f"--{boundary}\r\n{line}\r\n\r\n".encode()

    parts: list[bytes | Path] = []
    for name, value in values:
        parts += [head(name) + value.encode() + b"\r\n"]
    for name, filename, path in files:
        parts += [head(name, filename), path, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    length = sum(p.stat().st_size if isinstance(p, Path) else len(p) for p in parts)

    def body() -> Iterator[bytes]:
        for p in parts:
            if isinstance(p, bytes):
                yield p
                continue
            with p.open("rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk

    return body, length
