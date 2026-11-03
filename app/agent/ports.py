"""The functions the agent needs from the Workstation server, as a protocol, so this package never imports app.server.

The server provides one object with these methods when it wires agent mode in. create_job must use the same checks as
POST /api/jobs (a shared function, not an HTTP call) and must return at once: the job waits for the agent turn's
memory lease, so waiting for it inside the turn would deadlock (docs/AGENT.md).
"""

from typing import Any, Protocol


class WorkstationPort(Protocol):
    def list_tools(self) -> list[dict[str, Any]]:
        """The media tools as in app/tools.TOOLS (id, label, group, description, fields), each with "installed":
        whether its model files are on disk."""
        ...

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        """The job as GET /api/jobs shows it (status, progress, outputs, error...), or None when there is no such job."""
        ...

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Finished outputs whose text, tool or file name contains the query, newest first: dicts with job, tool,
        tool_label, kind, name, created and text."""
        ...

    def create_job(self, tool: str, fields: dict[str, str]) -> str:
        """Queue a job with these form values (strings, as the form sends them) and return its id. Raises ValueError
        with the reason when the tool or a value is refused."""
        ...
