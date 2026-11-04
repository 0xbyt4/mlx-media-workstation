"""The functions the agent needs from the Workstation server, as a protocol, so this package never imports app.server.

The server provides one object with these methods when it wires agent mode in. create_job must use the same checks as
POST /api/jobs (a shared function, not an HTTP call) and must return at once: the job waits for the agent turn's
memory lease, so waiting for it inside the turn would deadlock (docs/AGENT.md). wait_job therefore returns at once for
a job that needs that memory.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MCP = "mcp"  # the "chat" of a job queued by an MCP client (app/mcp_server.py); a chat id is a time stamp, never this


@dataclass(frozen=True)
class InputFile:
    """One input file of a job, from an upload or an agent tool: the name it came with (only its suffix is used), its
    size in bytes, and a function that writes its bytes to a path in the job folder."""

    name: str
    size: int
    write: Callable[[Path], None]


class WorkstationPort(Protocol):
    def list_tools(self) -> list[dict[str, Any]]:
        """The media tools as in app/tools.TOOLS (id, label, group, description, fields), each with "installed":
        whether its model files are on disk."""
        ...

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        """The job as GET /api/jobs shows it (status, progress, outputs, error...) plus "chat" (the agent chat that
        queued it, or None), or None when there is no such job."""
        ...

    def list_jobs(self, limit: int) -> list[dict[str, Any]]:
        """Every queued and running job, then the newest finished ones up to `limit` jobs in all, newest first, in
        the shape of job_status."""
        ...

    def wait_job(
        self, job_id: str, seconds: float, abort: threading.Event
    ) -> dict[str, Any] | None:
        """Wait until the job is done, failed or cancelled, `seconds` pass or abort is set; then return
        job_status(job_id). A queued job that needs the memory the chat model holds (it, or a job before it, does not
        fit next to the model) starts only after the agent's turn: then it returns at once, with "needs_memory": True."""
        ...

    def cancel_job(self, job_id: str) -> str:
        """Cancel a queued or running job as POST /api/jobs/{id}/cancel does; returns its status after that. Raises
        ValueError when there is no such job."""
        ...

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Finished outputs whose text, tool or file name contains the query, newest first: dicts with job, tool,
        tool_label, kind, name, created and text."""
        ...

    def output_file(self, job_id: str, name: str) -> Path | None:
        """The path of one listed output of a finished job (as the Library shows it), or None: no such job or
        output, a job that still runs, an input file, or a link."""
        ...

    def create_job(
        self,
        tool: str,
        fields: dict[str, str],
        inputs: dict[str, list[InputFile]] | None = None,
        chat: str | None = None,
    ) -> str:
        """Queue a job with these form values (strings, as the form sends them) and input files (by file field), and
        return its id. The files are copied into the job folder as an upload is; `chat` is stored with the job.
        Raises ValueError with the reason when the tool, a value or a file is refused."""
        ...

    def system_status(self) -> dict[str, Any]:
        """The Mac's memory, the running jobs, the queue, the chat model and whether each media tool fits now, as
        GET /api/status shows them."""
        ...
