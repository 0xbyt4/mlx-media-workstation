"""The run_shell sandbox, for real: each test runs commands under sandbox-exec and checks that the profile and the
limits hold. macOS only; every file outside the work dir is a temp file made by the test."""

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.agent import Context, Limits, Settings, build_registry
from app.agent import sandbox as sb
from app.agent import shell_sandbox as ss
from tests.test_agent_sandbox import PEEK

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(sb.SANDBOX_EXEC),
    reason="needs macOS sandbox-exec",
)

FAST = Limits(wall_s=10, cpu_s=10, memory_mb=512, file_mb=5, disk_mb=50)
BREW_PYTHON = "/opt/homebrew/bin/python3"


@pytest.fixture(scope="module")
def box() -> ss.SeatbeltShell:
    shell = ss.SeatbeltShell()
    assert shell.unavailable() is None
    return shell


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture
def granted(tmp_path: Path) -> Path:
    """A granted folder with a text file, a hidden file and a secret file."""
    folder = tmp_path / "granted"
    (folder / "sub").mkdir(parents=True)
    (folder / "notes.txt").write_text("granted text\n")
    (folder / "sub" / "deep.txt").write_text("deep text\n")
    (folder / ".hidden").write_text("dummy hidden\n")
    (folder / "server.pem").write_text("dummy pem\n")
    (folder / "SECRETS.json").write_text("dummy secrets\n")
    return folder


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A private file next to the work dir, not in it."""
    path = tmp_path / "private" / "secret.txt"
    path.parent.mkdir()
    path.write_text("dummy secret\n")
    return path


def run(
    box: ss.SeatbeltShell,
    work: Path,
    command: str,
    limits: Limits = FAST,
    read_only: Path | None = None,
) -> sb.RunResult:
    return box.run_shell(command, work, limits, threading.Event(), read_only)


def lines(r: sb.RunResult) -> list[str]:
    return r.stdout.split()


@pytest.fixture
def other_process():
    """A helper process with a fake marker in its environment and its argv."""
    marker = "mmw-marker-" + os.urandom(4).hex()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", marker],
        env={"PATH": "/usr/bin:/bin", "MMW_TEST_MARKER": marker},
    )
    try:
        yield proc.pid, marker
    finally:
        proc.kill()
        proc.wait()


def running(tag: str) -> list[str]:
    """Processes of this user whose command line holds the tag (seen from outside the sandbox)."""
    out = subprocess.run(
        ["/usr/bin/pgrep", "-f", tag], capture_output=True, text=True, check=False
    ).stdout
    return out.split()


def test_the_profile_denies_by_default(box: ss.SeatbeltShell) -> None:
    text = ss.shell_profile("/private/var/w", "/Users/x/notes", "mmw-run-ab", None)
    assert "(deny default)" in text
    assert "(deny process-info*)\n(allow process-info* (target self))" in text
    assert "(allow process-fork)" in text
    assert "network" not in text
    assert text.count("mach-lookup") == 1 and '(global-name "mmw-run-ab")' in text
    assert str(Path.home()) not in text
    assert "/opt/homebrew" not in text
    assert '(allow file-read* file-write* (subpath "/private/var/w"))' in text
    assert '(deny file-write* (literal "/private/var/w"))' in text
    # the granted folder: read only, without hidden and secret names
    assert '(allow file-read* (subpath "/Users/x/notes"))' in text
    assert "file-write" not in "".join(
        line for line in text.splitlines() if "/Users/x/notes" in line
    )
    assert "(deny process-exec (file-mode #o4000) (file-mode #o2000)" in text
    assert '(literal "/usr/bin/sudo")' in text and '(literal "/bin/ps")' in text
    brew = ss.shell_profile("/w", None, "m", "/opt/homebrew")
    assert '(subpath "/opt/homebrew/Cellar")' in brew
    assert "/opt/homebrew/etc" not in brew and "/opt/homebrew/var" not in brew


def test_commands_run_and_write_in_the_work_dir(
    box: ss.SeatbeltShell, work: Path
) -> None:
    r = run(
        box,
        work,
        'echo hello; [ "$PWD" = "$HOME" ] && echo home-is-work; printf "b\\na\\n" | sort | head -1\n'
        'mkdir -p out && seq 1 3 > out/n.txt && awk "{s+=\\$1} END {print s}" out/n.txt\n'
        "cat <<< heredoc; echo err >&2; jq -n '1+1'",
    )
    assert r.exit_code == 0 and r.killed is None, r.stderr
    assert lines(r) == ["hello", "home-is-work", "a", "6", "heredoc", "2"]
    assert r.stderr == "err\n"
    assert (work / "out" / "n.txt").read_text() == "1\n2\n3\n"
    assert r.files == ["out/n.txt"]
    assert not (work / ".tmp").exists()
    r = run(box, work, "cat out/n.txt | wc -l; exit 3")
    assert r.exit_code == 3 and lines(r) == ["3"] and r.files == []


def test_exec_only_from_the_system_and_homebrew_folders(
    box: ss.SeatbeltShell, work: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "bin" / "echo"
    elsewhere.parent.mkdir()
    shutil.copyfile("/bin/echo", elsewhere)  # a program outside the allowed folders
    elsewhere.chmod(0o755)
    command = (
        "cp /bin/echo ./myecho && ./myecho copied; echo $?\n"
        'printf "#!/bin/sh\\necho script\\n" > s.sh && chmod +x s.sh && ./s.sh; echo $?\n'
        "sh s.sh\n"  # an allowed interpreter may read the script
        f"{elsewhere} elsewhere; echo $?\n"
        "/usr/sbin/sysctl -n kern.ostype; echo $?\n"
        "/sbin/md5 -s x; echo $?\n"
        "/usr/libexec/PlistBuddy -h; echo $?\n"
        "/bin/echo allowed; /usr/bin/printf 'allowed\\n'"
    )
    r = run(box, work, command)
    assert lines(r) == ["127", "127", "script"] + ["127"] * 4 + ["allowed"] * 2, (
        r.stderr
    )
    assert r.stderr.count("operation not permitted") == 6
    assert "copied" not in r.stdout and "elsewhere" not in r.stdout


def test_setuid_programs_never_run(box: ss.SeatbeltShell, work: Path) -> None:
    r = run(
        box,
        work,
        "for p in /usr/bin/sudo /usr/bin/su /bin/ps /usr/bin/top /usr/bin/login /usr/bin/crontab; do\n"
        "  $p -h </dev/null >/dev/null 2>&1; echo $?\ndone\nsudo -n true 2>/dev/null; echo $?",
    )
    assert lines(r) == ["127"] * 7, r.stderr


def test_cannot_write_outside_the_work_dir(
    box: ss.SeatbeltShell, work: Path, outside: Path, granted: Path
) -> None:
    targets = [
        outside,
        outside.parent / "new.txt",
        work.parent / "beside.txt",
        granted / "notes.txt",
        granted / "new.txt",
        Path("/tmp/mmw-shell-test.txt"),
        Path.home() / "mmw-shell-test.txt",
    ]
    command = "\n".join(
        f"echo x > {str(p)!r} 2>/dev/null && echo wrote || echo denied" for p in targets
    )
    command += f"\nln {str(granted / 'notes.txt')!r} link.txt 2>/dev/null && echo linked || echo denied"
    command += f"\nmv {str(granted / 'notes.txt')!r} moved.txt 2>/dev/null && echo moved || echo denied"
    r = run(box, work, command, read_only=granted)
    assert lines(r) == ["denied"] * 9, r.stderr
    assert outside.read_text() == "dummy secret\n"
    assert (granted / "notes.txt").read_text() == "granted text\n"
    assert not (granted / "new.txt").exists()
    assert not (work.parent / "beside.txt").exists()
    assert not Path("/tmp/mmw-shell-test.txt").exists()
    assert not (Path.home() / "mmw-shell-test.txt").exists()


def test_cannot_read_the_home_folder_or_other_files(
    box: ss.SeatbeltShell, work: Path, outside: Path
) -> None:
    home = str(Path.home())
    paths = [str(outside), home, f"{home}/Library", f"{home}/Documents", "/Users"]
    paths += [
        str(outside.parent),
        "/etc/passwd",
        "/private/var/db",
        "/opt/homebrew/etc",
    ]
    command = "\n".join(
        f"{{ if [ -d {p!r} ]; then ls {p!r}; else cat {p!r}; fi }} >/dev/null 2>&1 "
        "&& echo read || echo denied"
        for p in paths
    )
    r = run(box, work, command)
    assert lines(r) == ["denied"] * len(paths), r.stderr
    assert "dummy secret" not in r.stdout


def test_the_granted_folder_is_read_only_without_hidden_and_secret_files(
    box: ss.SeatbeltShell, work: Path, granted: Path
) -> None:
    g = str(granted)
    command = (
        f"cat {g}/notes.txt {g}/sub/deep.txt; ls {g} | LC_ALL=C sort | tr '\\n' ' '; echo\n"
        f"cp {g}/notes.txt copy.txt && echo copied\n"
        f"for f in .hidden server.pem SECRETS.json; do cat {g}/$f 2>/dev/null || echo denied; done\n"
        f"cat {g}/../granted/.hidden 2>/dev/null || echo denied"
    )
    r = run(box, work, command, read_only=granted)
    assert (
        r.stdout.splitlines()
        == [
            "granted text",
            "deep text",
            "SECRETS.json notes.txt server.pem sub ",  # names are listed; the contents are refused
            "copied",
            "denied",
            "denied",
            "denied",
            "denied",
        ]
    ), r.stderr
    assert "dummy" not in r.stdout
    assert (work / "copy.txt").read_text() == "granted text\n"
    # without a grant the folder is not readable at all
    r = run(box, work, f"cat {g}/notes.txt 2>/dev/null || echo denied")
    assert lines(r) == ["denied"]


def test_no_network(box: ss.SeatbeltShell, work: Path) -> None:
    command = (
        "curl -sS -m 3 https://example.com >/dev/null 2>&1 && echo open || echo blocked\n"
        "curl -sS -m 3 http://127.0.0.1:7870/ >/dev/null 2>&1 && echo open || echo blocked\n"
        "nc -z -G 2 1.1.1.1 443 >/dev/null 2>&1 && echo open || echo blocked\n"
        "nc -z -G 2 127.0.0.1 7870 >/dev/null 2>&1 && echo open || echo blocked\n"
        "echo x | nc -U /var/run/mDNSResponder >/dev/null 2>&1 && echo open || echo blocked\n"
        "host example.com >/dev/null 2>&1 && echo open || echo blocked"
    )
    r = run(box, work, command)
    assert lines(r) == ["blocked"] * 6, r.stderr


def test_no_system_services(box: ss.SeatbeltShell, work: Path) -> None:
    """No mach-lookup: no pasteboard, keychain, Apple Events or Spotlight."""
    command = (
        "pbpaste >/dev/null 2>&1 && echo open || echo blocked\n"
        "security find-generic-password -s mmw-none >/dev/null 2>&1 && echo open || echo blocked\n"
        "osascript -e 'tell application \"Finder\" to get name of every disk' >/dev/null 2>&1 "
        "&& echo open || echo blocked\n"
        'test -z "$(mdfind -onlyin / -name notes.txt 2>/dev/null)" && echo blocked || echo open'
    )
    r = run(box, work, command)
    assert lines(r) == ["blocked"] * 4, r.stderr


def test_cannot_see_or_signal_other_processes(
    box: ss.SeatbeltShell, work: Path, other_process
) -> None:
    pid, marker = other_process
    command = (
        f"pgrep -f {marker} >/dev/null 2>&1 && echo seen || echo hidden\n"
        "ps ax >/dev/null 2>&1 && echo seen || echo hidden\n"
        "/bin/ps ax >/dev/null 2>&1 && echo seen || echo hidden\n"
        "/usr/sbin/sysctl kern.proc.all >/dev/null 2>&1 && echo seen || echo hidden\n"
        f"kill -0 {pid} 2>/dev/null && echo signalled || echo refused\n"
        f"kill -STOP {pid} 2>/dev/null && echo signalled || echo refused\n"
        "sleep 5 & kill $! && echo own-child"
    )
    r = run(box, work, command)
    assert lines(r) == ["hidden"] * 4 + ["refused"] * 2 + ["own-child"], r.stderr
    assert marker not in r.stdout + r.stderr


@pytest.mark.skipif(
    ss.homebrew() is None or not os.path.exists(BREW_PYTHON),
    reason="needs Homebrew's python3",
)
def test_a_forked_child_cannot_read_other_processes(
    box: ss.SeatbeltShell, work: Path, other_process
) -> None:
    """The process-info rule holds for a child of the shell: it reads neither the argv and environment of another
    process (KERN_PROCARGS2) nor the pid list nor other processes' paths."""
    pid, marker = other_process
    (work).mkdir(parents=True, exist_ok=True)
    (work / "peek.py").write_text(PEEK.format(pid=pid, marker=marker))
    r = run(box, work, "python3 peek.py | cat")  # a child of the shell, in a pipe
    assert r.exit_code == 0, r.stderr
    assert marker not in r.stdout
    out = r.stdout.split()
    assert out[:3] == ["procargs", "denied", "False"], r.stdout
    assert out[3:6] == ["listpids", "0", "False"], r.stdout
    assert out[6:8] == ["pidpath", "0"], r.stdout


