"""The Seatbelt sandbox, for real: each test runs code under sandbox-exec and checks that the profile and the limits
hold. macOS only; every file outside the work dir is a temp file made by the test."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.agent import Context, Limits, Settings, build_registry
from app.agent import sandbox as sb

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(sb.SANDBOX_EXEC),
    reason="needs macOS sandbox-exec",
)

FAST = Limits(wall_s=10, cpu_s=10, memory_mb=512, file_mb=5, disk_mb=50)


@pytest.fixture(scope="module")
def box() -> sb.SeatbeltSandbox:
    return sb.SeatbeltSandbox()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A private file next to the work dir, not in it."""
    path = tmp_path / "private" / "secret.txt"
    path.parent.mkdir()
    path.write_text("dummy secret\n")
    return path


def run(
    box: sb.SeatbeltSandbox, work: Path, code: str, limits: Limits = FAST
) -> sb.RunResult:
    return box.run_python(code, work, limits, threading.Event())


def test_normal_code_runs_and_writes_in_the_work_dir(
    box: sb.SeatbeltSandbox, work: Path
) -> None:
    r = run(
        box,
        work,
        "import json, math, os\n"
        "print(math.sqrt(16), os.getcwd() == os.environ['HOME'])\n"
        "os.makedirs('out', exist_ok=True)\n"
        "open('out/data.json', 'w').write(json.dumps({'a': 1}))\n"
        "import sys; print('err', file=sys.stderr)",
    )
    assert r.exit_code == 0 and r.killed is None
    assert r.stdout == "4.0 True\n" and r.stderr == "err\n"
    assert (work / "out" / "data.json").read_text() == '{"a": 1}'
    assert r.files == ["out/data.json"]
    assert not (work / ".tmp").exists()
    # the work dir stays for the next run of the chat; an unchanged file is not listed again
    r2 = run(box, work, "print(open('out/data.json').read())")
    assert r2.stdout == '{"a": 1}\n' and r2.files == []


def test_cannot_read_outside_the_work_dir(
    box: sb.SeatbeltSandbox, work: Path, outside: Path
) -> None:
    code = (
        "import os\n"
        f"paths = [{str(outside)!r}, '/System/Volumes/Data' + {str(outside)!r}]\n"
        "for p in paths:\n"
        "    try:\n"
        "        print(open(p).read())\n"
        "    except PermissionError:\n"
        "        print('denied')\n"
        f"for d in [{str(Path.home())!r}, {str(outside.parent)!r}, '/Users']:\n"
        "    try:\n"
        "        os.listdir(d); print('listed', d)\n"
        "    except PermissionError:\n"
        "        print('denied')\n"
        "for p in ['/etc/passwd', '/Library/Preferences/com.apple.networkd.plist']:\n"
        "    try:\n"
        "        open(p).read(); print('read', p)\n"
        "    except (PermissionError, FileNotFoundError):\n"
        "        print('denied')\n"
    )
    r = run(box, work, code)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["denied"] * 7
    assert "dummy secret" not in r.stdout


def test_cannot_write_outside_the_work_dir(
    box: sb.SeatbeltSandbox, work: Path, outside: Path
) -> None:
    targets = [
        outside,
        outside.parent / "new.txt",
        work.parent / "beside.txt",
        Path("/tmp/mmw-sandbox-test.txt"),
    ]
    code = "\n".join(
        f"try:\n    open({str(p)!r}, 'w').write('x'); print('wrote')\n"
        "except PermissionError:\n    print('denied')"
        for p in targets
    )
    r = run(box, work, code)
    assert r.stdout.split() == ["denied"] * 4, r.stderr
    assert outside.read_text() == "dummy secret\n"
    assert not (outside.parent / "new.txt").exists()
    assert not (work.parent / "beside.txt").exists()
    assert not Path("/tmp/mmw-sandbox-test.txt").exists()


def test_no_network(box: sb.SeatbeltSandbox, work: Path) -> None:
    code = """
import socket
def attempt(f):
    try:
        f(); print('open')
    except OSError as e:
        print('blocked')
attempt(lambda: socket.create_connection(('1.1.1.1', 443), timeout=3))
attempt(lambda: socket.create_connection(('127.0.0.1', 7870), timeout=3))
attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('1.1.1.1', 53)))
attempt(lambda: socket.getaddrinfo('example.com', 443))
attempt(lambda: socket.socket(socket.AF_UNIX).connect('/var/run/mDNSResponder'))
attempt(lambda: socket.socket(socket.AF_INET6).connect(('::1', 7870)))
"""
    r = run(box, work, code)
    assert r.stdout.split() == ["blocked"] * 6, r.stderr


