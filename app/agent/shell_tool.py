"""run_shell (R3): run a shell command in the sandbox (app/agent/shell_sandbox.py). The user approves every call."""

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from app.agent.file_tools import _root
from app.agent.policy import Risk
from app.agent.python_tool import REASONS, time_limit
from app.agent.registry import Cancelled, Context, Output, Tool, ToolError
from app.agent.sandbox import Limits, RunResult
from app.agent.shell_sandbox import homebrew

MAX_COMMAND = 20_000  # characters of one command
DEFAULT_SECONDS = 120
MAX_SECONDS = 600
# Homebrew programs the description names when they are installed (the model does not guess what is there)
KNOWN = ("ffmpeg", "ffprobe", "python3", "magick", "sox", "rg", "git", "pandoc")


class ShellSandbox(Protocol):
    name: str

    def unavailable(self) -> str | None:
        """None when this sandbox can run commands here; else the reason."""
        ...

    def run_shell(
        self,
        command: str,
        workdir: Path,
        limits: Limits,
        abort: Any,
        read_only: Path | None = None,
    ) -> RunResult:
        """Run the command (cwd = workdir) and return when every process of the run is gone."""
        ...


def programs() -> str:
    """The programs a command can start, for the tool's description."""
    text = "Programs: the macOS ones in /bin and /usr/bin (such as ls, grep, sed, awk, jq, zip, tar, perl)"
    brew = homebrew()
    if brew:
        found = [n for n in KNOWN if os.path.exists(f"{brew}/bin/{n}")]
        text += f" and Homebrew's in {brew}/bin" + (
            f" ({', '.join(found)})" if found else ""
        )
    return (
        text
        + ". The Xcode tools in /usr/bin (git, python3, clang, make) do not work; Homebrew versions do."
    )


def format_result(r: RunResult, limits: Limits, note: str = "") -> str:
    """The exit code, then stdout and stderr, each headed by its length in characters, so the page can split them
    again even when the output itself holds such a heading. note: a line after the exit code (a cut time limit)."""
    lines = [f"exit code: {r.exit_code} ({r.seconds} s)"]
    if note:
        lines.append(note)
    if r.killed in REASONS:
        lines.append(
            REASONS[r.killed].format(
                wall=f"{limits.wall_s:g}",
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
            note = f", cut at {limits.output_chars:,}" if cut else ""
            lines.append(f"{name} ({len(text)} characters{note}):\n{text}")
    if not (r.stdout or r.stderr):
        lines.append("no output")
    if r.leftover:
        lines.append(
            "warning: some processes of this command could not be stopped yet; the app keeps stopping them, and "
            "run_shell refuses new commands until they are gone"
        )
    if r.files:
        shown = r.files[:50]
        more = f" and {len(r.files) - 50} more" if len(r.files) > 50 else ""
        lines.append("files written: " + ", ".join(shown) + more)
    return "\n".join(lines)


def build(shell: ShellSandbox, limits: Limits, programs: str) -> Tool:
    def run_shell(args: dict[str, Any], ctx: Context) -> Output:
        # the granted folder, checked as the file tools check it (moved or replaced by a link: refused)
        granted = Path(_root(ctx)) if ctx.folder is not None else None
        # never into the answer's last minute: the model needs it to answer
        seconds, note = time_limit(args["timeout_s"], ctx)
        # each process gets the CPU time of all cores for the whole time: the wall-clock limit is the real one
        run_limits = replace(
            limits, wall_s=float(seconds), cpu_s=int(seconds) * (os.cpu_count() or 1)
        )
        try:
            r = shell.run_shell(
                args["command"], ctx.work_dir, run_limits, ctx.abort, granted
            )
        except ValueError as e:
            raise ToolError(str(e)) from None
        if r.killed == "aborted":
            raise Cancelled
        return Output(format_result(r, run_limits, note), r.files)

    return Tool(
        name="run_shell",
        description=(
            "Run a shell command with zsh in a sandbox and return its exit code, stdout and stderr. The current "
            "folder (also HOME) is this chat's work folder: commands can read and write files there, the files stay "
            "for later calls in this chat, and the user gets them as links. When the user granted a folder, "
            "commands can read it (read-only, without hidden and secret files) at its full path. Nothing else of "
            f"the user's files. {programs} No network, no sudo, no access to other processes, no input (stdin is empty), and "
            "a program in the work folder cannot be started directly (run a script with sh, zsh or another "
            f"interpreter). Limits: timeout_s (default {DEFAULT_SECONDS}, at most {MAX_SECONDS}), "
            f"{limits.memory_mb} MB of memory, {limits.file_mb} MB per file, {limits.disk_mb} MB in the work "
            f"folder, {limits.processes} processes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": MAX_COMMAND},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SECONDS,
                    "default": DEFAULT_SECONDS,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        risk=Risk.R3,
        run=run_shell,
        side_effects=True,
        untrusted_output=True,  # it prints files and program output
        explain=lambda a: (
            "Runs this shell command in the sandbox for at most "
            f"{a.get('timeout_s', DEFAULT_SECONDS)} s: no network, writes only to this chat's work folder, can "
            "read the granted folder (if there is one) without changing it."
        ),
    )
