"""The Workstation tools: list the media tools, read and list jobs, wait for a job, search the Library (R0); queue a
job, cancel a job and transcribe speech (R2).

They reach the server only through a WorkstationPort (app/agent/ports.py). create_job returns the job id at once.
wait_for_job and transcribe_audio wait for a job that can run next to the chat model. A wait ends at its time, when the
job ends, or at Stop, and leaves the answer time for its next steps. A job that needs the chat model's memory starts
only after the answer (it waits for the turn's memory lease), so the port does not wait for it.

A file field of a job takes file references (REF_HELP): a Library output, a file in the chat's work folder, or a file
in the granted folder. The rules of the file tools apply (no hidden or secret files, real-path checks, O_NOFOLLOW).
The file is opened here and the port copies it into the job folder as an upload is copied, with the upload's checks.
"""

import os
import time
from pathlib import Path
from typing import Any

from app.agent import file_tools
from app.agent.policy import Risk
from app.agent.ports import InputFile, WorkstationPort
from app.agent.registry import Context, Output, Tool, ToolError

UPLOADS = ("file", "files")
MAX_VALUE = 20000  # characters of one job field, as a chat message
MAX_INPUTS = 10  # file references of one field (Klein Edit takes 10 references)
MAX_WAIT = 300  # seconds of one wait
# seconds an answer keeps after a wait, for its next model steps (the answer has 10 minutes, app/chat.py)
ANSWER_RESERVE = 60
COPY_CHUNK = 1024 * 1024
MAX_TRANSCRIPT = 1024 * 1024  # bytes of a transcript read
TRANSCRIBE = "voice.transcribe"  # the media tool behind transcribe_audio
ENDED = ("done", "failed", "cancelled")
SOURCES = ("library", "work", "folder")
REF_HELP = (
    '"library:<job id>/<file name>" (an output of a finished job, as search_library and job_status show it), '
    '"work:<path>" (a file in this chat\'s work folder, where run_python writes) or "folder:<path>" (a file in the '
    "folder the user granted)"
)


def _accepts(f: dict[str, Any]) -> str:
    kinds = [a.strip().split("/")[0] for a in f.get("accept", "").split(",")]
    return " or ".join(k for k in kinds if k) or "media"


def _field_line(f: dict[str, Any]) -> str:
    bits = [f["type"]]
    if f["type"] == "select":
        bits = ["one of " + " | ".join(str(o["value"]) for o in f.get("options", []))]
    elif f["type"] == "number" and ("min" in f or "max" in f):
        bits.append(f"{f.get('min', '')}..{f.get('max', '')}")
    elif f["type"] in UPLOADS:
        bits.append(_accepts(f))
        if f["type"] == "files":
            bits.append(f"up to {f.get('max', MAX_INPUTS)}")
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
        if t.get("fields"):
            lines.append("  fields: " + "; ".join(_field_line(f) for f in t["fields"]))
    if any(f["type"] in UPLOADS for t in items for f in t.get("fields", [])):
        lines.append(
            f"A file field takes a file reference, a files field a list of them: {REF_HELP}."
        )
    return "\n".join(lines) or "No media tools."


def format_job(job: dict[str, Any], chat_id: str | None = None) -> str:
    lines = [f"Job {job.get('id')}: {job.get('tool_label') or job.get('tool')}"]
    if chat_id is not None and job.get("chat") == chat_id:
        lines.append("queued by this chat")
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
        lines.append(
            f"output: {o.get('name')} ({o.get('kind')}), file reference "
            f"library:{job.get('id')}/{o.get('name')}"
        )
    return "\n".join(lines)


def job_line(job: dict[str, Any], chat_id: str) -> str:
    """One short line of list_jobs."""
    bits = [str(job.get("status"))]
    if job.get("position"):
        bits.append(f"place {job['position']} in the queue")
    progress = job.get("progress") or {}
    if progress.get("fraction") is not None:
        bits.append(f"{round(100 * float(progress['fraction']))}%")
    if progress.get("eta_s") is not None:
        bits.append(f"about {round(float(progress['eta_s']))} s left")
    if job.get("outputs"):
        bits.append("outputs: " + ", ".join(str(o.get("name")) for o in job["outputs"]))
    if job.get("error"):
        error = " ".join(str(job["error"]).split())
        bits.append("error: " + (error if len(error) <= 120 else error[:120] + "..."))
    mine = ", queued by this chat" if job.get("chat") == chat_id else ""
    label = job.get("tool_label") or job.get("tool")
    return f"- {job.get('id')} {label} ({job.get('created')}{mine}): {'; '.join(bits)}"