def test_no_processes(box: sb.SeatbeltSandbox, work: Path) -> None:
    code = """
import os, subprocess, sys
for argv in (['/bin/sh', '-c', 'echo hi'], ['/usr/bin/curl', '-s', 'https://example.com'], [sys.executable, '-c', '1']):
    try:
        subprocess.run(argv, capture_output=True); print('ran')
    except PermissionError:
        print('blocked')
try:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    print('forked')
except PermissionError:
    print('blocked')
try:
    os.system('echo hi > owned.txt'); print('system' if os.path.exists('owned.txt') else 'blocked')
except PermissionError:
    print('blocked')
"""
    r = run(box, work, code)
    assert r.stdout.split() == ["blocked"] * 5, r.stderr
    assert not (work / "owned.txt").exists()


def test_timeout_kills_it(box: sb.SeatbeltSandbox, work: Path) -> None:
    t0 = time.monotonic()
    r = run(box, work, "import time\nwhile True: time.sleep(0.01)", Limits(wall_s=1))
    assert r.killed == "timeout" and r.exit_code == -9
    assert time.monotonic() - t0 < 8


def test_cpu_limit_kills_a_busy_loop(box: sb.SeatbeltSandbox, work: Path) -> None:
    r = run(box, work, "while True: pass", Limits(wall_s=10, cpu_s=1))
    assert r.killed == "cpu" and r.seconds < 8


def test_memory_watchdog_kills_a_big_allocation(
    box: sb.SeatbeltSandbox, work: Path
) -> None:
    code = (
        "import time\nheld = []\n"
        "for _ in range(64):\n    held.append(b'x' * (32 * 2**20))\n"
        "print('allocated 2 GB')\ntime.sleep(10)"
    )
    r = run(box, work, code, Limits(wall_s=10, memory_mb=256))
    assert r.killed == "memory" and r.exit_code == -9
    assert "allocated" not in r.stdout
    assert r.seconds < 8


def test_file_size_limit(box: sb.SeatbeltSandbox, work: Path) -> None:
    code = (
        "try:\n    open('big.bin', 'wb').write(b'x' * (2 * 2**20)); print('wrote')\n"
        "except OSError as e:\n    print('refused', e.errno)"
    )
    r = run(box, work, code, Limits(file_mb=1))
    assert r.stdout.strip() == "refused 27"  # EFBIG


def test_disk_watchdog(box: sb.SeatbeltSandbox, work: Path) -> None:
    code = (
        "import time\n"
        "for i in range(40):\n    open(f'f{i}.bin', 'wb').write(b'x' * 2**20)\n"
        "time.sleep(10)"
    )
    r = run(box, work, code, Limits(wall_s=10, disk_mb=10))
    assert r.killed == "disk"


def test_abort_stops_it(box: sb.SeatbeltSandbox, work: Path) -> None:
    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    t0 = time.monotonic()
    r = box.run_python("import time\ntime.sleep(30)", work, FAST, abort)
    assert r.killed == "aborted"
    assert time.monotonic() - t0 < 5


def test_output_is_capped(box: sb.SeatbeltSandbox, work: Path) -> None:
    r = run(
        box,
        work,
        "print('a' * 100000)\nimport sys; sys.stderr.write('b' * 100000)",
        Limits(output_chars=1000),
    )
    assert len(r.stdout) == 1000 and r.stdout_truncated
    assert len(r.stderr) == 1000 and r.stderr_truncated


def test_environment_is_clean(
    box: sb.SeatbeltSandbox, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "dummy-token")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    r = run(
        box,
        work,
        "import os, sys\nprint(sorted(os.environ))\nprint(sys.flags.isolated, sys.flags.no_site)",
    )
    assert "HF_TOKEN" not in r.stdout and "PYTHONPATH" not in r.stdout
    assert r.stdout.splitlines()[1] == "1 1"


def test_profile_is_deny_by_default_and_names_no_home_path(
    box: sb.SeatbeltSandbox, work: Path
) -> None:
    text = sb.profile(box.interpreter(), "/private/var/w", [])
    assert "(deny default)" in text
    assert "network" not in text and "mach-lookup" not in text
    assert "process-fork" not in text
    assert str(Path.home()) not in text
    assert '(allow file-read* file-write* (subpath "/private/var/w"))' in text
    assert sb.profile(box.interpreter(), 'a"b', []).count('a\\"b') == 2


