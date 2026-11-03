"""Run untrusted Python code: the Sandbox interface and its Seatbelt backend (sandbox-exec, on every supported Mac).

The interface is small, so an Apple `container` backend (a VM boundary and a real memory limit) can be added later
without changing the tools. Seatbelt profile (deny by default), built in profile():

- exec: only the interpreter (and the Python.app stub a framework build re-executes). No fork, so no /bin/sh, no
  curl, no child processes at all and no fork bomb.
- read: "/" itself, /usr/lib, /usr/share, /System/Library, the OS cryptexes, the time zone data, /dev/null,
  /dev/random and /dev/urandom, the interpreter's own folders, and the extra read-only paths a caller passes.
- read and write: the work dir only (and writing to /dev/null).
- file metadata (stat) anywhere. Directory listings and file contents elsewhere are refused.
- sysctl reads of the hardware facts (hw.*) and the OS name and version only: not the process table (kern.proc*).
- process information about itself only: "(deny default)" does not cover the process-info* operations, so the
  profile denies them and allows them for the process itself. Other processes' arguments and environment
  (KERN_PROCARGS2), their paths (proc_pidpath) and the process list (proc_listpids) are refused.
- the work dir node itself is not writable, so the code cannot remove it and put a symlink in its place. The host
  also refuses a work dir that is a symlink.
- no network rule, so every socket (TCP, UDP, Unix) and DNS lookup is refused; no mach-lookup, so no system service
  (such as the DNS resolver) can act for the code.

Limits around it: a wall-clock timeout, RLIMIT_CPU, RLIMIT_FSIZE, no core files, a cap on the text kept from stdout
and stderr, and a watchdog that polls the memory footprint of the process group (macOS refuses memory rlimits) and
the size of the work dir. The process runs in its own session; a timeout, Stop or a limit kills the whole group.
"""

import ctypes
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
POLL = 0.05  # seconds between watchdog checks
DISK_EVERY = 0.5  # seconds between work dir size checks
MAX_WORK_FILES = 10000

SYSTEM_READ = (
    "/usr/lib",
    "/usr/share",
    "/System/Library",
    "/System/Cryptexes",
    "/System/Volumes/Preboot/Cryptexes",
    "/private/var/db/timezone",
)
# what Python's start, os.uname() and platform read besides hw.* (found with the sandbox violation log)
SYSCTLS = (
    "kern.hostname",  # os.uname() needs it (ctypes, platform): the code can read the Mac's name
    "kern.ostype",
    "kern.osrelease",
    "kern.osversion",
    "kern.version",
    "kern.osproductversion",
    "kern.iossupportversion",
    "kern.osvariant_status",
    "security.mac.lockdown_mode_state",
)
DEVICES = ("/dev/null", "/dev/random", "/dev/urandom", "/dev/zero")


@dataclass(frozen=True)
class Limits:
    wall_s: float = 60.0  # wall-clock time
    cpu_s: int = 60  # CPU time (RLIMIT_CPU)
    memory_mb: int = 2048  # memory footprint of the process group (watchdog)
    file_mb: int = 50  # size of one written file (RLIMIT_FSIZE)
    disk_mb: int = 500  # size of the whole work dir (watchdog)
    output_chars: int = 20000  # kept of stdout and of stderr each


@dataclass
class RunResult:
    exit_code: int | None  # negative: killed by that signal
    stdout: str
    stderr: str
    seconds: float
    killed: str | None = None  # "timeout", "memory", "cpu", "disk", "aborted" or None
    peak_mb: float = 0.0
    files: list[str] = field(
        default_factory=list
    )  # new or changed, relative to the work dir
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class Sandbox(Protocol):
    name: str

    def unavailable(self) -> str | None:
        """None when this sandbox can run code here; else the reason."""
        ...

    def run_python(
        self,
        code: str,
        workdir: Path,
        limits: Limits,
        abort: threading.Event,
        read_only: Sequence[Path] = (),
    ) -> RunResult:
        """Run the code (from stdin, cwd = workdir) and return when the process group is gone."""
        ...


