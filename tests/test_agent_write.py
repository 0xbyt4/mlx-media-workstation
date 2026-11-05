"""The tools that write files: write_file, edit_file, copy_to_workspace and export_from_workspace (app/agent/write_tools.py,
app/agent/safe_fs.py), also of a media job's output in the Library. Only inside the granted folder and the chat's work folder, never through a link, never a hidden,
secret or executable file, atomic, with a backup before a change; and read_file's line ranges.

Every file here is a temp file; the "secrets" are dummy text."""

import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import (
    Context,
    Limits,
    Registry,
    Settings,
    build_registry,
    file_tools,
    safe_fs,
    write_tools,
)
from app.agent.registry import visible_lines
from tests.fake_workstation import FakeWorkstation


@pytest.fixture
def reg() -> Registry:
    return build_registry(None, Settings())


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "granted"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "todo.md").write_text("# Todo\nbuy milk\nCall Ada\n")
    (root / ".env").write_text("TOKEN=dummy\n")
    (root / "server.pem").write_text("dummy\n")
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    (tmp_path / "private.txt").write_text("outside\n")
    # the stored folder is the disk's spelling (/private/var/...), as the server stores it
    fd = os.open(root, os.O_RDONLY)
    try:
        return Path(file_tools.fd_path(fd))
    finally:
        os.close(fd)


@pytest.fixture
def ctx(folder: Path, tmp_path: Path) -> Context:
    (tmp_path / "chat").mkdir()
    return Context("c1", folder, threading.Event(), tmp_path / "chat" / "ws")


def call(reg: Registry, ctx: Context, name: str, **args: object):
    return reg.call(name, args, ctx)


def leftovers(path: Path) -> list[str]:
    """Temp files a write left behind."""
    return [p.name for p in path.rglob(".mmw-*")]


# ---------- write_file ----------


def test_write_file_creates_a_new_text_file(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    r = call(reg, ctx, "write_file", path="notes/plan.md", content="# Plan\nstep one\n")
    assert r.status == "ok", r.content
    assert r.content == "Wrote notes/plan.md (16 B, 2 lines)."
    assert r.saved == ["notes/plan.md"]
    target = folder / "notes" / "plan.md"
    assert target.read_text() == "# Plan\nstep one\n"
    assert not target.stat().st_mode & 0o111  # never executable
    assert not leftovers(folder)


def test_write_file_never_overwrites(reg: Registry, ctx: Context, folder: Path) -> None:
    r = call(reg, ctx, "write_file", path="notes/todo.md", content="gone")
    assert r.status == "error" and "edit_file" in r.content
    assert (folder / "notes" / "todo.md").read_text().startswith("# Todo")
    # a link at the name counts as a file: it is neither followed nor replaced
    (folder / "link.txt").symlink_to(folder.parent / "private.txt")
    r = call(reg, ctx, "write_file", path="link.txt", content="x")
    assert r.status == "error" and "exists" in r.content
    assert (folder.parent / "private.txt").read_text() == "outside\n"


def test_the_link_step_refuses_a_name_taken_after_the_check(folder: Path) -> None:
    """write_new links the temp file in: a file that appeared after new_target() is not overwritten."""
    (folder / "late.txt").write_text("first")
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(file_tools.ToolError, match="exists"):
            safe_fs.write_new(fd, "late.txt", safe_fs.write_bytes(b"second"))
    finally:
        os.close(fd)
    assert (folder / "late.txt").read_text() == "first"
    assert not leftovers(folder)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../escape.txt", "outside the granted folder"),
        ("/etc/escape.txt", "outside the granted folder"),
        (".env.local", "hidden or secret"),
        (".git/config", "hidden or secret"),
        ("notes/.hidden.md", "hidden or secret"),
        ("server.key", "hidden or secret"),
        ("id_rsa", "hidden or secret"),
        ("run.sh", "can run code"),
        ("Run.COMMAND", "can run code"),
        ("tool.py", "can run code"),
        ("x.zsh", "can run code"),
        ("x.bash", "can run code"),
        ("Info.plist", "can run code"),
        ("lib.dylib", "can run code"),
        ("lib.so", "can run code"),
        ("setup.exe", "can run code"),
        ("Thing.app", "can run code"),
        ("Thing.app/Contents/x.txt", "cannot write inside Thing.app"),
        ("evil.pth", "can run code"),
        ("page.js", "can run code"),
        ("x.hta", "can run code"),
        ("x.reg", "can run code"),
        ("x.osascript", "can run code"),
        ("lib/site-packages/notes.txt", "cannot write inside site-packages"),
        ("Dist-Packages/notes.txt", "cannot write inside Dist-Packages"),
        ("missing/x.txt", "no such file or folder"),
        ("notes/", "give a file name"),
        ("bad\nname.txt", "control character"),
        ("trailing.", "cannot end with"),
    ],
)
def test_write_file_refusals(
    reg: Registry, ctx: Context, folder: Path, path: str, message: str
) -> None:
    r = call(reg, ctx, "write_file", path=path, content="x")
    assert r.status == "error"
    assert message in r.content, r.content
    assert not leftovers(folder)


