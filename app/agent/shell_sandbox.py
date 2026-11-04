"""Run a shell command in a Seatbelt sandbox (sandbox-exec): the backend of run_shell (app/agent/shell_tool.py).

It shares the limits and the supervision of run_python (sandbox.execute). The profile differs from the Python one:

- process-fork is allowed, so pipes and child processes work. Exec is allowed only from the system binary folders
  (/bin, /usr/bin) and Homebrew's (/opt/homebrew/bin and the Cellar it links into). A setuid or setgid program
  (sudo, su, ps, top, login...) is refused, so nothing can run with more rights than the user.
- read: the system folders the programs need, Homebrew's program folders (not its etc or var), the work dir, and
  the chat's granted folder read-only (without hidden and secret files, as the file tools). Nothing else of the
  home folder.
- write: the work dir only (and /dev/null). The work dir node itself is not writable. Extended attributes, resource
  forks and file flags stay allowed (cp, tar and Python's shutil.copy2 set them); the disk limit counts the first two
  (sandbox.disk_bytes), and the host clears the flags before it removes a file (sandbox.unlock).
- process information about itself only, also for each child: (deny process-info*) + (allow process-info* (target
  self)). No sysctl of the process table. So no child reads other processes' arguments or environment.
- no network rule and no mach-lookup (except one random name that no service has, see below): every socket, DNS
  lookup and system service (keychain, pasteboard, Apple Events, launchd) is refused.

A child can leave the process group (setsid), so the host finds the run's processes by their sandbox: the profile
allows mach-lookup of one random name, and the host asks the kernel (sandbox_check) for every process of the user
whose sandbox allows that name and refuses a second random name. Only this run's profile names them, so a process
that matches is a process of this run. After a timeout, Stop, a limit or the end of the main process, reap() kills
each one the moment it finds it and looks again until it found none for CALM_SECONDS, so also a process that forks
and exits in a loop ends. When that fails, the result says so, a thread goes on hunting, and new commands are refused
until it is done. RLIMIT_NPROC caps how many processes the run can add.
"""

import ctypes
import os
import resource
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from app.agent.file_tools import SECRET_NAMES
from app.agent.sandbox import (
    DEVICES,
    SANDBOX_EXEC,
    SYSCTLS,
    SYSTEM_READ,
    Limits,
    RunResult,
    _quote,
    execute,
    prepare,
)

# no startup files (-f); background jobs keep their priority (the sandbox refuses nice)
SHELL = ("/bin/zsh", "-f", "+o", "bgnice", "-c")
SYSTEM_EXEC = ("/bin", "/usr/bin")
HOMEBREW = "/opt/homebrew"
# Homebrew's program folders: the links in bin and opt point into the Cellar; lib and share hold what they load
HOMEBREW_EXEC = ("bin", "Cellar", "opt")
HOMEBREW_READ = ("bin", "Cellar", "opt", "lib", "share", "Frameworks")
SHELL_READ = (
    *SYSTEM_READ,
    "/bin",
    "/usr/bin",
    "/usr/libexec",
)
SHELL_LITERALS = ("/", "/private/etc/localtime", *DEVICES)

PROC_UID_ONLY = 4
SANDBOX_FILTER_NONE = 0
SANDBOX_FILTER_GLOBAL_NAME = 2
SANDBOX_CHECK_NO_REPORT = 0x40000000
REAP_SECONDS = 5.0  # how long the end of a run hunts for its processes before it gives up and leaves it to a thread
CALM_SECONDS = 0.3  # the hunt ends after this long without finding a process of the run
# Between full looks (every process of the user), the hunt checks only the QUICK_LOOK newest processes: a process
# that forks and exits in a loop is always among the newest, and a full look over the ~750 processes of a busy Mac
# is slower than such a loop.
FULL_LOOK_SECONDS = 0.05
QUICK_LOOK = 64

_libc = (
    ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    if sys.platform == "darwin"
    else None
)
if _libc is not None:
    # int sandbox_check(pid_t pid, const char *operation, enum sandbox_filter_type type, ...): the filter's value
    # goes as a variadic argument (ctypes passes arguments past argtypes as variadic ones)
    _libc.sandbox_check.restype = ctypes.c_int
    _libc.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]


