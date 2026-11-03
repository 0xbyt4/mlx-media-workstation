"""The Workstation tools: list the media tools, read a job's status, search the Library (R0) and queue a job (R2).

They reach the server only through a WorkstationPort (app/agent/ports.py). create_job returns the job id at once and
never waits for the job.
"""

from typing import Any

from app.agent.policy import Risk
from app.agent.ports import WorkstationPort
from app.agent.registry import Context, Tool, ToolError

UPLOADS = ("file", "files")
MAX_VALUE = 20000  # characters of one job field, as a chat message


def _field_line(f: dict[str, Any]) -> str:
    bits = [f["type"]]
    if f["type"] == "select":
        bits = ["one of " + " | ".join(str(o["value"]) for o in f.get("options", []))]
    elif f["type"] == "number" and ("min" in f or "max" in f):
        bits.append(f"{f.get('min', '')}..{f.get('max', '')}")
    if f.get("required"):
        bits.append("required")
    if f.get("default") not in (None, ""):
        bits.append(f"default {f['default']}")
    return f"{f['name']} ({', '.join(bits)})"


def format_tools(items: list[dict[str, Any]]) -> str:
    lines = []
    for t in items:
        state = "installed" if t.get("installed", True) else "NOT installed"
        lines.append(f"{t['id']}: {t['label']} ({t.get('group', '')}), {state}.")
        if t.get("description"):
            lines.append(f"  {t['description']}")
        fields = [f for f in t.get("fields", []) if f["type"] not in UPLOADS]
        if fields:
            lines.append("  fields: " + "; ".join(_field_line(f) for f in fields))
        if any(f["type"] in UPLOADS and f.get("required") for f in t.get("fields", [])):
            lines.append(
                "  needs an uploaded file: the user must start it on its page."
            )
    return "\n".join(lines) or "No media tools."


def format_job(job: dict[str, Any]) -> str:
    lines = [f"Job {job.get('id')}: {job.get('tool_label') or job.get('tool')}"]
    lines.append(f"status: {job.get('status')}")
    if job.get("position"):
        lines.append(f"place in the queue: {job['position']}")
    if job.get("waiting"):
        lines.append(job["waiting"])
    progress = job.get("progress")
    if isinstance(progress, dict):
        if progress.get("stage"):
            lines.append(f"stage: {progress['stage']}")
        if progress.get("fraction") is not None:
            lines.append(f"progress: {round(100 * float(progress['fraction']))}%")
        if progress.get("eta_s") is not None:
            lines.append(f"about {round(float(progress['eta_s']))} s left")
    if job.get("elapsed") is not None:
        lines.append(f"elapsed: {job['elapsed']} s")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    for o in job.get("outputs") or []:
        lines.append(f"output: {o.get('name')} ({o.get('kind')})")
    return "\n".join(lines)


def _as_form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def build(port: WorkstationPort) -> list[Tool]:
    def list_tools(args: dict[str, Any], ctx: Context) -> str:
        return format_tools(port.list_tools())

    def job_status(args: dict[str, Any], ctx: Context) -> str:
        job = port.job_status(args["job_id"])
        if job is None:
            raise ToolError(f"No job with the id {args['job_id']}.")
        return format_job(job)

    def search_library(args: dict[str, Any], ctx: Context) -> str:
        found = port.search_library(args["query"], args["limit"])
        if not found:
            return f"No outputs match {args['query']!r}."
        lines = []
        for i in found[: args["limit"]]:
            text = " ".join(str(i.get("text") or "").split())
            text = text if len(text) <= 200 else text[:200] + "..."
            lines.append(
                f"- {i.get('created')} {i.get('tool_label') or i.get('tool')}: "
                f"{i.get('name')} ({i.get('kind')}), job {i.get('job')}"
                + (f"\n  text: {text}" if text else "")
            )
        return "\n".join(lines)

    def create_job(args: dict[str, Any], ctx: Context) -> str:
        tools = {t["id"]: t for t in port.list_tools()}
        tool = tools.get(args["tool"])
        if tool is None:
            raise ToolError(
                f"Unknown tool {args['tool']!r}. Tools: {', '.join(tools)}."
            )
        if not tool.get("installed", True):
            raise ToolError(f"{tool['label']} is not installed on this Mac.")
        fields = {f["name"]: f for f in tool["fields"]}
        if any(f["type"] in UPLOADS and f.get("required") for f in fields.values()):
            raise ToolError(
                f"{tool['label']} needs an uploaded file; the user must start it on its page."
            )
        values: dict[str, str] = {}
        for name, value in args["fields"].items():
            f = fields.get(name)
            if f is None or f["type"] in UPLOADS:
                known = ", ".join(
                    n for n, f in fields.items() if f["type"] not in UPLOADS
                )
                raise ToolError(f"{tool['id']} has no field {name!r}. Fields: {known}.")
            values[name] = _as_form_value(value)
            if len(values[name]) > MAX_VALUE:
                raise ToolError(f"{name}: at most {MAX_VALUE} characters.")
        try:
            job_id = port.create_job(tool["id"], values)
        except ValueError as e:
            raise ToolError(f"The job was refused: {e}") from None
        return (
            f"Queued job {job_id} ({tool['label']}). It runs in the background after this answer; "
            "the user sees it in the job list. Do not wait for it; use job_status to check it later."
        )

    def explain_job(args: dict[str, Any]) -> str:
        shown = ", ".join(
            f"{k}={str(v)[:80]!r}" for k, v in (args.get("fields") or {}).items()
        )
        return f"Queues a {args.get('tool')} job ({shown or 'default values'}). It uses this Mac for minutes."

    return [
        Tool(
            name="list_workstation_tools",
            description=(
                "List the app's media tools (image, voice, music, video): their ids, what they do, whether they "
                "are installed, and the fields create_job accepts."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=list_tools,
        ),
        Tool(
            name="job_status",
            description="Read the status, progress, error and output files of one media job by its id.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 1, "maxLength": 64}
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=job_status,
        ),
        Tool(
            name="search_library",
            description=(
                "Search the user's finished outputs (images, audio, videos) by the words of their prompt, the "
                "tool or the file name. Newest first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=search_library,
            untrusted_output=True,  # prompts can hold text that came from a web page
        ),
        Tool(
            name="create_job",
            description=(
                "Queue a media job with one of the app's tools (see list_workstation_tools for ids and fields). "
                "The user approves it first. It returns the job id at once; the job runs after your answer."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "minLength": 1, "maxLength": 64},
                    "fields": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["string", "number", "boolean"]
                        },
                        "default": {},
                    },
                },
                "required": ["tool"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=create_job,
            explain=explain_job,
        ),
    ]