def test_run_python_tool(work: Path) -> None:
    reg = build_registry(None, Settings(), limits=FAST)
    ctx = Context("c1", None, threading.Event(), work)
    r = reg.call(
        "run_python", {"code": "open('plot.txt','w').write('1')\nprint(6*7)"}, ctx
    )
    assert r.status == "ok"
    assert r.content.startswith("exit code: 0 (")
    assert "stdout:\n42" in r.content and "files written: plot.txt" in r.content
    assert r.files == ["plot.txt"]
    r = reg.call("run_python", {"code": "raise SystemExit(3)"}, ctx)
    assert r.status == "ok" and r.content.startswith("exit code: 3")
    short = build_registry(None, Settings(), limits=Limits(wall_s=0.5))
    r = short.call("run_python", {"code": "while True: pass"}, ctx)
    assert r.status == "ok" and "stopped: it ran longer than 0.5 s" in r.content
    stop = Context("c1", None, threading.Event(), work)
    stop.abort.set()
    assert reg.call("run_python", {"code": "1"}, stop).status == "cancelled"


def test_a_configured_environment_runs_with_its_own_packages(tmp_path: Path) -> None:
    """[agent] python = a venv: it runs with -I, sees its own site-packages, and still cannot read outside."""
    env = tmp_path / "venv-sandbox"
    base = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    subprocess.run(
        [base, "-m", "venv", "--without-pip", str(env)], check=True, timeout=60
    )
    site = next((env / "lib").glob("python3*/site-packages"))
    (site / "sandbox_pkg.py").write_text("VALUE = 42\n")
    (tmp_path / "private.txt").write_text("dummy secret\n")
    box = sb.SeatbeltSandbox(env / "bin" / "python")
    assert box.unavailable() is None
    r = box.run_python(
        "import sys, sandbox_pkg\nprint(sandbox_pkg.VALUE, sys.flags.isolated, sys.flags.no_site)\n"
        f"try:\n    open({str(tmp_path / 'private.txt')!r}).read()\nexcept PermissionError:\n    print('denied')",
        tmp_path / "ws",
        FAST,
        threading.Event(),
    )
    assert r.stdout.split() == ["42", "1", "0", "denied"], r.stderr
    assert "does not exist" in (
        sb.SeatbeltSandbox(tmp_path / "missing" / "python").unavailable() or ""
    )


def test_the_code_cannot_swap_its_work_dir_for_a_link(
    box: sb.SeatbeltSandbox, work: Path, outside: Path
) -> None:
    """Removing the work dir and putting a symlink in its place must fail, and the host refuses a linked work dir."""
    code = f"""
import os, shutil
os.chdir("/")
for step in (lambda: shutil.rmtree({str(work)!r}), lambda: os.rename({str(work)!r}, {str(work)!r} + "2"),
             lambda: os.rmdir({str(work)!r}), lambda: os.symlink({str(outside.parent)!r}, {str(work)!r})):
    try:
        step(); print("done")
    except OSError:
        print("denied")
"""
    r = run(box, work, code)
    assert r.stdout.split()[-3:] == ["denied"] * 3, r.stderr
    assert work.is_dir() and not work.is_symlink()
    # the same attack done from outside the sandbox: the next run refuses the link
    for child in work.iterdir():
        if child.is_dir():
            os.rmdir(child)
        else:
            child.unlink()
    work.rmdir()
    work.symlink_to(outside.parent)
    with pytest.raises(ValueError, match="symlink"):
        run(box, work, "print(open('secret.txt').read())")


def test_the_tmp_link_is_not_followed_by_the_host(
    box: sb.SeatbeltSandbox, work: Path, outside: Path
) -> None:
    keep = outside.parent / "keep"
    keep.mkdir()
    (keep / "file.txt").write_text("x")
    work.mkdir()
    (work / ".tmp").symlink_to(keep)
    r = run(box, work, "print('ok')")
    assert r.stdout == "ok\n"
    assert (
        keep / "file.txt"
    ).exists()  # the host removed the link, not the folder it named


def test_system_facts_work_under_the_sysctl_allowlist(
    box: sb.SeatbeltSandbox, work: Path
) -> None:
    code = (
        "import ctypes, os, platform\n"
        "print(os.cpu_count() > 0, platform.machine(), bool(platform.platform()))\n"
        "import ctypes.util; print(ctypes.sizeof(ctypes.c_void_p))"
    )
    r = run(box, work, code)
    assert r.stdout.split() == ["True", "arm64", "True", "8"], r.stderr