def _as_form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _work_root(ctx: Context) -> str:
    """The real path of the chat's work folder. A work folder that is a link is refused, as the sandbox does."""
    try:
        fd = os.open(ctx.work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ToolError(
            "This chat's work folder is empty: run_python wrote no file yet."
        ) from None
    except OSError:
        raise ToolError("This chat's work folder is missing or is a link.") from None
    try:
        return file_tools.fd_path(fd)
    finally:
        os.close(fd)


def _copier(fd: int, size: int):
    """Writes the first `size` bytes of the open file (the size that was checked) to a path."""

    def write(path: Path) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        left = size
        with path.open("wb") as out:
            while left > 0:
                chunk = os.read(fd, min(COPY_CHUNK, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)

    return write


def _refs(name: str, value: Any) -> list[str]:
    refs = [value] if isinstance(value, str) else value
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        raise ToolError(f"{name}: give a file reference: {REF_HELP}.")
    return refs


def build(port: WorkstationPort) -> list[Tool]:
    def open_ref(ref: str, ctx: Context) -> tuple[int, InputFile]:
        """An open descriptor of the referenced file and its InputFile. The caller closes the descriptor."""
        source, sep, rest = ref.partition(":")
        rest = rest.strip()
        if not sep or source not in SOURCES or not rest:
            raise ToolError(f"{ref!r}: not a file reference. Give {REF_HELP}.")
        if source == "library":
            job_id, _, name = rest.partition("/")
            path = port.output_file(job_id, name) if name else None
            if path is None:
                raise ToolError(
                    f"{ref}: not an output of a finished job. Find outputs with search_library or job_status."
                )
            root = os.path.realpath(path.parent)
            real = os.path.join(root, path.name)
            fd, st = file_tools.open_file(real, root)
            return fd, InputFile(path.name, st.st_size, _copier(fd, st.st_size))
        try:
            root = _work_root(ctx) if source == "work" else file_tools._root(ctx)
            real = file_tools.resolve(root, rest)
            if os.path.isdir(real):
                raise ToolError(f"{rest}: a folder, not a file.")
            fd, st = file_tools.open_file(real, root)
        except ToolError as e:
            if source == "folder":
                raise
            raise ToolError(
                str(e).replace("the granted folder", "the work folder")
            ) from None
        try:
            name = os.path.basename(file_tools.fd_path(fd))
        except OSError:
            os.close(fd)
            raise ToolError(f"{rest}: cannot read the file.") from None
        return fd, InputFile(name, st.st_size, _copier(fd, st.st_size))

    def queue(
        tool_id: str, values: dict[str, str], refs: dict[str, list[str]], ctx: Context
    ) -> str:
        """Open the referenced files, queue the job (the port copies them into its folder) and close them."""
        fds: list[int] = []
        try:
            inputs: dict[str, list[InputFile]] = {}
            for name, given in refs.items():
                inputs[name] = []
                for ref in given:
                    fd, inp = open_ref(ref, ctx)
                    fds.append(fd)
                    inputs[name].append(inp)
            try:
                return port.create_job(tool_id, values, inputs, ctx.chat_id)
            except ValueError as e:
                raise ToolError(f"The job was refused: {e}") from None
        finally:
            for fd in fds:
                os.close(fd)

    def wait(job_id: str, seconds: float, ctx: Context) -> tuple[dict[str, Any], float]:
        """Wait for the job at most `seconds`, and never into the answer's last ANSWER_RESERVE seconds. Returns the
        job and the seconds it could wait (0 = no time was left)."""
        budget = float(seconds)
        if ctx.deadline is not None:
            budget = min(budget, ctx.deadline - time.monotonic() - ANSWER_RESERVE)
        if budget <= 0:
            job = port.job_status(job_id)
        else:
            job = port.wait_job(job_id, budget, ctx.abort)
            ctx.check_abort()
        if job is None:
            raise ToolError(f"No job with the id {job_id}.")
        return job, max(0.0, budget)

    def still(job: dict[str, Any], waited: float) -> str:
        if job.get("needs_memory"):
            return (
                "[This job, or a job before it in the queue, needs the memory that the chat model uses, so it starts "
                "after this answer ends. Do not wait for it in this answer; tell the user that it runs then.]"
            )
        if waited <= 0:
            return "[This answer has no time left to wait. Tell the user that the job goes on in the background.]"
        return (
            f"[Still {job.get('status')} after {round(waited)} s. It goes on in the background; tell the user, or "
            "wait again only when they need the result in this answer.]"
        )

    def list_tools(args: dict[str, Any], ctx: Context) -> str:
        return format_tools(port.list_tools())

    def job_status(args: dict[str, Any], ctx: Context) -> str:
        job = port.job_status(args["job_id"])
        if job is None:
            raise ToolError(f"No job with the id {args['job_id']}.")
        return format_job(job, ctx.chat_id)

    def list_jobs(args: dict[str, Any], ctx: Context) -> str:
        found = port.list_jobs(args["limit"])
        if not found:
            return "No jobs."
        return "\n".join(job_line(j, ctx.chat_id) for j in found)

    def wait_for_job(args: dict[str, Any], ctx: Context) -> str:
        job = port.job_status(args["job_id"])
        if job is None:
            raise ToolError(f"No job with the id {args['job_id']}.")
        if job.get("status") in ENDED:
            return format_job(job, ctx.chat_id)
        job, waited = wait(args["job_id"], args["seconds"], ctx)
        text = format_job(job, ctx.chat_id)
        return text if job.get("status") in ENDED else f"{text}\n{still(job, waited)}"

    def cancel_job(args: dict[str, Any], ctx: Context) -> str:
        job = port.job_status(args["job_id"])
        if job is None:
            raise ToolError(f"No job with the id {args['job_id']}.")
        label = job.get("tool_label") or job.get("tool")
        if job.get("chat") != ctx.chat_id:
            raise ToolError(
                f"Job {job['id']} ({label}) was not queued by this chat, so you cannot cancel it. The user can "
                "cancel it in the Jobs view."
            )
        if job.get("status") not in ("queued", "running"):
            return f"Job {job['id']} ({label}) is already {job.get('status')}."
        try:
            status = port.cancel_job(job["id"])
        except ValueError as e:
            raise ToolError(str(e)) from None
        return f"Job {job['id']} ({label}) is {status}."

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
                f"{i.get('name')} ({i.get('kind')}), job {i.get('job')}, file reference "
                f"library:{i.get('job')}/{i.get('name')}"
                + (f"\n  text: {text}" if text else "")
            )
        return "\n".join(lines)

    def create_job(args: dict[str, Any], ctx: Context) -> Output:
        tools = {t["id"]: t for t in port.list_tools()}
        tool = tools.get(args["tool"])
        if tool is None:
            raise ToolError(
                f"Unknown tool {args['tool']!r}. Tools: {', '.join(tools)}."
            )
        if not tool.get("installed", True):
            raise ToolError(f"{tool['label']} is not installed on this Mac.")
        fields = {f["name"]: f for f in tool["fields"]}
        values: dict[str, str] = {}
        refs: dict[str, list[str]] = {}
        for name, value in args["fields"].items():
            f = fields.get(name)
            if f is None:
                raise ToolError(
                    f"{tool['id']} has no field {name!r}. Fields: {', '.join(fields)}."
                )
            if f["type"] in UPLOADS:
                refs[name] = _refs(name, value)
                continue
            if isinstance(value, list):
                raise ToolError(f"{name}: give one value, not a list.")
            values[name] = _as_form_value(value)
            if len(values[name]) > MAX_VALUE:
                raise ToolError(f"{name}: at most {MAX_VALUE} characters.")
        for name, f in fields.items():
            if f["type"] in UPLOADS and f.get("required") and not refs.get(name):
                raise ToolError(
                    f"{tool['label']} needs a file in its field {name!r} ({_accepts(f)}): {REF_HELP}."
                )
        job_id = queue(tool["id"], values, refs, ctx)
        given = "; ".join(f"{n} = {', '.join(r)}" for n, r in refs.items() if r)
        return Output(
            f"Queued job {job_id} ({tool['label']})"
            + (f" with the input files {given}" if given else "")
            + ". It runs in the background; the user sees it in the job list and in this chat. Do not wait for it "
            "unless the user needs its result in this answer: then call wait_for_job. Use job_status to check it "
            "later.",
            job=job_id,
        )

    def transcribe(args: dict[str, Any], ctx: Context) -> Output:
        tool = next((t for t in port.list_tools() if t["id"] == TRANSCRIBE), None)
        if tool is None or not tool.get("installed", True):
            raise ToolError(
                "Speech recognition is not installed on this Mac. It needs the Qwen3-ASR 0.6B model and the Fish "
                "Drama 3 environment: the user can install them in Models."
            )
        if bool(args.get("audio")) == bool(args.get("job_id")):
            raise ToolError("Give either audio (a file reference) or job_id.")
        if args.get("audio"):
            field = next(f for f in tool["fields"] if f["name"] == "language")
            options = {
                str(o["value"]).lower(): str(o["value"]) for o in field["options"]
            }
            language = options.get(args["language"].strip().lower())
            if language is None:
                raise ToolError(f"language: one of {', '.join(options.values())}.")
            job_id = queue(
                TRANSCRIBE, {"language": language}, {"audio": [args["audio"]]}, ctx
            )
        else:
            job_id = args["job_id"]
            job = port.job_status(job_id)
            if job is None or job.get("tool") != TRANSCRIBE:
                raise ToolError(f"Job {job_id} is not a transcription job.")
        job, waited = wait(job_id, args["seconds"], ctx)
        status = job.get("status")
        if status in ("failed", "cancelled"):
            raise ToolError(
                f"The transcription job {job_id} {status}"
                + (f": {job['error']}" if job.get("error") else ".")
            )
        if status != "done":
            return Output(
                f"Transcription job {job_id} is {status}.\n{still(job, waited)}\nTo read the transcript later, call "
                f"transcribe_audio with job_id={job_id}.",
                job=job_id,
            )
        path = port.output_file(job_id, "transcript.txt")
        if path is None:
            raise ToolError(f"The transcription job {job_id} has no transcript.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            data = os.read(fd, MAX_TRANSCRIPT)
        finally:
            os.close(fd)
        return Output(
            f"Transcript of job {job_id} (times are minutes:seconds):\n"
            + data.decode("utf-8", errors="replace"),
            job=job_id,
        )

    def explain_job(args: dict[str, Any]) -> str:
        tool = next((t for t in port.list_tools() if t["id"] == args.get("tool")), None)
        uploads = {
            f["name"] for f in (tool or {}).get("fields", []) if f["type"] in UPLOADS
        }
        values = args.get("fields") or {}
        shown = ", ".join(
            f"{k}={str(v)[:80]!r}" for k, v in values.items() if k not in uploads
        )
        files = "; ".join(
            f"{k} = {', '.join(v) if isinstance(v, list) else v}"
            for k, v in values.items()
            if k in uploads
        )
        return (
            f"Queues a job for {args.get('tool')} ({shown or 'default values'})."
            + (f" Input files: {files}." if files else "")
            + " It uses this Mac for minutes."
        )

    def explain_transcribe(args: dict[str, Any]) -> str:
        if args.get("job_id"):
            return f"Reads the transcript of job {args['job_id']} and waits for it if it still runs."
        return (
            f"Queues a speech recognition job for the file {args.get('audio')!r} (language "
            f"{args.get('language') or 'auto'}) and waits for its text. It uses this Mac for a while."
        )

    job_id_arg = {"type": "string", "minLength": 1, "maxLength": 64}
    seconds_arg = {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_WAIT,
        "default": 120,
    }
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
            description=(
                "Read the status, progress, error and output files of one media job by its id. An output's file "
                "reference can be an input of the next job."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": job_id_arg},
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
                "A file field takes a file reference, a files field a list of them: "
                f"{REF_HELP}. The user approves it first. It returns the job id at once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "minLength": 1, "maxLength": 64},
                    "fields": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["string", "number", "boolean", "array"],
                            "items": {"type": "string", "maxLength": 1100},
                            "maxItems": MAX_INPUTS,
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
        Tool(
            name="list_jobs",
            description="List the queued and running media jobs and the newest finished ones, one short line each.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 10,
                    }
                },
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=list_jobs,
        ),
        Tool(
            name="wait_for_job",
            description=(
                f"Wait until a media job ends (at most `seconds`, up to {MAX_WAIT}), then show it as job_status does. "
                "Use it only when the user needs the job's result in this answer. It returns early when the user "
                "stops the answer."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": job_id_arg, "seconds": seconds_arg},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=wait_for_job,
        ),
        Tool(
            name="cancel_job",
            description="Cancel a queued or running media job that this chat queued.",
            parameters={
                "type": "object",
                "properties": {"job_id": job_id_arg},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=cancel_job,
            explain=lambda a: (
                f"Cancels the media job {a.get('job_id')}. The work it did is lost."
            ),
        ),
        Tool(
            name="transcribe_audio",
            description=(
                "Turn speech into text (Qwen3-ASR on this Mac): the words of an audio or video file with a time "
                f"for about every 30 s, and the language. audio is a file reference: {REF_HELP}. It queues a job "
                "and waits for it (at most `seconds`). When the job is still running, call it again later with "
                "job_id to read the text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "audio": {"type": "string", "maxLength": 1100},
                    "language": {"type": "string", "maxLength": 40, "default": "auto"},
                    "job_id": job_id_arg,
                    "seconds": seconds_arg,
                },
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=transcribe,
            untrusted_output=True,  # the transcript is text from outside the app
            explain=explain_transcribe,
        ),
    ]
