"""The host tools (app/agent/host_tools.py): spotlight_search, speak, convert_document, list_shortcuts and run_shortcut
run macOS programs outside the sandbox with fixed argument lists. Most tests replace the program run (run_program)
with a fake; a few run the real program where it is safe and gives the same result every time (say into a temp file,
textutil, mdfind on a temp folder, the shortcut list). No test runs a real shortcut or plays audio.

Every file here is a temp file; the "secrets" are dummy text."""

import os
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.agent import Context, Registry, file_tools, host_tools, providers
from app.agent.extensions import collect
from app.agent.host_tools import MDFIND, SAY, SHORTCUTS, TEXTUTIL, Ran
from app.agent.policy import Action, Grants, Risk, decide, taints
from app.agent.registry import Cancelled, ToolError

VOICES = (
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Eddy (English (US)) en_US    # Hello! My name is Eddy.\n"
    "Eddy (German (Germany)) de_DE    # Hallo! Ich heiße Eddy.\n"
    "Anna                de_DE    # Hallo! Ich heiße Anna.\n"
    "Yelda (Turkish (Türkiye)) tr_TR    # Merhaba, benim adım Yelda.\n"
    "Yelda (Turkish (Türkiye)) tr_TR    # Merhaba, benim adım Yelda.\n"
)
SHORTCUT_LIST = (
    "Dosyayı Kaydet (7D08FF71-A37C-49F9-9372-11397EDF981A)\n"
    "Make GIF (5ACAB88F-A7F9-4732-89FB-41A09F9EE79D)\n"
    "What’s a shortcut? (9BE81FF3-09B0-42DA-8434-7CB982F93530)\n"
)


@pytest.fixture(autouse=True)
def no_voice_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_tools, "voice_cache", None)


@pytest.fixture
def reg() -> Registry:
    return Registry(host_tools.build(lambda program: True))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "granted"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "invoice.txt").write_text("invoice 42\n")
    (root / "report.html").write_text("<p>Rapor: ağaç, şeker, ılık</p>")
    (root / ".env").write_text("TOKEN=dummy\n")
    (root / "server.pem").write_text("dummy\n")
    (tmp_path / "private.txt").write_text("outside\n")
    os.symlink(tmp_path / "private.txt", root / "link.txt")
    fd = os.open(root, os.O_RDONLY)
    try:
        return Path(file_tools.fd_path(fd))
    finally:
        os.close(fd)


@pytest.fixture
def ctx(folder: Path, tmp_path: Path) -> Context:
    (tmp_path / "chat").mkdir()
    return Context("c1", folder, threading.Event(), tmp_path / "chat" / "ws")


class FakeRun:
    """Stands in for run_program: records each argument list and answers with handler(argv)."""

    def __init__(self, handler: Callable[[list[str]], Ran]) -> None:
        self.handler = handler
        self.calls: list[list[str]] = []
        self.seconds: list[float] = []

    def __call__(self, argv: list[str], ctx: Context, seconds: float, what: str) -> Ran:
        self.calls.append(list(argv))
        self.seconds.append(seconds)
        return self.handler(list(argv))


def fake(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[list[str]], Ran]
) -> FakeRun:
    run = FakeRun(handler)
    monkeypatch.setattr(host_tools, "run_program", run)
    return run


def ok(stdout: bytes = b"") -> Ran:
    return Ran(0, stdout, False, "")


# ---------- the provider ----------


def test_the_provider_gives_the_host_tools_all_deferred_and_no_section() -> None:
    assert any(isinstance(p, host_tools.HostTools) for p in providers.load())
    got = collect([host_tools.HostTools()], {})
    programs = {
        "spotlight_search": MDFIND,
        "speak": SAY,
        "convert_document": TEXTUTIL,
        "list_shortcuts": SHORTCUTS,
        "run_shortcut": SHORTCUTS,
    }
    assert [t.name for t in got.tools] == [
        n for n, p in programs.items() if os.path.exists(p)
    ]
    assert got.core == [] and got.sections == [] and got.problems == []


def test_a_tool_whose_program_is_missing_is_not_offered() -> None:
    assert host_tools.build(lambda program: False) == []
    only_say = host_tools.build(lambda program: program == SAY)
    assert [t.name for t in only_say] == ["speak"]