def test_no_write_when_the_granted_folder_is_code(
    reg: Registry, tmp_path: Path
) -> None:
    """A folder granted before the app refused such grants: inside site-packages (Python loads code from there) the
    write tools refuse to write."""
    root = tmp_path / "venv" / "lib" / "site-packages" / "pkg"
    root.mkdir(parents=True)
    (root / "data.txt").write_text("old\n")
    fd = os.open(root, os.O_RDONLY)
    try:
        real = Path(file_tools.fd_path(fd))
    finally:
        os.close(fd)
    (tmp_path / "chat").mkdir()
    ctx = Context("c1", real, threading.Event(), tmp_path / "chat" / "ws")
    r = call(reg, ctx, "write_file", path="new.txt", content="x")
    assert r.status == "error" and "inside site-packages" in r.content
    r = call(reg, ctx, "edit_file", path="data.txt", old_text="old", new_text="new")
    assert r.status == "error" and "inside site-packages" in r.content
    assert sorted(p.name for p in root.iterdir()) == ["data.txt"]


def test_write_file_refuses_a_path_through_a_link(
    reg: Registry, ctx: Context, folder: Path, tmp_path: Path
) -> None:
    (folder / "out").symlink_to(tmp_path)
    (folder / "inner").symlink_to(folder / "notes")
    r = call(reg, ctx, "write_file", path="out/new.txt", content="x")
    assert r.status == "error" and "outside" in r.content
    r = call(reg, ctx, "write_file", path="inner/new.txt", content="x")
    assert r.status == "error" and "through a link" in r.content
    assert (
        not (tmp_path / "new.txt").exists()
        and not (folder / "notes" / "new.txt").exists()
    )


def test_write_file_text_rules(reg: Registry, ctx: Context, folder: Path) -> None:
    big = "x" * (safe_fs.MAX_TEXT + 1)
    r = call(reg, ctx, "write_file", path="big.txt", content=big)
    assert r.status == "error" and "at most 1048576 characters" in r.content
    # four bytes a character in UTF-8: fewer characters than the schema allows, too many bytes
    r = call(reg, ctx, "write_file", path="wide.txt", content="\U0001f600" * 300_000)
    assert r.status == "error" and "larger than" in r.content
    r = call(reg, ctx, "write_file", path="nul.txt", content="a\x00b")
    assert r.status == "error" and "NUL" in r.content
    r = call(reg, ctx, "write_file", path="surrogate.txt", content="a\ud800b")
    assert r.status == "error" and "not valid UTF-8" in r.content
    assert sorted(p.name for p in folder.iterdir()) == [
        ".env", "notes", "photo.png", "server.pem"
    ]  # fmt: skip