# Memory of a process group, from libproc (no psutil): the larger of the resident size and the physical footprint
# (the footprint also counts compressed memory, which a plain RSS misses).
class _RUsage(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


_libc = (
    ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    if sys.platform == "darwin"
    else None
)
PROC_PGRP_ONLY = 2
RUSAGE_INFO_V0 = 0


def group_pids(pgid: int) -> list[int]:
    if _libc is None:
        return [pgid]
    buf = (ctypes.c_int * 256)()
    n = _libc.proc_listpids(PROC_PGRP_ONLY, pgid, buf, ctypes.sizeof(buf))
    pids = [p for p in buf[: max(0, n) // ctypes.sizeof(ctypes.c_int)] if p > 0]
    return pids or [pgid]


def group_bytes(pgid: int) -> int:
    """The memory of every process in the group, in bytes (0 when it is gone)."""
    if _libc is None:
        return 0
    total = 0
    for pid in group_pids(pgid):
        info = _RUsage()
        if _libc.proc_pid_rusage(pid, RUSAGE_INFO_V0, ctypes.byref(info)) == 0:
            total += max(info.ri_resident_size, info.ri_phys_footprint)
    return total


def kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def snapshot(workdir: Path) -> dict[str, tuple[int, int]]:
    """Visible regular files (no dot names, no symlinks) under the work dir: relative path -> (size, mtime_ns)."""
    found: dict[str, tuple[int, int]] = {}
    for here, dirs, files in os.walk(workdir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = os.path.join(here, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not os.path.islink(path) and os.path.isfile(path):
                found[os.path.relpath(path, workdir)] = (st.st_size, st.st_mtime_ns)
    return found


def all_files(workdir: Path) -> set[str]:
    """Every file and link under the work dir, hidden ones too (absolute paths; links are not followed)."""
    found: set[str] = set()
    for here, dirs, files in os.walk(workdir):
        found.update(os.path.join(here, name) for name in files)
        found.update(
            os.path.join(here, d) for d in dirs if os.path.islink(os.path.join(here, d))
        )
    return found


def dir_size(workdir: Path) -> tuple[int, int]:
    """Bytes and entries under the work dir, counting at most MAX_WORK_FILES + 1 entries."""
    size = count = 0
    stack = [str(workdir)]
    while stack and count <= MAX_WORK_FILES:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    count += 1
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        size += e.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return size, count


class _Capture(threading.Thread):
    """Reads a pipe to the end and keeps the first `limit` bytes."""

    def __init__(self, pipe, limit: int) -> None:
        super().__init__(daemon=True)
        self.pipe, self.limit = pipe, limit
        self.data = bytearray()
        self.more = False

    def run(self) -> None:
        while chunk := self.pipe.read1(65536):
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.more = True

    def text(self, chars: int) -> tuple[str, bool]:
        text = self.data.decode("utf-8", errors="replace")
        cut = self.more or len(text) > chars
        return text[:chars], cut


@dataclass(frozen=True)
class Interpreter:
    argv: list[str]  # the interpreter and its flags; the code comes from stdin ("-")
    exec_paths: list[str]  # real paths the profile lets the sandbox exec
    read_paths: list[str]  # real folders the interpreter needs to read


def default_interpreter() -> Interpreter:
    """The app's base interpreter with -I -S: the standard library only, never a site-packages folder."""
    exe = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
    base = os.path.realpath(sys.base_prefix)
    return Interpreter([exe, "-I", "-S"], _execs(exe, base), [base])


def unsafe_interpreter(python: Path) -> str | None:
    """Why the app must not run this interpreter, or None. configured_interpreter() runs it once outside the sandbox
    (as the user), and the sandbox may exec it, so no other user may be able to change it: the file and every folder
    above it, up to "/", must belong to the user or root and must not be writable by the group or by others (whoever
    can write to a folder can rename what is in it, also a folder only the user can open). The path is checked as
    written (the file a symlink names, not the folders of its target)."""
    uid = os.getuid()
    path = Path(os.path.abspath(python))

    def problem(p: Path, st: os.stat_result) -> str | None:
        if st.st_uid not in (uid, 0):
            return f"{p} belongs to another user."
        if st.st_mode & 0o022:
            return (
                f"{p} can be changed by other users (it is group- or world-writable)."
            )
        return None

    try:
        if error := problem(path, os.stat(path)):
            return error
        for folder in path.parents:
            if error := problem(folder, os.stat(folder)):
                return error
    except OSError as e:
        return f"Cannot check {path}: {e.strerror}."
    return None


def configured_interpreter(python: Path) -> Interpreter:
    """A dedicated interpreter (usually a venv made for the sandbox), run with -I so its own site-packages work but
    no user site folder or PYTHON* variable does. Asks the interpreter once for its folders (outside the sandbox, so
    only after unsafe_interpreter() found nothing)."""
    if error := unsafe_interpreter(python):
        raise ValueError(f"The sandbox interpreter is refused: {error}")
    probe = (
        "import json, os, sys; print(json.dumps([sys.prefix, sys.base_prefix, "
        "os.path.realpath(getattr(sys, '_base_executable', sys.executable))]))"
    )
    out = subprocess.run(
        [str(python), "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout
    prefix, base, exe = json.loads(out)
    real = os.path.realpath(python)
    reads = list(dict.fromkeys(os.path.realpath(p) for p in (prefix, base)))
    return Interpreter([str(python), "-I"], _execs(real, reads[-1], exe), reads)


def _execs(exe: str, base: str, *more: str) -> list[str]:
    paths = [exe, *more]
    stub = os.path.join(base, "Resources", "Python.app", "Contents", "MacOS", "Python")
    if os.path.exists(
        stub
    ):  # a framework build re-executes itself through this app bundle
        paths.append(stub)
    return list(dict.fromkeys(paths))


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(interp: Interpreter, workdir: str, read_only: Sequence[str]) -> str:
    """The Seatbelt profile (SBPL) for one run."""
    sysctls = " ".join(
        ['(sysctl-name-prefix "hw.")'] + [f"(sysctl-name {_quote(n)})" for n in SYSCTLS]
    )
    execs = " ".join(f"(literal {_quote(p)})" for p in interp.exec_paths)
    reads = " ".join(
        [
            f"(subpath {_quote(p)})"
            for p in (*SYSTEM_READ, *interp.read_paths, *read_only)
        ]
        + [f"(literal {_quote(p)})" for p in ("/", *DEVICES)]
    )
    return f"""(version 1)
(deny default)
(deny process-info*)
(allow process-info* (target self))
(allow process-exec {execs})
(allow sysctl-read {sysctls})
(allow file-read-metadata)
(allow file-read* {reads})
(allow file-read* file-write* (subpath {_quote(workdir)}))
(deny file-write* (literal {_quote(workdir)}))
(allow file-write-data (literal "/dev/null"))
"""


class SeatbeltSandbox:
    name = "seatbelt"

    def __init__(self, python: Path | None = None) -> None:
        self.python = python
        self._interp: Interpreter | None = None

    def unavailable(self) -> str | None:
        if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
            return "sandbox-exec is not on this system (macOS only)."
        if self.python is not None and not self.python.exists():
            return f"The sandbox interpreter {self.python} does not exist ([agent] python in config.toml)."
        if self.python is not None and (error := unsafe_interpreter(self.python)):
            return f"The sandbox interpreter is refused ([agent] python in config.toml): {error}"
        return None

    def interpreter(self) -> Interpreter:
        if self._interp is None:
            self._interp = (
                default_interpreter()
                if self.python is None
                else configured_interpreter(self.python)
            )
        return self._interp

    def run_python(
        self,
        code: str,
        workdir: Path,
        limits: Limits,
        abort: threading.Event,
        read_only: Sequence[Path] = (),
    ) -> RunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        work = os.path.realpath(workdir)
        # an earlier run must not have turned the work dir into a link to another folder
        if workdir.is_symlink() or work != os.path.join(
            os.path.realpath(workdir.parent), workdir.name
        ):
            raise ValueError(f"The work dir {workdir} is a symlink; refusing to run.")
        tmp = os.path.join(work, ".tmp")
        if os.path.islink(tmp) or (os.path.exists(tmp) and not os.path.isdir(tmp)):
            os.unlink(tmp)  # made by the code: the host never follows it
        os.makedirs(tmp, exist_ok=True)
        interp = self.interpreter()
        sbpl = profile(interp, work, [os.path.realpath(p) for p in read_only])
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": work,
            "TMPDIR": tmp,
            "LANG": "en_US.UTF-8",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": os.path.join(tmp, "matplotlib"),
        }

        def limit_child() -> None:  # runs in the child between fork and exec
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s + 1))
            size = limits.file_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        before = snapshot(Path(work))
        before_all = all_files(Path(work))
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [SANDBOX_EXEC, "-p", sbpl, *interp.argv, "-B", "-X", "utf8", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work,
            env=env,
            start_new_session=True,
            preexec_fn=limit_child,  # noqa: PLW1509 - only setrlimit calls
        )
        out = _Capture(proc.stdout, limits.output_chars * 4)
        err = _Capture(proc.stderr, limits.output_chars * 4)
        out.start()
        err.start()
        killed = None
        peak = 0
        try:
            try:
                proc.stdin.write(code.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            next_disk = 0.0
            while proc.poll() is None:
                now = time.monotonic()
                if abort.is_set():
                    killed = "aborted"
                elif now - t0 > limits.wall_s:
                    killed = "timeout"
                else:
                    used = group_bytes(proc.pid)
                    peak = max(peak, used)
                    if used > limits.memory_mb * 1024 * 1024:
                        killed = "memory"
                    elif now >= next_disk:
                        next_disk = now + DISK_EVERY
                        size, count = dir_size(Path(work))
                        if (
                            size > limits.disk_mb * 1024 * 1024
                            or count > MAX_WORK_FILES
                        ):
                            killed = "disk"
                if killed:
                    kill_group(proc.pid)
                    break
                abort.wait(POLL)
            code_ = proc.wait()
        finally:
            # nothing of the run may outlive it; once reaped, its id may belong to another process, so not after
            if proc.returncode is None:
                kill_group(proc.pid)
                proc.wait()
        out.join(5)
        err.join(5)
        if killed is None and code_ == -signal.SIGXCPU:
            killed = "cpu"
        shutil.rmtree(tmp, ignore_errors=True)
        after = snapshot(Path(work))
        changed = sorted(p for p, v in after.items() if before.get(p) != v)
        size, count = dir_size(Path(work))
        if size > limits.disk_mb * 1024 * 1024 or count > MAX_WORK_FILES:
            # the watchdog looks every DISK_EVERY seconds: code that wrote fast and ended in between loses what it
            # wrote, hidden files too, so the folder is back under the limit for the next call
            for path in sorted(all_files(Path(work)) - before_all, reverse=True):
                Path(path).unlink(missing_ok=True)
            for rel in changed:
                (Path(work) / rel).unlink(missing_ok=True)
            killed = killed or "disk"
            changed = []
        stdout, stdout_cut = out.text(limits.output_chars)
        stderr, stderr_cut = err.text(limits.output_chars)
        return RunResult(
            exit_code=code_,
            stdout=stdout,
            stderr=stderr,
            seconds=round(time.monotonic() - t0, 2),
            killed=killed,
            peak_mb=round(peak / 2**20, 1),
            files=changed,
            stdout_truncated=stdout_cut,
            stderr_truncated=stderr_cut,
        )