def test_timeout_kills_every_process_of_the_run(
    box: ss.SeatbeltShell, work: Path
) -> None:
    """A background child, a child in its own session (setsid) and the shell itself all end."""
    tag = f"{secrets.randbelow(10**6) + 10**6}.5"
    command = (
        f"sleep {tag} &\n"
        f'(perl -MPOSIX -e \'POSIX::setsid(); exec "/bin/sleep", "{tag}"\' &)\n'
        f"sleep {tag}"
    )
    t0 = time.monotonic()
    r = run(box, work, command, Limits(wall_s=1))
    assert r.killed == "timeout" and r.exit_code == -9
    assert time.monotonic() - t0 < 8
    assert running(f"sleep {tag}") == []


def test_background_children_end_with_the_command(
    box: ss.SeatbeltShell, work: Path
) -> None:
    tag = f"{secrets.randbelow(10**6) + 10**6}.25"
    command = (
        f"sleep {tag} &\n"
        f'(perl -MPOSIX -e \'POSIX::setsid(); exec "/bin/sleep", "{tag}"\' &)\n'
        "echo started"
    )
    t0 = time.monotonic()
    r = run(box, work, command)
    assert r.exit_code == 0 and lines(r) == ["started"]
    assert (
        time.monotonic() - t0 < 5
    )  # the children held stdout open; they were killed, not waited for
    assert running(f"sleep {tag}") == []