def user_pids() -> list[int]:
    """The ids of every process of this user, the newest first (the kernel's order). Do not sort them: ids wrap
    around at 99,999, and a loop of forks uses up thousands of ids a second, so a high id is not a new process."""
    if _libc is None:
        return []
    uid = os.getuid()
    need = max(_libc.proc_listpids(PROC_UID_ONLY, uid, None, 0), 0)
    buf = (ctypes.c_int * (need // ctypes.sizeof(ctypes.c_int) + 1024))()
    n = _libc.proc_listpids(PROC_UID_ONLY, uid, buf, ctypes.sizeof(buf))
    return [p for p in buf[: max(0, n) // ctypes.sizeof(ctypes.c_int)] if p > 0]


def _lookup_allowed(pid: int, name: str) -> int:
    return _libc.sandbox_check(
        pid,
        b"mach-lookup",
        SANDBOX_FILTER_GLOBAL_NAME | SANDBOX_CHECK_NO_REPORT,
        ctypes.c_char_p(name.encode()),
    )


def in_sandbox(pid: int, marker: str, other: str) -> bool:
    """Whether the process runs under the profile that allows `marker`: it is sandboxed, its sandbox allows the
    marker name (0) and refuses the other random name (1). A profile that allows every name, or none, fails one of
    the two."""
    if _libc is None or pid == os.getpid():
        return False
    return (
        _libc.sandbox_check(pid, None, SANDBOX_FILTER_NONE) == 1
        and _lookup_allowed(pid, marker) == 0
        and _lookup_allowed(pid, other) == 1
    )


def members_of(marker: str, other: str) -> list[int]:
    """This user's processes that run under the profile with this marker."""
    return [pid for pid in user_pids() if in_sandbox(pid, marker, other)]


def reap(marker: str, other: str, seconds: float = REAP_SECONDS) -> bool:
    """SIGKILL every process of the run, each one the moment a look finds it (the newest first), and keep looking
    without a pause until no look found one for CALM_SECONDS: True. False when that did not happen within `seconds`.
    A process that forks and exits in a loop can slip between two looks; it cannot do that for hundreds of looks in
    a row. Between full looks (FULL_LOOK_SECONDS) a look checks only the QUICK_LOOK newest processes; the calm time
    is longer than that, so the hunt never ends without full looks that found nothing."""
    deadline = time.monotonic() + seconds
    last_hit = time.monotonic()
    full_at = 0.0  # when the next full look is due
    while (now := time.monotonic()) < deadline:
        if now - last_hit >= CALM_SECONDS:
            return True
        pids = user_pids()
        if now >= full_at:
            full_at = now + FULL_LOOK_SECONDS
        else:
            pids = pids[:QUICK_LOOK]
        for pid in pids:
            if in_sandbox(pid, marker, other):
                last_hit = time.monotonic()
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    return False


def homebrew() -> str | None:
    """Homebrew's prefix when it is there and belongs to this user or root; else None (its programs are not
    offered). Its folders are often writable by the admin group, which the sandbox cannot make worse: it never
    writes there, and a program it runs stays in the sandbox."""
    try:
        st = os.stat(HOMEBREW)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (os.getuid(), 0):
        return None
    return HOMEBREW


def _ci(text: str) -> str:
    """A regex part that matches the text in any letter case (Seatbelt regexes have no flag for it)."""
    out = []
    for ch in text:
        if ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch == "*":
            out.append("[^/]*")
        elif ch.isalnum() or ch in "_-":
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


def secret_parts() -> list[str]:
    """Regexes for a path part that is hidden or a secret file (file_tools.SECRET_NAMES), for the granted folder.
    Several short ones: Seatbelt refuses a string of more than about 1,000 characters."""
    names = [_ci(p) for p in SECRET_NAMES]
    out = ["/\\."]
    for i in range(0, len(names), 8):
        out.append("/(" + "|".join(names[i : i + 8]) + ")(/|$)")
    return out


def setid_programs(folders: Sequence[str]) -> list[str]:
    """The setuid and setgid files directly in these folders (real paths)."""
    found = []
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                found.append(os.path.realpath(path))
    return sorted(set(found))


def shell_profile(
    workdir: str, granted: str | None, marker: str, brew: str | None
) -> str:
    """The Seatbelt profile (SBPL) for one run_shell call."""
    execs = list(SYSTEM_EXEC) + [f"{brew}/{d}" for d in HOMEBREW_EXEC] * bool(brew)
    reads = list(SHELL_READ) + [f"{brew}/{d}" for d in HOMEBREW_READ] * bool(brew)
    sysctls = " ".join(
        ['(sysctl-name-prefix "hw.")'] + [f"(sysctl-name {_quote(n)})" for n in SYSCTLS]
    )
    setid = " ".join(f"(literal {_quote(p)})" for p in setid_programs(SYSTEM_EXEC))
    lines = [
        "(version 1)",
        "(deny default)",
        "(deny process-info*)",
        "(allow process-info* (target self))",
        "(allow process-fork)",
        "(allow process-exec "
        + " ".join(f"(subpath {_quote(p)})" for p in execs)
        + ")",
        # setuid and setgid programs never run: by mode (any folder) and by name (the system ones, found now)
        "(deny process-exec (file-mode #o4000) (file-mode #o2000)"
        + (f" {setid}" if setid else "")
        + ")",
        "(allow signal (target same-sandbox))",
        f"(allow sysctl-read {sysctls})",
        "(allow file-read-metadata)",
        "(allow file-read* "
        + " ".join(
            [f"(subpath {_quote(p)})" for p in reads]
            + [f"(literal {_quote(p)})" for p in SHELL_LITERALS]
        )
        + ")",
    ]
    if granted:
        lines += [
            f"(allow file-read* (subpath {_quote(granted)}))",
        ] + [
            f'(deny file-read* (require-all (subpath {_quote(granted)}) (regex #"{rx}")))'
            for rx in secret_parts()
        ]
    lines += [
        f"(allow file-read* file-write* (subpath {_quote(workdir)}))",
        f"(deny file-write* (literal {_quote(workdir)}))",
        '(allow file-write-data (literal "/dev/null"))',
        f"(allow mach-lookup (global-name {_quote(marker)}))",
    ]
    return "\n".join(lines) + "\n"


_tracking: str | None = None  # the self-test's answer, once per app run ("" = it works)
_tracking_lock = threading.Lock()


def tracking_problem() -> str | None:
    """Why the host cannot find a run's processes by their sandbox, or None. Runs a sandboxed sleep once (per app
    run) and checks that the scan finds exactly it."""
    global _tracking
    with _tracking_lock:
        if _tracking is None:
            _tracking = _self_test()
        return _tracking or None


def _self_test() -> str:
    marker, other = (f"mmw-run-{secrets.token_hex(16)}" for _ in range(2))
    # the real profile (no Homebrew, no granted folder): this also checks that sandbox-exec takes it
    sbpl = shell_profile("/var/empty/mmw-self-test", None, marker, None)
    try:
        proc = subprocess.Popen(
            [SANDBOX_EXEC, "-p", sbpl, "/bin/sleep", "10"],
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
    except OSError as e:
        return f"sandbox-exec does not start: {e.strerror}."
    try:
        found: list[int] = []
        for _ in range(100):  # sandbox-exec applies the profile, then runs sleep
            found = members_of(marker, other)
            if found:
                break
            time.sleep(0.01)
        if found != [proc.pid]:
            return "The app cannot find the processes of a sandbox run (sandbox_check), so it could not stop them all."
        return ""
    finally:
        proc.kill()
        proc.wait()


class SeatbeltShell:
    name = "seatbelt"

    def __init__(self) -> None:
        # markers of runs whose processes could not all be stopped; a thread keeps hunting them
        self.escaped: set[tuple[str, str]] = set()
        self.lock = threading.Lock()

    def _hunt(self, marker: str, other: str) -> None:
        while not reap(marker, other):
            time.sleep(1)
        with self.lock:
            self.escaped.discard((marker, other))

    def blocked(self) -> str | None:
        """Why no new command may run now: processes of an earlier run still escape the reaper."""
        with self.lock:
            if self.escaped:
                return (
                    "Processes of an earlier run_shell command could not be stopped yet; the app keeps stopping "
                    "them. No new command runs until they are gone."
                )
        return None

    def unavailable(self) -> str | None:
        if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
            return "sandbox-exec is not on this system (macOS only)."
        if not os.path.exists(SHELL[0]):
            return f"{SHELL[0]} is missing."
        return tracking_problem()

    def run_shell(
        self,
        command: str,
        workdir: Path,
        limits: Limits,
        abort: threading.Event,
        read_only: Path | None = None,
    ) -> RunResult:
        """Run the command with zsh in the sandbox (cwd = workdir, stdin = /dev/null); return when every process of
        the run is gone. read_only: the granted folder (a real path the caller checked), or None."""
        if error := self.blocked():
            raise ValueError(error)
        work, tmp = prepare(workdir)
        brew = homebrew()
        marker, other = (f"mmw-run-{secrets.token_hex(16)}" for _ in range(2))
        granted = os.path.realpath(read_only) if read_only is not None else None
        sbpl = shell_profile(work, granted, marker, brew)
        env = {
            "PATH": ":".join(([f"{brew}/bin"] if brew else []) + ["/usr/bin", "/bin"]),
            "HOME": work,
            "TMPDIR": tmp,
            "LANG": "en_US.UTF-8",
            "SHELL": SHELL[0],
            "TMPPREFIX": os.path.join(tmp, "zsh"),  # here-documents
        }
        # RLIMIT_NPROC counts every process of the user: allow the ones there now plus the run's share
        nproc = len(user_pids()) + limits.processes
        hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
        if hard != resource.RLIM_INFINITY:
            nproc = min(nproc, hard)
        argv = [SANDBOX_EXEC, "-p", sbpl, *SHELL, command]
        try:
            r = execute(
                argv,
                work,
                tmp,
                env,
                limits,
                abort,
                nproc=nproc,
                members=lambda: members_of(marker, other),
                reap=lambda: reap(marker, other),
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if r.leftover:
            with self.lock:
                self.escaped.add((marker, other))
            threading.Thread(
                target=self._hunt, args=(marker, other), daemon=True
            ).start()
        return r