def test_risk_levels_and_taint(reg: Registry, folder: Path) -> None:
    t = reg.tools
    assert (
        t["spotlight_search"].risk == Risk.R1 and t["spotlight_search"].folder_needed()
    )
    assert t["speak"].risk == Risk.R0 and not t["speak"].folder_needed()
    assert (
        t["convert_document"].risk == Risk.R1
        and not t["convert_document"].folder_needed()
    )
    assert t["list_shortcuts"].risk == Risk.R0
    assert t["run_shortcut"].risk == Risk.R3
    # file names, document text and shortcut names or output come from outside the app; speech does not
    for name in (
        "spotlight_search",
        "convert_document",
        "list_shortcuts",
        "run_shortcut",
    ):
        assert taints(t[name], False)
    assert not taints(t["speak"], False)
    assert not any(tool.network for tool in t.values())
    # Spotlight needs a granted folder; a shortcut asks every time, also with a chat grant, and never chat-wide
    assert decide(t["spotlight_search"], Grants(), False).action == Action.REFUSE
    assert decide(t["spotlight_search"], Grants(folder), False).action == Action.RUN
    assert decide(t["speak"], Grants(), True).action == Action.RUN
    v = decide(t["run_shortcut"], Grants(folder, frozenset({"run_shortcut"})), False)
    assert v.action == Action.ASK and not v.chat_scope


# ---------- run_program ----------


def plain_ctx(tmp_path: Path, deadline: float | None = None) -> Context:
    return Context("c1", None, threading.Event(), tmp_path / "ws", deadline)


def test_run_program_stops_a_program_at_its_time_limit(tmp_path: Path) -> None:
    t0 = time.monotonic()
    with pytest.raises(ToolError, match="took longer than 0 s"):
        host_tools.run_program(
            ["/bin/sleep", "30"], plain_ctx(tmp_path), 0.3, "Sleeping"
        )
    assert time.monotonic() - t0 < 5


def test_run_program_ends_at_the_answers_deadline(tmp_path: Path) -> None:
    ctx = plain_ctx(tmp_path, time.monotonic() + 0.3)
    t0 = time.monotonic()
    with pytest.raises(ToolError, match="took longer"):
        host_tools.run_program(["/bin/sleep", "30"], ctx, 60, "Sleeping")
    assert time.monotonic() - t0 < 5
    with pytest.raises(ToolError, match="no time is left"):
        host_tools.run_program(
            ["/bin/echo"], plain_ctx(tmp_path, time.monotonic() - 1), 60, "Echo"
        )


def test_run_program_stops_on_stop(tmp_path: Path) -> None:
    ctx = plain_ctx(tmp_path)
    threading.Timer(0.2, ctx.abort.set).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        host_tools.run_program(["/bin/sleep", "30"], ctx, 60, "Sleeping")
    assert time.monotonic() - t0 < 5


def test_run_program_gives_a_small_environment_and_no_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MMW_DUMMY_SECRET", "dummy")
    ran = host_tools.run_program(["/usr/bin/env"], plain_ctx(tmp_path), 10, "env")
    names = {line.split("=", 1)[0] for line in ran.stdout.decode().splitlines()}
    assert ran.code == 0 and "MMW_DUMMY_SECRET" not in names and "PATH" in names
    ran = host_tools.run_program(["/bin/cat"], plain_ctx(tmp_path), 10, "cat")
    assert ran.code == 0 and ran.stdout == b""


def test_run_program_names_a_program_that_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="cannot start"):
        host_tools.run_program(
            [str(tmp_path / "missing")], plain_ctx(tmp_path), 5, "Missing"
        )


# ---------- spotlight_search ----------


def test_spotlight_searches_only_the_granted_folder_and_shows_relative_paths(
    reg: Registry,
    ctx: Context,
    folder: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        folder / "notes" / "invoice.txt",
        folder / "notes",
        folder / ".env",  # hidden
        folder / "server.pem",  # secret
        tmp_path
        / "private.txt",  # outside (mdfind would not give it; the tool checks anyway)
        folder / "link.txt",  # a link out of the folder
        folder / "gone.txt",  # indexed, then deleted
        folder,
    ]
    run = fake(
        monkeypatch, lambda argv: ok(b"\0".join(os.fsencode(p) for p in hits) + b"\0")
    )
    r = reg.call("spotlight_search", {"query": "invoice"}, ctx)
    assert r.status == "ok", r.content
    assert run.calls == [[MDFIND, "-0", "-onlyin", str(folder), "invoice"]]
    assert run.seconds == [host_tools.SPOTLIGHT_SECONDS]
    assert (
        r.content == "2 found by Spotlight in '.':\nnotes/\nnotes/invoice.txt  (11 B)"
    )
    for word in ("env", "pem", "private", "link", "gone", str(tmp_path)):
        assert word not in r.content