def test_a_failed_write_leaves_nothing(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(safe_fs, "write_bytes", lambda data: broken)
    r = call(reg, ctx, "write_file", path="new.txt", content="x")
    assert r.status == "error" and "No space left" in r.content
    assert not (folder / "new.txt").exists() and not leftovers(folder)


def test_write_tools_need_a_folder(reg: Registry, tmp_path: Path) -> None:
    ctx = Context("c1", None, threading.Event(), tmp_path / "ws")
    for name, args in (
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("edit_file", {"path": "a.txt", "content": "x"}),
        ("copy_to_workspace", {"path": "a.txt"}),
        ("export_from_workspace", {"path": "a.txt"}),
    ):
        r = reg.call(name, args, ctx)
        assert r.status == "error" and "No folder" in r.content, name


# ---------- edit_file ----------


def test_edit_file_replaces_once_and_keeps_a_backup(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    todo = folder / "notes" / "todo.md"
    os.chmod(todo, 0o600)
    inode = todo.stat().st_ino
    r = call(
        reg,
        ctx,
        "edit_file",
        path="notes/todo.md",
        old_text="buy milk",
        new_text="buy oat milk",
    )
    assert r.status == "ok", r.content
    assert todo.read_text() == "# Todo\nbuy oat milk\nCall Ada\n"
    assert stat.S_IMODE(todo.stat().st_mode) == 0o600  # the mode stays
    assert todo.stat().st_ino != inode  # a new file renamed over the old one: atomic
    assert r.content.startswith(
        "Changed notes/todo.md: 1 replacement, 1 line removed, 1 added."
    )
    (copy,) = r.files
    assert copy.startswith("backups/") and copy.endswith("-todo.md")
    assert copy in r.content
    assert (ctx.work_dir / copy).read_text() == "# Todo\nbuy milk\nCall Ada\n"
    assert r.saved == ["notes/todo.md"]
    assert not leftovers(folder)


def test_edit_file_match_rules(reg: Registry, ctx: Context, folder: Path) -> None:
    f = folder / "list.txt"
    f.write_text("a = 1\nb = 1\nc = 2\n")
    r = call(reg, ctx, "edit_file", path="list.txt", old_text="= 1", new_text="= 3")
    assert r.status == "error" and "found 2 times" in r.content
    r = call(reg, ctx, "edit_file", path="list.txt", old_text="= 9", new_text="= 3")
    assert r.status == "error" and "not found" in r.content
    r = call(reg, ctx, "edit_file", path="list.txt", old_text="", new_text="x")
    assert r.status == "error" and "must not be empty" in r.content
    r = call(reg, ctx, "edit_file", path="list.txt", old_text="= 2", new_text="= 2")
    assert r.status == "error" and "leaves the file as it is" in r.content
    r = call(
        reg, ctx, "edit_file", path="list.txt", old_text="a", new_text="b", content="x"
    )
    assert r.status == "error" and "not both" in r.content
    r = call(reg, ctx, "edit_file", path="list.txt", old_text="a")
    assert r.status == "error" and "old_text and new_text" in r.content
    assert f.read_text() == "a = 1\nb = 1\nc = 2\n"  # nothing changed so far
    assert not (ctx.work_dir / "backups").exists()
    r = call(
        reg,
        ctx,
        "edit_file",
        path="list.txt",
        old_text="= 1",
        new_text="= 3",
        replace_all=True,
    )
    assert r.status == "ok" and "2 replacements" in r.content
    assert f.read_text() == "a = 3\nb = 3\nc = 2\n"
    r = call(reg, ctx, "edit_file", path="list.txt", content="new\n")
    assert r.status == "ok" and "replaced the whole file" in r.content
    assert f.read_text() == "new\n"
    assert len(list((ctx.work_dir / "backups").iterdir())) == 2


def test_edit_file_keeps_windows_line_ends(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    f = folder / "dos.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    r = call(
        reg, ctx, "edit_file", path="dos.txt", old_text="one\ntwo", new_text="1\n2"
    )
    assert r.status == "ok", r.content
    assert f.read_bytes() == b"1\r\n2\r\nthree\r\n"


@pytest.mark.parametrize(
    ("setup", "path", "message"),
    [
        (lambda f: None, ".env", "hidden or secret"),
        (lambda f: None, "server.pem", "hidden or secret"),
        (lambda f: None, "../private.txt", "outside the granted folder"),
        (lambda f: None, "notes", "a folder"),
        (lambda f: None, "missing.txt", "no such file"),
        (lambda f: (f / "run.sh").write_text("echo hi\n"), "run.sh", "can run code"),
        (
            lambda f: (f / "latin.txt").write_bytes(b"caf\xe9\n"),
            "latin.txt",
            "not UTF-8",
        ),
        (
            lambda f: (f / "bin.txt").write_bytes(b"a\x00b"),
            "bin.txt",
            "not a text file",
        ),
        (
            lambda f: (f / "big.txt").write_text("x" * (safe_fs.MAX_TEXT + 1)),
            "big.txt",
            "smaller files only",
        ),
        (
            lambda f: (
                (f / "exec.txt").write_text("x\n"),
                os.chmod(f / "exec.txt", 0o755),
            ),
            "exec.txt",
            "executable",
        ),
        (
            lambda f: (
                (f / "a.txt").write_text("x\n"),
                os.link(f / "a.txt", f / "b.txt"),
            ),
            "b.txt",
            "hard links",
        ),
        (
            lambda f: (f / "via.md").symlink_to(f / "notes" / "todo.md"),
            "via.md",
            "through a link",
        ),
        (
            lambda f: (f / "out.txt").symlink_to(f.parent / "private.txt"),
            "out.txt",
            "outside the granted folder",
        ),
    ],
)
def test_edit_file_refusals(
    reg: Registry, ctx: Context, folder: Path, setup, path: str, message: str
) -> None:
    setup(folder)
    r = call(reg, ctx, "edit_file", path=path, content="changed\n")
    assert r.status == "error"
    assert message in r.content, r.content
    assert "changed" not in (folder.parent / "private.txt").read_text()
    assert (folder / "notes" / "todo.md").read_text().startswith("# Todo")
    assert not (ctx.work_dir / "backups").exists()


def test_edit_file_runs_only_while_the_approved_diff_holds(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    """The approval card showed a diff (ctx.preview); a file that changed after that is not written."""
    tool = reg.get("edit_file")
    args = {"path": "notes/todo.md", "old_text": "Call Ada", "new_text": "Call Grace"}
    shown = tool.show(args, ctx)
    assert shown is not None
    assert shown.splitlines()[:3] == [
        "--- a/notes/todo.md",
        "+++ b/notes/todo.md",
        "@@ -1,3 +1,3 @@",
    ]
    assert "-Call Ada" in shown and "+Call Grace" in shown
    (folder / "notes" / "todo.md").write_text("# Todo\nbuy bread\nCall Ada\n")
    ctx.preview = shown
    r = reg.call("edit_file", args, ctx)
    assert r.status == "error" and "changed after the user saw" in r.content
    assert (folder / "notes" / "todo.md").read_text() == "# Todo\nbuy bread\nCall Ada\n"
    ctx.preview = tool.show(args, ctx)
    assert reg.call("edit_file", args, ctx).status == "ok"
    assert (
        folder / "notes" / "todo.md"
    ).read_text() == "# Todo\nbuy bread\nCall Grace\n"


def test_the_approved_diff_is_compared_before_the_escaping(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    """The card writes a zero-width space as the text \\u200b. A file that has that text now, where it had the
    character when the user looked, gives the same card but another change: it is not written."""
    tool = reg.get("edit_file")
    path = folder / "zw.txt"
    path.write_text("Call Ada\nx\u200by\n")
    args = {"path": "zw.txt", "old_text": "Call Ada", "new_text": "Call Grace"}
    ctx.preview = tool.show(args, ctx)
    path.write_text("Call Ada\nx\\u200by\n")
    assert (
        tool.show(args, Context("c1", folder, ctx.abort, ctx.work_dir)) == ctx.preview
    )
    r = reg.call("edit_file", args, ctx)
    assert r.status == "error" and "changed after the user saw" in r.content
    assert path.read_text() == "Call Ada\nx\\u200by\n"


def test_an_edit_too_large_for_the_card_is_refused(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card shows at most MAX_DIFF_LINES lines of a diff: edit_file refuses a larger change, so the user never
    approves a part they did not see."""
    monkeypatch.setattr(write_tools, "MAX_DIFF_LINES", 10)
    tool = reg.get("edit_file")
    (folder / "long.txt").write_text("a\n" * 19 + "end\n")
    args = {"path": "long.txt", "content": "b\n" * 20}
    with pytest.raises(file_tools.ToolError, match="too large to show"):
        tool.show(args, ctx)
    r = reg.call("edit_file", args, ctx)
    assert r.status == "error" and "Split it into smaller" in r.content
    assert (folder / "long.txt").read_text() == "a\n" * 19 + "end\n"
    small = {"path": "long.txt", "old_text": "end", "new_text": "END"}
    ctx.preview = tool.show(small, ctx)
    assert reg.call("edit_file", small, ctx).status == "ok"


def test_the_preview_escapes_hidden_characters_and_is_cut(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = reg.get("write_file")
    shown = tool.show({"path": "new.txt", "content": "a\u202eb\tc\n"}, ctx)
    assert (
        shown.splitlines()[-1] == "+a\\u202eb\tc"
    )  # the bidi override is visible, the tab stays
    monkeypatch.setattr(write_tools, "MAX_DIFF_LINES", 5)
    long = tool.show({"path": "new.txt", "content": "x\n" * 50}, ctx)
    assert long.splitlines()[-1] == "[The diff goes on: 48 more lines are not shown.]"
    with pytest.raises(file_tools.ToolError, match="exists"):
        tool.show({"path": "notes/todo.md", "content": "x"}, ctx)
    with pytest.raises(file_tools.ToolError, match="Invalid arguments"):
        tool.show({"path": "x.txt"}, ctx)
    assert visible_lines("a\x07\tb") == "a\\u0007\tb"


def test_a_file_swapped_during_the_edit_is_not_overwritten(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename happens only while the file is still the one that was read."""
    todo = folder / "notes" / "todo.md"
    real_backup = write_tools.backup

    def backup_then_swap(c, name, data):
        out = real_backup(c, name, data)
        todo.unlink()
        todo.write_text("someone else's text\n")
        return out

    monkeypatch.setattr(write_tools, "backup", backup_then_swap)
    r = call(
        reg, ctx, "edit_file", path="notes/todo.md", old_text="milk", new_text="tea"
    )
    assert r.status == "error" and "changed while" in r.content
    assert todo.read_text() == "someone else's text\n"
    assert not leftovers(folder)


# ---------- copy_to_workspace ----------


def test_copy_to_workspace_copies_any_type(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    r = call(reg, ctx, "copy_to_workspace", path="photo.png")
    assert r.status == "ok", r.content
    assert (ctx.work_dir / "photo.png").read_bytes() == (
        folder / "photo.png"
    ).read_bytes()
    assert r.files == ["photo.png"] and "as photo.png" in r.content
    r = call(reg, ctx, "copy_to_workspace", path="photo.png")
    assert r.status == "error" and "Choose another name" in r.content
    r = call(reg, ctx, "copy_to_workspace", path="photo.png", to="in/p.png")
    assert r.status == "ok" and (ctx.work_dir / "in" / "p.png").exists()
    assert not leftovers(ctx.work_dir)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"path": ".env"}, "hidden or secret"),
        ({"path": "server.pem"}, "hidden or secret"),
        ({"path": "../private.txt"}, "outside the granted folder"),
        ({"path": "notes"}, "a folder"),
        ({"path": "photo.png", "to": "../up.png"}, "without '..'"),
        ({"path": "photo.png", "to": ".hidden.png"}, "hidden or secret"),
    ],
)
def test_copy_to_workspace_refusals(
    reg: Registry, ctx: Context, args: dict, message: str
) -> None:
    r = reg.call("copy_to_workspace", args, ctx)
    assert r.status == "error" and message in r.content, r.content


def test_copy_to_workspace_limits(
    ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (folder / "two.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    small = build_registry(None, Settings(), limits=Limits(disk_mb=1))
    r = small.call("copy_to_workspace", {"path": "two.bin"}, ctx)
    assert r.status == "error" and "does not fit" in r.content
    reg = build_registry(None, Settings())
    monkeypatch.setattr(write_tools, "MAX_COPY", 1024)
    r = reg.call("copy_to_workspace", {"path": "two.bin"}, ctx)
    assert r.status == "error" and "up to 1.0 KB" in r.content
    monkeypatch.setattr(write_tools, "MAX_COPY", 500 * 1024 * 1024)
    monkeypatch.setattr(
        write_tools.shutil, "disk_usage", lambda p: SimpleNamespace(free=100)
    )
    r = reg.call("copy_to_workspace", {"path": "two.bin"}, ctx)
    assert r.status == "error" and "Not enough free disk space" in r.content
    assert not (ctx.work_dir / "two.bin").exists()


def test_copy_to_workspace_does_not_follow_links_in_the_work_folder(
    reg: Registry, ctx: Context, tmp_path: Path
) -> None:
    ctx.work_dir.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (ctx.work_dir / "sub").symlink_to(tmp_path / "elsewhere")
    r = call(reg, ctx, "copy_to_workspace", path="photo.png", to="sub/p.png")
    assert r.status == "error" and "cannot open its folder" in r.content
    assert not (tmp_path / "elsewhere" / "p.png").exists()
    # the work folder itself replaced by a link
    ctx.work_dir.rename(tmp_path / "ws-old")
    ctx.work_dir.symlink_to(tmp_path / "elsewhere")
    r = call(reg, ctx, "copy_to_workspace", path="photo.png")
    assert r.status == "error" and "is a link" in r.content
    assert not (tmp_path / "elsewhere" / "photo.png").exists()


# ---------- export_from_workspace ----------


def test_export_copies_media_and_text(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    ctx.work_dir.mkdir()
    (ctx.work_dir / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01" * 1000)
    (ctx.work_dir / "out").mkdir()
    (ctx.work_dir / "out" / "table.csv").write_bytes(
        b"a;b\n\xe9;1\n"
    )  # Latin-1 is fine in a CSV
    (ctx.work_dir / "summary.md").write_text("# Summary\n")
    r = call(reg, ctx, "export_from_workspace", path="plot.png")
    assert r.status == "ok", r.content
    assert (folder / "plot.png").read_bytes() == (
        ctx.work_dir / "plot.png"
    ).read_bytes()
    assert r.saved == ["plot.png"]
    r = call(reg, ctx, "export_from_workspace", path="out/table.csv", to="notes/t.csv")
    assert (
        r.status == "ok"
        and (folder / "notes" / "t.csv").read_bytes() == b"a;b\n\xe9;1\n"
    )
    r = call(reg, ctx, "export_from_workspace", path="summary.md")
    assert r.status == "ok" and (folder / "summary.md").read_text() == "# Summary\n"
    r = call(reg, ctx, "export_from_workspace", path="summary.md")
    assert r.status == "error" and "exists" in r.content
    assert not leftovers(folder)


@pytest.mark.parametrize(
    ("make", "args", "message"),
    [
        (lambda w: (w / "a.sh").write_text("x"), {"path": "a.sh"}, "can run code"),
        (
            lambda w: (w / "a.txt").write_text("x"),
            {"path": "a.txt", "to": "a.py"},
            "can run code",
        ),
        (
            lambda w: (w / "a.txt").write_text("x"),
            {"path": "a.txt", "to": ".a"},
            "hidden or secret",
        ),
        (
            lambda w: (w / "a.txt").write_text("x"),
            {"path": "a.txt", "to": "../a.txt"},
            "outside",
        ),
        (
            lambda w: ((w / "x.txt").write_text("x"), os.chmod(w / "x.txt", 0o700)),
            {"path": "x.txt"},
            "executable",
        ),
        (
            lambda w: (w / "l.txt").write_bytes(b"caf\xe9"),
            {"path": "l.txt"},
            "not UTF-8",
        ),
        (
            lambda w: (w / "b.bin").write_bytes(b"\x00\x01"),
            {"path": "b.bin"},
            "not a text file",
        ),
        (
            lambda w: (w / "big.txt").write_text("x" * (safe_fs.MAX_TEXT + 1)),
            {"path": "big.txt"},
            "larger than 1024 KB",
        ),
        (
            lambda w: (w / ".h.txt").write_text("x"),
            {"path": ".h.txt", "to": "h.txt"},
            "hidden",
        ),
        (lambda w: None, {"path": "../chat.json", "to": "c.txt"}, "without '..'"),
        (lambda w: None, {"path": "missing.txt"}, "no such file"),
        (
            lambda w: (w / "d").mkdir(),
            {"path": "d", "to": "d.txt"},
            "not a regular file",
        ),
    ],
)
def test_export_refusals(
    reg: Registry, ctx: Context, folder: Path, make, args: dict, message: str
) -> None:
    ctx.work_dir.mkdir()
    make(ctx.work_dir)
    before = sorted(p.name for p in folder.iterdir())
    r = reg.call("export_from_workspace", args, ctx)
    assert r.status == "error" and message in r.content, r.content
    assert sorted(p.name for p in folder.iterdir()) == before


def test_export_does_not_follow_a_link_in_the_work_folder(
    reg: Registry, ctx: Context, folder: Path, tmp_path: Path
) -> None:
    """The sandboxed code can link to any file; export would copy that file into the folder."""
    ctx.work_dir.mkdir()
    (ctx.work_dir / "leak.txt").symlink_to(tmp_path / "private.txt")
    (ctx.work_dir / "dir").symlink_to(tmp_path)
    for args in ({"path": "leak.txt"}, {"path": "dir/private.txt", "to": "p.txt"}):
        r = reg.call("export_from_workspace", args, ctx)
        assert r.status == "error", args
    assert not (folder / "leak.txt").exists() and not (folder / "p.txt").exists()


# ---------- export_from_workspace: a media job's output ----------

JOB = "20261103-110000-beef"  # the done job of the fake Workstation, with image.png
PICTURE = b"\x89PNG\r\n\x1a\n" + b"made by a job" * 10


@pytest.fixture
def lib(tmp_path: Path) -> tuple[Registry, FakeWorkstation]:
    """The tools with a fake Workstation whose done job has image.png on disk."""
    port = FakeWorkstation(tmp_path / "out")
    (tmp_path / "out" / JOB).mkdir(parents=True)
    (tmp_path / "out" / JOB / "image.png").write_bytes(PICTURE)
    return build_registry(port, Settings()), port


def test_export_saves_a_library_output_in_the_folder(
    lib: tuple[Registry, FakeWorkstation], ctx: Context, folder: Path
) -> None:
    reg, _ = lib
    r = call(reg, ctx, "export_from_workspace", path=f"library:{JOB}/image.png")
    assert r.status == "ok", r.content
    assert r.content == (
        f"Copied library:{JOB}/image.png from the Library to image.png (138 B)."
    )
    assert r.saved == ["image.png"] and (folder / "image.png").read_bytes() == PICTURE
    r = call(
        reg,
        ctx,
        "export_from_workspace",
        path=f" library:{JOB}/image.png ",
        to="notes/fox.png",
    )
    assert r.status == "ok" and (folder / "notes" / "fox.png").read_bytes() == PICTURE
    assert not leftovers(folder)
    # the approval card says where the file comes from and its name in the folder
    tool = reg.get("export_from_workspace")
    assert tool.describe({"path": f"library:{JOB}/image.png", "to": "a.png"}) == (
        f"Copies 'library:{JOB}/image.png' from the Library into the granted folder as 'a.png'."
    )
    assert tool.describe({"path": "work:out/plot.png"}) == (
        "Copies 'out/plot.png' from the work folder into the granted folder as 'plot.png'."
    )


def test_export_takes_a_work_reference(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    ctx.work_dir.mkdir()
    (ctx.work_dir / "plot.png").write_bytes(PICTURE)
    r = call(reg, ctx, "export_from_workspace", path="work:plot.png")
    assert r.status == "ok", r.content
    assert r.content.startswith("Copied plot.png from the work folder to plot.png")
    assert (folder / "plot.png").read_bytes() == PICTURE


@pytest.mark.parametrize(
    ("args", "message"),
    [
        # no overwrite: the same rule as write_file
        ({"path": f"library:{JOB}/image.png", "to": "photo.png"}, "the file exists"),
        # no path out of the folder, no hidden or executable name
        ({"path": f"library:{JOB}/image.png", "to": "../x.png"}, "outside"),
        ({"path": f"library:{JOB}/image.png", "to": "/tmp/x.png"}, "outside"),
        ({"path": f"library:{JOB}/image.png", "to": ".x.png"}, "hidden or secret"),
        ({"path": f"library:{JOB}/image.png", "to": "x.command"}, "can run code"),
        ({"path": f"library:{JOB}/image.png", "to": "nowhere/x.png"}, "no such file"),
        # an image saved under a text name must be UTF-8 text
        ({"path": f"library:{JOB}/image.png", "to": "x.txt"}, "not UTF-8 text"),
        # a reference that names no output of a done job
        ({"path": f"library:{JOB}/nope.png"}, "not an output of a finished job"),
        (
            {"path": "library:20261103-120000-abcd/image.png"},
            "not an output of a finished job",
        ),  # still running
        ({"path": f"library:{JOB}", "to": "x.png"}, "not an output of a finished job"),
        ({"path": "library:", "to": "x.png"}, "not an output of a finished job"),
    ],
)
def test_export_refuses_a_bad_library_save(
    lib: tuple[Registry, FakeWorkstation],
    ctx: Context,
    folder: Path,
    args: dict,
    message: str,
) -> None:
    reg, _ = lib
    before = sorted(p.name for p in folder.rglob("*"))
    r = reg.call("export_from_workspace", args, ctx)
    assert r.status == "error" and message in r.content, r.content
    assert sorted(p.name for p in folder.rglob("*")) == before


def test_export_of_a_library_output_does_not_write_through_a_link(
    lib: tuple[Registry, FakeWorkstation], ctx: Context, folder: Path, tmp_path: Path
) -> None:
    reg, _ = lib
    (tmp_path / "elsewhere").mkdir()
    (folder / "out").symlink_to(tmp_path / "elsewhere")
    (folder / "inner").symlink_to(folder / "notes")
    for to, message in (
        ("out/fox.png", "outside"),
        ("inner/fox.png", "through a link"),
    ):
        r = call(
            reg, ctx, "export_from_workspace", path=f"library:{JOB}/image.png", to=to
        )
        assert r.status == "error" and message in r.content, r.content
    assert not list((tmp_path / "elsewhere").iterdir())
    assert not (folder / "notes" / "fox.png").exists()


def test_export_does_not_copy_a_library_output_that_is_a_link(
    lib: tuple[Registry, FakeWorkstation], ctx: Context, folder: Path, tmp_path: Path
) -> None:
    """A link in place of an output (the server refuses it too, library.output_path) is never followed."""
    reg, port = lib
    (tmp_path / "out" / JOB / "link.png").symlink_to(tmp_path / "private.txt")
    port.outputs[(JOB, "image.png")] = tmp_path / "out" / JOB / "link.png"
    r = call(reg, ctx, "export_from_workspace", path=f"library:{JOB}/image.png")
    assert r.status == "error" and "cannot open" in r.content, r.content
    assert not (folder / "image.png").exists()


def test_export_without_a_workstation_has_no_library(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    r = call(reg, ctx, "export_from_workspace", path=f"library:{JOB}/image.png")
    assert r.status == "error" and "has no Library" in r.content
    assert not (folder / "image.png").exists()


# ---------- read_file ranges ----------


def test_read_file_end_line_and_parts(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    (folder / "log.txt").write_text("".join(f"row {i}\n" for i in range(1, 101)))
    r = call(reg, ctx, "read_file", path="log.txt", start_line=10, end_line=12)
    assert r.content.splitlines() == [
        "log.txt (692 B, 100 lines), lines 10-12; read on with start_line=13:",
        "row 10",
        "row 11",
        "row 12",
    ]
    r = call(reg, ctx, "read_file", path="log.txt", start_line=5, end_line=4)
    assert r.status == "error" and "end_line" in r.content
    # lines that do not fit in one result: it stops before the cap and says where to read on
    (folder / "wide.txt").write_text(
        "".join(f"{i:04d} " + "w" * 995 + "\n" for i in range(1, 51))
    )
    r = call(reg, ctx, "read_file", path="wide.txt")
    assert not r.truncated
    head = r.content.splitlines()[0]
    assert head.startswith(
        "wide.txt (48.9 KB, 50 lines), lines 1-11; read on with start_line=12"
    )
    nxt = call(reg, ctx, "read_file", path="wide.txt", start_line=12)
    assert nxt.content.splitlines()[1].startswith("0012 ")
    # one line longer than a result: its start, with a note
    (folder / "one.txt").write_text("y" * 30000)
    r = call(reg, ctx, "read_file", path="one.txt")
    assert not r.truncated and "[line cut at" in r.content
