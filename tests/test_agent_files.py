"""The file tools: only inside the granted folder, no symlink escape, no hidden or secret files, text only, caps.

Every file here is a temp file; the "secrets" are dummy text."""

import os
import threading
from pathlib import Path

import pytest

from app.agent import Context, Registry, Settings, build_registry, file_tools


@pytest.fixture
def reg() -> Registry:
    return build_registry(None, Settings())


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "granted"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "todo.md").write_text("# Todo\nbuy milk\nCall Ada\n")
    (root / "readme.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" * 10)
    (root / ".env").write_text("TOKEN=dummy-milk\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("milk\n")
    (root / "server.pem").write_text("dummy milk\n")
    (root / "id_ed25519").write_text("dummy milk\n")
    (root / "login.keychain-db").write_text("dummy milk\n")
    # outside the folder
    (tmp_path / "private.txt").write_text("milk outside\n")
    return root


@pytest.fixture
def ctx(folder: Path, tmp_path: Path) -> Context:
    return Context("c1", folder, threading.Event(), tmp_path / "ws")


def call(reg: Registry, ctx: Context, name: str, **args: object) -> tuple[str, str]:
    r = reg.call(name, args, ctx)
    return r.status, r.content


def test_list_files_hides_hidden_and_secret_files(reg: Registry, ctx: Context) -> None:
    status, text = call(reg, ctx, "list_files")
    assert status == "ok"
    assert text.splitlines() == [
        "image.png  (160 B)",
        "notes/",
        "readme.txt  (8.7 KB)",
    ]
    _, deep = call(reg, ctx, "list_files", recursive=True)
    assert "notes/todo.md  (25 B)" in deep
    for secret in (".env", ".git", "server.pem", "id_ed25519", "keychain"):
        assert secret not in deep


def test_read_file_by_lines(reg: Registry, ctx: Context) -> None:
    status, text = call(reg, ctx, "read_file", path="readme.txt", max_lines=3)
    assert status == "ok"
    assert text.splitlines() == [
        "readme.txt (8.7 KB, 1000 lines), lines 1-3; read on with start_line=4:",
        "line 1",
        "line 2",
        "line 3",
    ]
    _, end = call(reg, ctx, "read_file", path="readme.txt", start_line=999)
    assert end.splitlines()[1:] == ["line 999", "line 1000"]
    _, nested = call(reg, ctx, "read_file", path="notes/../notes/todo.md")
    assert "Call Ada" in nested


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../private.txt", "outside the granted folder"),
        ("/etc/hosts", "outside the granted folder"),
        ("~/.zshrc", "outside the granted folder"),
        (".env", "hidden or secret"),
        (".git/config", "hidden or secret"),
        ("server.pem", "hidden or secret"),
        ("id_ed25519", "hidden or secret"),
        ("login.keychain-db", "hidden or secret"),
        ("image.png", "not a text file"),
        ("notes", "a folder"),
        ("missing.txt", "no such file"),
    ],
)
def test_read_file_refusals(
    reg: Registry, ctx: Context, path: str, message: str
) -> None:
    status, text = call(reg, ctx, "read_file", path=path)
    assert status == "error"
    assert message in text
    assert "milk" not in text