HOPPER = (
    # forks, the child leaves the session (setsid) and the parent exits, over and over; it ends by itself after 20 s
    "perl -MPOSIX -e '$t = time + 20; $n = 0; while (time < $t) { exit 0 if fork(); POSIX::setsid(); "
    'if (++$n % 100 == 0) { open(F, ">hop$ARGV[0].txt"); print F $n; close F } }\' '
)


def test_processes_that_hop_to_new_sessions_are_stopped(
    box: ss.SeatbeltShell, work: Path
) -> None:
    command = (
        "\n".join(f"{HOPPER}{i} &" for i in range(4)) + "\nsleep 0.5; echo started"
    )
    for limits in (
        FAST,
        Limits(wall_s=0.3),
    ):  # at the end of the command, and at the timeout
        r = run(box, work, command, limits)
        assert not r.leftover and r.seconds < 4
        seen = [p.read_text() for p in sorted(work.glob("hop*.txt"))]
        time.sleep(0.5)
        assert [p.read_text() for p in sorted(work.glob("hop*.txt"))] == seen
    assert box.blocked() is None


def test_a_run_whose_processes_escape_blocks_new_runs(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the reaper cannot confirm that a run's processes are gone, a thread keeps hunting them and run_shell
    refuses new commands until it is done."""
    real = ss.reap
    calls: list[str] = []
    gate = threading.Event()

    def first_fails(marker, other, seconds=ss.REAP_SECONDS):
        calls.append(marker)
        if len(calls) == 1:
            return False
        gate.wait(
            5
        )  # the hunting thread waits until the test looked at the blocked state
        return real(marker, other, seconds)

    monkeypatch.setattr(ss, "reap", first_fails)
    shell = ss.SeatbeltShell()
    r = run(shell, work, "echo hi")
    assert r.leftover and r.stdout == "hi\n"
    assert "could not be stopped" in (shell.blocked() or "")
    with pytest.raises(ValueError, match="could not be stopped"):
        run(shell, work, "echo again")
    gate.set()
    assert wait(lambda: shell.blocked() is None)
    assert run(shell, work, "echo again").stdout == "again\n"


def wait(cond, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_the_process_limit_stops_a_fork_bomb(box: ss.SeatbeltShell, work: Path) -> None:
    tag = f"{secrets.randbelow(10**6) + 10**6}.75"
    command = (
        f"for i in {{1..400}}; do sleep {tag} & if [ $? -ne 0 ]; then break; fi; "
        "echo $i > count.txt; done"
    )
    r = run(box, work, command, Limits(wall_s=10, processes=16))
    assert "fork failed" in r.stderr
    # RLIMIT_NPROC counts all of the user's processes: the run gets about 16 more than there were at its start
    assert int((work / "count.txt").read_text()) < 100
    assert running(f"sleep {tag}") == []


def test_abort_stops_it(box: ss.SeatbeltShell, work: Path) -> None:
    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    t0 = time.monotonic()
    r = box.run_shell("sleep 30", work, FAST, abort)
    assert r.killed == "aborted"
    assert time.monotonic() - t0 < 5


def test_output_is_capped(box: ss.SeatbeltShell, work: Path) -> None:
    r = run(
        box,
        work,
        "head -c 100000 /dev/zero | tr '\\0' a; head -c 100000 /dev/zero | tr '\\0' b >&2",
        Limits(output_chars=1000),
    )
    assert r.stdout == "a" * 1000 and r.stdout_truncated
    assert r.stderr == "b" * 1000 and r.stderr_truncated


def test_file_size_limit(box: ss.SeatbeltShell, work: Path) -> None:
    r = run(
        box, work, "head -c 2000000 /dev/zero > big.bin; echo $?", Limits(file_mb=1)
    )
    assert lines(r)[-1] != "0"
    assert (work / "big.bin").stat().st_size <= 1024 * 1024


def test_memory_watchdog(box: ss.SeatbeltShell, work: Path) -> None:
    r = run(
        box,
        work,
        'perl -e \'$x = "x" x (700 * 1024 * 1024); print "allocated\\n"; sleep 10\' | cat',
        Limits(wall_s=10, memory_mb=256),
    )
    assert r.killed == "memory" and "allocated" not in r.stdout
    assert r.seconds < 8


def test_a_fast_write_past_the_disk_limit_is_removed(
    box: ss.SeatbeltShell, work: Path
) -> None:
    work.mkdir(parents=True)
    (work / "kept.txt").write_text("from an earlier call")
    r = run(
        box,
        work,
        "mkdir -p d/e; for i in {1..30}; do head -c 1048576 /dev/zero > d/e/f$i.bin; done",
        Limits(wall_s=10, disk_mb=10),
    )
    assert r.killed == "disk"
    assert sorted(p.name for p in work.iterdir()) == ["kept.txt"]


def test_environment_is_clean(
    box: ss.SeatbeltShell, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "dummy-token")
    r = run(
        box,
        work,
        "env | cut -d= -f1 | sort | tr '\\n' ' '; echo; echo $HOME; echo $TMPDIR",
    )
    names, home, tmp = r.stdout.splitlines()
    assert "HF_TOKEN" not in names and "dummy-token" not in r.stdout
    assert set(names.split()) <= {
        "HOME",
        "LANG",
        "LOGNAME",  # set by zsh
        "OLDPWD",
        "PATH",
        "PWD",
        "SHELL",
        "SHLVL",
        "TMPDIR",
        "TMPPREFIX",
        "_",
    }
    assert home == os.path.realpath(work) and tmp == os.path.realpath(work / ".tmp")


def test_a_linked_work_dir_is_refused(
    box: ss.SeatbeltShell, work: Path, outside: Path
) -> None:
    work.symlink_to(outside.parent)
    with pytest.raises(ValueError, match="symlink"):
        run(box, work, "cat secret.txt")


def test_run_shell_tool(tmp_path: Path, granted: Path) -> None:
    reg = build_registry(None, Settings(), limits=FAST)
    tool = reg.get("run_shell")
    assert tool is not None and tool.untrusted_output and not tool.network
    work = tmp_path / "ws"
    ctx = Context("c1", granted, threading.Event(), work)
    r = reg.call(
        "run_shell",
        {"command": f"cat {granted}/notes.txt > copy.txt; cat copy.txt; echo oops >&2"},
        ctx,
    )
    assert r.status == "ok" and r.args == {
        "command": f"cat {granted}/notes.txt > copy.txt; cat copy.txt; echo oops >&2",
        "timeout_s": 120,
    }
    assert r.content.startswith("exit code: 0 (")
    assert "stdout (13 characters):\ngranted text\n" in r.content
    assert "stderr (5 characters):\noops\n" in r.content
    assert "files written: copy.txt" in r.content and r.files == ["copy.txt"]
    r = reg.call("run_shell", {"command": "true"}, ctx)
    assert r.content.endswith("no output")
    r = reg.call("run_shell", {"command": "sleep 5", "timeout_s": 1}, ctx)
    assert r.status == "ok" and "stopped: it ran longer than 1 s" in r.content
    assert (
        reg.call("run_shell", {"command": "x", "timeout_s": 601}, ctx).status == "error"
    )
    # a granted folder that was moved: refused, as the file tools refuse it
    moved = Context("c1", tmp_path / "gone", threading.Event(), work)
    r = reg.call("run_shell", {"command": "true"}, moved)
    assert r.status == "error" and "Grant it again" in r.content
    stop = Context("c1", None, threading.Event(), work)
    stop.abort.set()
    assert reg.call("run_shell", {"command": "true"}, stop).status == "cancelled"