def test_spotlight_by_name_in_a_subfolder_with_a_cap(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(5):
        (folder / "notes" / f"draft{n}.txt").write_text("x")
    paths = sorted((folder / "notes").glob("draft*.txt"))
    run = fake(monkeypatch, lambda argv: ok(b"\0".join(os.fsencode(p) for p in paths)))
    r = reg.call(
        "spotlight_search",
        {"query": "draft", "by": "name", "path": "notes", "max_results": 2},
        ctx,
    )
    assert run.calls == [
        [MDFIND, "-0", "-onlyin", str(folder / "notes"), "-name", "draft"]
    ]
    assert r.content.splitlines() == [
        "5 found by Spotlight in 'notes':",
        "notes/draft0.txt  (1 B)",
        "notes/draft1.txt  (1 B)",
        "[Showing 2: narrow the query or the path.]",
    ]


def test_spotlight_refusals(
    reg: Registry, ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, lambda argv: ok())
    for args, why in (
        ({"query": "-live"}, "must not start with '-'"),
        ({"query": "a\nb"}, "line breaks"),
        ({"query": "x", "path": ".."}, "outside the granted folder"),
        ({"query": "x", "path": "notes/invoice.txt"}, "not a folder"),
    ):
        r = reg.call("spotlight_search", args, ctx)
        assert r.status == "error" and why in r.content, (args, r.content)
    assert run.calls == []
    no_folder = Context("c1", None, threading.Event(), tmp_path / "ws")
    r = reg.call("spotlight_search", {"query": "x"}, no_folder)
    assert r.status == "error" and "No folder is granted" in r.content


def test_spotlight_with_no_result_or_an_error(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(monkeypatch, lambda argv: ok())
    r = reg.call("spotlight_search", {"query": "nothing"}, ctx)
    assert (
        r.status == "ok"
        and "found no file" in r.content
        and "search_files" in r.content
    )
    fake(
        monkeypatch,
        lambda argv: Ran(
            1,
            b"",
            False,
            "mdfind[1:2] [UserQueryParser] Loading keywords\nFailed to create query\n",
        ),
    )
    r = reg.call("spotlight_search", {"query": "a ==="}, ctx)
    assert (
        r.status == "error"
        and r.content == "Spotlight could not search (Failed to create query)."
    )


@pytest.mark.skipif(not os.path.exists(MDFIND), reason="no mdfind")
def test_spotlight_real_call_on_a_temp_folder(reg: Registry, ctx: Context) -> None:
    """Spotlight may not index a temp folder, so only the call is checked: it runs and gives a result."""
    r = reg.call("spotlight_search", {"query": "invoice"}, ctx)
    assert r.status == "ok", r.content
    assert "Spotlight" in r.content and str(ctx.folder) not in r.content


# ---------- speak ----------


def say_fake(spoken: list[str]) -> Callable[[list[str]], Ran]:
    def handler(argv: list[str]) -> Ran:
        if argv[1:] == ["-v", "?"]:
            return ok(VOICES.encode())
        spoken.append(Path(argv[argv.index("-f") + 1]).read_text())
        Path(argv[argv.index("-o") + 1]).write_bytes(b"FAKE-AUDIO" * 100)
        return ok()

    return handler


def test_speak_turkish_text_gets_the_turkish_voice(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoken: list[str] = []
    run = fake(monkeypatch, say_fake(spoken))
    r = reg.call(
        "speak", {"text": "Merhaba, ışık ve ağaç.", "to": "audio/hello.m4a"}, ctx
    )
    assert r.status == "ok", r.content
    assert r.files == ["audio/hello.m4a"]
    assert (ctx.work_dir / "audio" / "hello.m4a").read_bytes() == b"FAKE-AUDIO" * 100
    assert "Yelda (Turkish (Türkiye))" in r.content and "1000 B" in r.content
    argv = run.calls[-1]
    # the text goes in as a file, never as an argument, so a leading '-' is no option
    assert (
        argv[:3] == [SAY, "-v", "Yelda (Turkish (Türkiye))"]
        and argv[3] == "-f"
        and argv[5] == "-o"
    )
    assert spoken == ["Merhaba, ışık ve ağaç."]


def test_speak_without_a_language_uses_the_system_voice(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoken: list[str] = []
    run = fake(monkeypatch, say_fake(spoken))
    r = reg.call("speak", {"text": "-v Zarvox hello"}, ctx)
    assert r.status == "ok", r.content
    assert len(run.calls) == 1 and "-v" not in run.calls[0][:3]  # no voice list needed
    assert spoken == ["-v Zarvox hello"] and "the system voice" in r.content
    assert r.files[0].startswith("speech-") and r.files[0].endswith(".m4a")


def test_speak_picks_a_voice_by_language_or_name(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, say_fake([]))
    cases = (
        ({"language": "en"}, "Eddy (English (US))"),  # Albert is a novelty voice
        ({"language": "de-DE"}, "Eddy (German (Germany))"),
        ({"voice": "anna"}, "Anna"),
        ({"voice": "Eddy", "language": "de"}, "Eddy (German (Germany))"),
        ({"voice": "Eddy (English (US))"}, "Eddy (English (US))"),
        ({"voice": "Albert"}, "Albert"),
    )
    for extra, voice in cases:
        r = reg.call("speak", {"text": "hello", **extra}, ctx)
        assert r.status == "ok", (extra, r.content)
        assert run.calls[-1][1:3] == ["-v", voice], extra
    # the voice list is read once and kept
    assert sum(argv[1:] == ["-v", "?"] for argv in run.calls) == 1


def test_speak_refusals(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, say_fake([]))
    for args, why in (
        ({"text": "hi", "voice": "Eddy"}, "Several voices are named 'Eddy'"),
        (
            {"text": "hi", "voice": "Edy"},
            "No installed voice is named 'Edy'. Close: Eddy.",
        ),
        (
            {"text": "hi", "language": "ja"},
            "No voice for the language 'ja' is installed",
        ),
        ({"text": "hi", "to": "clip.mp3"}, "must end with .m4a, .aiff, .wav"),
        ({"text": "hi", "to": "../clip.m4a"}, "without '..'"),
        ({"text": "hi", "to": ".hidden.m4a"}, "hidden or secret"),
        ({"text": "   "}, "give the text"),
        ({"text": "x" * (host_tools.MAX_SPEAK + 1)}, "Invalid arguments"),
    ):
        r = reg.call("speak", args, ctx)
        assert r.status == "error" and why in r.content, (args, r.content)
    assert all(argv[1:] == ["-v", "?"] for argv in run.calls)  # say never spoke
    r = reg.call("speak", {"text": "hi", "to": "a.m4a"}, ctx)
    assert r.status == "ok"
    r = reg.call("speak", {"text": "hi", "to": "a.m4a"}, ctx)
    assert r.status == "error" and "Choose another name" in r.content


def test_speak_wav_and_a_failed_say(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, say_fake([]))
    r = reg.call("speak", {"text": "hi", "to": "clip.wav"}, ctx)
    assert r.status == "ok" and run.calls[-1][-1] == "--data-format=LEI16@22050"
    fake(
        monkeypatch,
        lambda argv: Ran(1, b"", False, "Opening output file failed: -50\n"),
    )
    r = reg.call("speak", {"text": "hi"}, ctx)
    assert r.status == "error" and "Opening output file failed" in r.content


@pytest.mark.skipif(not os.path.exists(SAY), reason="no say")
def test_speak_real_call_writes_an_audio_file(reg: Registry, ctx: Context) -> None:
    """The real say, into a file (nothing plays)."""
    r = reg.call("speak", {"text": "One two three.", "to": "real.aiff"}, ctx)
    assert r.status == "ok", r.content
    data = (ctx.work_dir / "real.aiff").read_bytes()
    assert data[:4] == b"FORM" and len(data) > 8000  # an AIFF file with sound in it


# ---------- convert_document ----------


@pytest.mark.skipif(not os.path.exists(TEXTUTIL), reason="no textutil")
def test_convert_html_to_text_and_word_with_the_real_textutil(
    reg: Registry, ctx: Context
) -> None:
    r = reg.call("convert_document", {"path": "report.html", "format": "txt"}, ctx)
    assert r.status == "ok", r.content
    assert r.files == ["report.txt"]
    # HTML without a charset is read as UTF-8, not Latin-1
    assert r.content.endswith("The text:\nRapor: ağaç, şeker, ılık\n")
    r = reg.call("convert_document", {"path": "report.html", "format": "docx"}, ctx)
    assert r.status == "ok" and r.files == ["report.docx"]
    assert (
        "Converted report.html (HTML) to Word: report.docx in the work folder"
        in r.content
    )
    with zipfile.ZipFile(ctx.work_dir / "report.docx") as z:
        assert "word/document.xml" in z.namelist()
    # from the work folder, under a free name
    r = reg.call(
        "convert_document",
        {"path": "report.docx", "format": "txt", "location": "work"},
        ctx,
    )
    assert r.status == "ok", r.content
    assert r.files == ["report-2.txt"] and "ağaç" in r.content
    r = reg.call(
        "convert_document",
        {"path": "report.docx", "format": "rtf", "location": "work", "to": "out/r.rtf"},
        ctx,
    )
    assert r.status == "ok" and (
        ctx.work_dir / "out" / "r.rtf"
    ).read_bytes().startswith(b"{\\rtf")


def test_convert_runs_textutil_on_a_copy_without_loading_resources(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (folder / "page.html").write_text(
        '<meta charset="windows-1254"><img src="http://example.invalid/x.png">'
    )

    def handler(argv: list[str]) -> Ran:
        src = argv[-1]
        assert src != str(folder / "page.html") and Path(src).read_bytes().startswith(
            b"<meta"
        )
        Path(argv[argv.index("-output") + 1]).write_text("converted")
        return ok()

    run = fake(monkeypatch, handler)
    r = reg.call("convert_document", {"path": "page.html", "format": "rtf"}, ctx)
    assert r.status == "ok", r.content
    argv = run.calls[0]
    assert argv[:7] == [
        TEXTUTIL,
        "-convert",
        "rtf",
        "-format",
        "html",
        "-noload",
        "-nostore",
    ]
    assert "-inputencoding" not in argv  # the file names its own charset
    assert argv[-2] == "--" and run.seconds == [host_tools.CONVERT_SECONDS]


def test_convert_refusals(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (folder / "bomb.docx").write_bytes(b"")
    with zipfile.ZipFile(folder / "bomb.docx", "w") as z:
        for n in range(2001):
            z.writestr(f"f{n}.xml", "")
    (folder / "same.txt").write_text("x")
    run = fake(monkeypatch, lambda argv: ok())  # textutil writes nothing
    for args, why in (
        ({"path": "photo.png", "format": "txt"}, "not a supported document"),
        ({"path": "same.txt", "format": "txt"}, "plain text already"),
        ({"path": "report.html", "format": "txt", "to": "r.rtf"}, "must end with .txt"),
        ({"path": ".env.txt", "format": "rtf"}, "hidden or secret"),
        ({"path": "../private.txt", "format": "rtf"}, "outside the granted folder"),
        (
            {"path": "nothing.txt", "format": "rtf", "location": "work"},
            "work folder is empty",
        ),
        ({"path": "bomb.docx", "format": "txt"}, "more than 2000 parts"),
    ):
        r = reg.call("convert_document", args, ctx)
        assert r.status == "error" and why in r.content, (args, r.content)
    assert run.calls == []
    r = reg.call("convert_document", {"path": "report.html", "format": "rtf"}, ctx)
    assert r.status == "error" and "textutil could not convert it" in r.content
    assert not ctx.work_dir.exists() or not any(ctx.work_dir.iterdir())


# ---------- Shortcuts ----------


def shortcut_fake(
    outputs: dict[str, bytes], seen: list[str]
) -> Callable[[list[str]], Ran]:
    def handler(argv: list[str]) -> Ran:
        if argv[1] == "list":
            return ok(SHORTCUT_LIST.encode())
        if "--input-path" in argv:
            seen.append(Path(argv[argv.index("--input-path") + 1]).read_text())
        Path(argv[argv.index("--output-path") + 1]).write_bytes(
            outputs.get(argv[2], b"")
        )
        return ok()

    return handler


def test_list_shortcuts(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, shortcut_fake({}, []))
    r = reg.call("list_shortcuts", {}, ctx)
    assert r.status == "ok"
    assert (
        r.content
        == "The user's shortcuts (3):\n- Dosyayı Kaydet\n- Make GIF\n- What’s a shortcut?"
    )
    assert run.calls == [[SHORTCUTS, "list", "--show-identifiers"]]


def test_run_shortcut_shows_the_name_and_input_and_runs_by_identifier(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    ident = "5ACAB88F-A7F9-4732-89FB-41A09F9EE79D"
    run = fake(monkeypatch, shortcut_fake({ident: b"done: 3 frames\n"}, seen))
    tool = reg.get("run_shortcut")
    args = {"name": "make gif", "input": "-i frames/"}
    assert tool.describe(args) == (
        "Runs your shortcut 'make gif' with 10 characters of input text. It can do anything the shortcut does."
    )
    ctx.preview = tool.show(args, ctx)
    assert (
        ctx.preview
        == f"Shortcut: Make GIF\nIdentifier: {ident}\nInput text (10 characters):\n-i frames/"
    )
    r = reg.call("run_shortcut", args, ctx)
    assert r.status == "ok", r.content
    assert r.content == "The shortcut 'Make GIF' ran. Its output:\ndone: 3 frames\n"
    argv = run.calls[-1]
    assert argv[:3] == [SHORTCUTS, "run", ident] and argv[3] == "--output-path"
    assert argv[5] == "--input-path" and seen == ["-i frames/"]
    assert run.seconds[-1] == host_tools.SHORTCUT_SECONDS


def test_run_shortcut_without_input_and_with_output_that_is_not_text(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = "7D08FF71-A37C-49F9-9372-11397EDF981A"
    run = fake(monkeypatch, shortcut_fake({ident: b"\x89PNG\r\n\x1a\n\x00\x00"}, []))
    assert (
        reg.get("run_shortcut")
        .show({"name": "Dosyayı Kaydet"}, ctx)
        .endswith("Input: none")
    )
    r = reg.call("run_shortcut", {"name": "Dosyayı Kaydet"}, ctx)
    assert r.status == "ok" and "is not text" in r.content
    assert "--input-path" not in run.calls[-1]


def test_run_shortcut_does_not_run_a_changed_or_unknown_shortcut(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = fake(monkeypatch, shortcut_fake({}, []))
    tool = reg.get("run_shortcut")
    with pytest.raises(
        ToolError, match="no shortcut named 'Make GIFF'. Close names: 'Make GIF'"
    ):
        tool.show({"name": "Make GIFF"}, ctx)
    ctx.preview = tool.show({"name": "Make GIF"}, ctx)
    # another shortcut takes the name after the user saw the card
    other = SHORTCUT_LIST.replace(
        "5ACAB88F-A7F9-4732-89FB-41A09F9EE79D", "11111111-2222-3333-4444-555555555555"
    )
    fake(monkeypatch, lambda argv: ok(other.encode()))
    r = reg.call("run_shortcut", {"name": "Make GIF"}, ctx)
    assert r.status == "error" and "changed after the user saw the call" in r.content
    assert not any(argv[1] == "run" for argv in run.calls)


def test_run_shortcut_failure_names_the_reason(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(argv: list[str]) -> Ran:
        if argv[1] == "list":
            return ok(SHORTCUT_LIST.encode())
        return Ran(1, b"", False, "Error: The operation couldn’t be completed.\n")

    fake(monkeypatch, handler)
    r = reg.call("run_shortcut", {"name": "Make GIF"}, ctx)
    assert r.status == "error"
    assert (
        r.content
        == "The shortcut 'Make GIF' failed (Error: The operation couldn’t be completed.)."
    )


@pytest.mark.skipif(not os.path.exists(SHORTCUTS), reason="no shortcuts")
def test_shortcuts_real_list_and_an_unknown_name(reg: Registry, ctx: Context) -> None:
    """The real shortcut list (read only). An unknown name fails before anything runs."""
    assert reg.call("list_shortcuts", {}, ctx).status == "ok"
    with pytest.raises(ToolError, match="no shortcut named"):
        reg.get("run_shortcut").show({"name": "No such shortcut 7f3a"}, ctx)
