"""run_python (R3): run Python code in the sandbox (app/agent/sandbox.py). The user approves every call."""

from typing import Any

from app.agent.policy import Risk
from app.agent.registry import Cancelled, Context, Output, Tool, ToolError
from app.agent.sandbox import Limits, Sandbox

MAX_CODE = 100_000  # characters of code in one call
REASONS = {
    "timeout": "stopped: it ran longer than {wall} s",
    "cpu": "stopped: it used more than {cpu} s of CPU time",
    "memory": "stopped: it used more than {mem} MB of memory",
    "disk": "stopped: the work folder grew past {disk} MB or too many files",
}


def format_result(r: Any, limits: Limits) -> str:
    lines = [f"exit code: {r.exit_code} ({r.seconds} s)"]
    if r.killed in REASONS:
        lines.append(
            REASONS[r.killed].format(
                wall=limits.wall_s,
                cpu=limits.cpu_s,
                mem=limits.memory_mb,
                disk=limits.disk_mb,
            )
        )
    for name, text, cut in (
        ("stdout", r.stdout, r.stdout_truncated),
        ("stderr", r.stderr, r.stderr_truncated),
    ):
        if text or cut:
            lines.append(f"{name}:\n{text.rstrip()}")
            if cut:
                lines.append(f"[{name} cut at {limits.output_chars:,} characters]")
    if r.files:
        shown = r.files[:50]
        more = f" and {len(r.files) - 50} more" if len(r.files) > 50 else ""
        lines.append("files written: " + ", ".join(shown) + more)
    return "\n".join(lines)


def build(sandbox: Sandbox, limits: Limits, packages: str) -> Tool:
    def run_python(args: dict[str, Any], ctx: Context) -> Output:
        try:
            r = sandbox.run_python(args["code"], ctx.work_dir, limits, ctx.abort)
        except ValueError as e:
            raise ToolError(str(e)) from None
        if r.killed == "aborted":
            raise Cancelled
        return Output(format_result(r, limits), r.files)

    return Tool(
        name="run_python",
        description=(
            f"Run Python 3 code in a sandbox and return its printed output. {packages} No network, no "
            "subprocesses, no access to the user's files: the granted folder is not there, so copy a file into the "
            "work folder with copy_to_workspace, or read it with read_file and put the data you need into the code. "
            "The current folder is this chat's work folder: the "
            "code can read and write files there, and they stay for later calls in this chat. The user gets the "
            "files it writes as links in the chat (read_file cannot open them; export_from_workspace saves one in "
            "the granted folder). Print the results "
            f"you need. Limits: {limits.wall_s:.0f} s, {limits.memory_mb} MB of memory, "
            f"{limits.file_mb} MB per file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": MAX_CODE}
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        risk=Risk.R3,
        run=run_python,
        untrusted_output=True,  # it can print files and text that earlier untrusted content made
        explain=lambda a: (
            "Runs this Python code in the sandbox: no network, no access to your files, "
            "writes only to this chat's work folder."
        ),
    )