def test_absolute_path_inside_the_folder_is_fine(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    status, text = call(reg, ctx, "read_file", path=str(folder / "notes" / "todo.md"))
    assert status == "ok" and "buy milk" in text


def test_symlinks_cannot_leave_the_folder(
    reg: Registry, ctx: Context, folder: Path, tmp_path: Path
) -> None:
    (folder / "escape.txt").symlink_to(tmp_path / "private.txt")
    (folder / "outdir").symlink_to(tmp_path)
    (folder / "env_link").symlink_to(folder / ".env")
    (folder / "inside_link.md").symlink_to(folder / "notes" / "todo.md")
    for path in ("escape.txt", "outdir/private.txt", "env_link"):
        status, text = call(reg, ctx, "read_file", path=path)
        assert status == "error" and "milk" not in text, path
    status, text = call(reg, ctx, "read_file", path="inside_link.md")
    assert status == "ok" and "buy milk" in text
    _, listing = call(reg, ctx, "list_files", recursive=True)
    assert "escape.txt" not in listing and "outdir" not in listing
    assert "env_link" not in listing and "inside_link.md" in listing
    _, found = call(reg, ctx, "search_files", query="milk")
    assert "outside" not in found and "dummy" not in found


def test_a_swapped_path_is_checked_again_after_open(
    folder: Path, tmp_path: Path
) -> None:
    """read_text checks the path of the open file, so a symlink that appeared after resolve() is refused."""
    link = folder / "late.txt"
    link.symlink_to(tmp_path / "private.txt")
    root = os.path.realpath(folder)
    with pytest.raises(file_tools.ToolError):
        file_tools.read_text(str(link), root)  # O_NOFOLLOW refuses the link itself
    # a hard path to a file outside, as if a parent folder had been swapped for a link
    with pytest.raises(file_tools.ToolError, match="outside"):
        file_tools.read_text(str(tmp_path / "private.txt"), root)


def test_search_files_finds_lines_and_names(reg: Registry, ctx: Context) -> None:
    status, text = call(reg, ctx, "search_files", query="MILK")
    assert status == "ok"
    assert text.splitlines() == ["notes/todo.md:2: buy milk"]
    _, names = call(reg, ctx, "search_files", query="todo")
    assert "notes/todo.md  (file name)" in names
    _, globbed = call(reg, ctx, "search_files", query="line 7", glob="*.md")
    assert globbed.startswith("No match")
    _, capped = call(reg, ctx, "search_files", query="line", max_results=5)
    assert len(capped.splitlines()) == 6 and capped.endswith("[Stopped at 5 matches.]")


def test_no_folder_means_refused(reg: Registry, tmp_path: Path) -> None:
    ctx = Context("c1", None, threading.Event(), tmp_path)
    status, text = call(reg, ctx, "list_files")
    assert status == "error" and "No folder" in text


def test_big_files_are_read_up_to_the_cap(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_tools, "MAX_READ", 100)
    (folder / "big.txt").write_text("abcdefghi\n" * 50)
    status, text = call(reg, ctx, "read_file", path="big.txt")
    assert status == "ok"
    assert "10 lines in the first 100 B" in text.splitlines()[0]


def test_list_stops_at_the_entry_cap(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_tools, "MAX_ENTRIES", 2)
    _, text = call(reg, ctx, "list_files")
    assert text.splitlines()[-1] == "[Stopped at 2 entries: list a subfolder.]"


@pytest.mark.parametrize(
    ("sample", "text"),
    [
        (b"plain ascii\n", True),
        ("Türkçe metin ğş".encode(), True),
        ("café".encode("latin-1") * 20, True),
        (b"abc\x00def", False),
        (bytes(range(1, 9)) * 100, False),
    ],
)
def test_looks_text(sample: bytes, text: bool) -> None:
    assert file_tools.looks_text(sample) is text


@pytest.mark.parametrize(
    "name",
    [".env", ".ssh", ".aws", "id_rsa", "id_ed25519.pub", "Cert.PEM", "x.key", "a.p12"]
    + ["Keychains", "login.keychain-db", "vault.kdbx", "credentials", "secrets.toml"]
    + [
        "AuthKey_ABC.p8",
        "key.ppk",
        "private.asc",
        "keystore",
        "UTC--2026-x",
        "a.tfstate",
    ],
)
def test_blocked_names(name: str) -> None:
    assert file_tools.blocked(name)


@pytest.mark.parametrize("name", ["notes.md", "id_list.csv", "keyboard.txt", "env"])
def test_ordinary_names(name: str) -> None:
    assert not file_tools.blocked(name)


def test_the_search_time_cap_counts_skipped_files(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_tools, "SEARCH_SECONDS", -1.0)
    _, text = call(reg, ctx, "search_files", query="x", glob="*.none")
    assert "Stopped after 0 files" in text


def test_a_folder_granted_in_another_case_still_works(
    reg: Registry, folder: Path, tmp_path: Path
) -> None:
    other = Path(str(folder).replace("granted", "GRANTED"))
    if not other.exists():
        pytest.skip("case-sensitive file system")
    ctx = Context("c1", other, threading.Event(), tmp_path / "ws")
    status, text = call(reg, ctx, "read_file", path="notes/todo.md")
    assert status == "ok" and "buy milk" in text
