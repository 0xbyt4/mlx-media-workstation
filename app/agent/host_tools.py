"""Host tools: macOS programs that the shell sandbox cannot run, as narrow typed tools (docs/AGENT.md "Host tools").

The Seatbelt sandbox of run_shell and run_python has no mach-lookup, so Spotlight (mdfind), speech (say), textutil and
Shortcuts fail inside it: they need system services. Opening those services in the sandbox would weaken it. These
tools run the programs outside the sandbox instead, each with a fixed argument list (no shell), a time limit, the
answer's Stop and only the inputs that the tool checked:

- spotlight_search (R1): mdfind -onlyin the granted folder (or a folder in it). Results outside it, and hidden or
  secret files, are dropped. Paths are relative. File names are text from outside the app (untrusted).
- speak (R0): say writes an audio file from the text; the file goes into the chat's work folder. Nothing plays.
- convert_document (R1): textutil converts a copy of a document (Word, RTF, HTML, OpenDocument, plain text) into a new
  file in the work folder. HTML resources are not loaded (-noload). A text result shows its start (untrusted).
- list_shortcuts (R0): the names of the user's Shortcuts (untrusted: a shortcut can come from a link).
- run_shortcut (R3): runs one Shortcut by its identifier, with an optional text input. A shortcut can do anything,
  so it asks every time; the approval card shows the name, the identifier and the whole input.

All tools are deferred: the model finds them with tool_search. A tool whose program is missing is not offered.
"""

import difflib
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import attachments
from app.agent import safe_fs
from app.agent.extensions import Section
from app.agent.file_tools import (
    _rel,
    _root,
    blocked,
    human,
    inside,
    looks_text,
    open_file,
    resolve,
)
from app.agent.policy import Risk
from app.agent.registry import Cancelled, Context, Output, Tool, ToolError, digest
from app.agent.sandbox import Limits, _Capture, dir_size, kill_group
from app.agent.write_tools import check_free

MDFIND = "/usr/bin/mdfind"
SAY = "/usr/bin/say"
TEXTUTIL = attachments.TEXTUTIL
SHORTCUTS = "/usr/bin/shortcuts"
POLL = 0.05  # seconds between checks of Stop and the time limit
OUTPUT_BYTES = 4 * 1024 * 1024  # stdout kept of one program run
ERROR_BYTES = 64 * 1024  # stderr kept
ENV = {  # what the programs get: no secrets of the app's environment
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME": str(Path.home()),
    "LANG": "en_US.UTF-8",
}

SPOTLIGHT_SECONDS = 20
MAX_RESULTS = 200  # spotlight_search results the model may ask for
MAX_QUERY = 200

SAY_SECONDS = 120
VOICES_SECONDS = 15
VOICES_TTL = 300.0  # seconds the voice list is kept (a voice installed later shows up after this)
MAX_SPEAK = 5000  # characters of text speak reads (about 4 minutes of speech)
AUDIO_TYPES = (".m4a", ".aiff", ".wav")
SAY_FORMAT = {".m4a": [], ".aiff": [], ".wav": ["--data-format=LEI16@22050"]}
TURKISH = set("ğĞşŞıİ")  # letters that only Turkish (of the languages say has) writes
# voices that sing, whisper or make noises: never picked for a language, only by name
NOVELTY = frozenset(
    "albert,bad news,bahh,bells,boing,bubbles,cellos,good news,jester,organ,superstar,trinoids,whisper,"  # noqa: SIM905
    "wobble,zarvox".split(",")
)

CONVERT_SECONDS = attachments.CONVERT_SECONDS
MAX_DOCUMENT = attachments.MAX_DOCUMENT  # bytes of a document convert_document reads
MAX_CONVERTED = 200 * 1024 * 1024  # bytes of a converted file
EXCERPT = 8000  # characters of a text result shown to the model
# input suffix -> textutil's format name
DOC_INPUTS = {
    ".txt": "txt",
    ".rtf": "rtf",
    ".html": "html",
    ".htm": "html",
    ".doc": "doc",
    ".docx": "docx",
    ".odt": "odt",
}
DOC_OUTPUTS = ("txt", "rtf", "html", "docx", "odt")
DOC_KINDS = {
    "txt": "plain text",
    "rtf": "RTF",
    "html": "HTML",
    "doc": "Word",
    "docx": "Word",
    "odt": "OpenDocument",
}

SHORTCUT_SECONDS = 120
LIST_SECONDS = 15
MAX_INPUT = 20000  # characters of a shortcut's text input
MAX_SHORTCUTS = 300  # names list_shortcuts shows
SHORTCUT_LINE = re.compile(r"(.+) \(([0-9A-Fa-f-]{36})\)")
VOICE_LINE = re.compile(r"(.+?)\s+([a-z]{2,3}_[A-Za-z0-9]{2,4})\s+#")


# ---------- running a program ----------


@dataclass
class Ran:
    code: int
    stdout: bytes
    more: bool  # stdout was longer than kept
    stderr: str


def run_program(argv: list[str], ctx: Context, seconds: float, what: str) -> Ran:
    """Run a program outside the sandbox: an argument list (no shell), its own session, stdin from /dev/null and a
    small environment. Stop (ctx.abort) and the time limit (`seconds`, or less when the answer ends sooner) kill its
    process group. Raises Cancelled on Stop, ToolError at the time limit or when it cannot start."""
    left = (
        seconds
        if ctx.deadline is None
        else min(seconds, ctx.deadline - time.monotonic())
    )
    if left <= 0:
        raise ToolError(f"{what}: no time is left in this answer.")
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ENV,
            start_new_session=True,
        )
    except OSError as e:
        raise ToolError(f"{what}: {argv[0]} cannot start ({e.strerror}).") from None
    out = _Capture(proc.stdout, OUTPUT_BYTES)
    err = _Capture(proc.stderr, ERROR_BYTES)
    out.start()
    err.start()
    end = time.monotonic() + left
    try:
        while proc.poll() is None:
            if ctx.abort.is_set():
                raise Cancelled
            if time.monotonic() > end:
                raise ToolError(
                    f"{what} took longer than {left:.0f} s, so it was stopped."
                )
            ctx.abort.wait(POLL)
    finally:
        # once reaped, its id may belong to another process: kill the group only while it runs
        if proc.returncode is None:
            kill_group(proc.pid)
            proc.wait()
        out.join(5)
        err.join(5)
    return Ran(
        proc.returncode,
        bytes(out.data),
        out.more,
        err.data.decode("utf-8", errors="replace"),
    )


def why(ran: Ran) -> str:
    """The last lines of a program's error output (mdfind's locale notes left out), or its exit code."""
    lines = [
        line.strip()
        for line in ran.stderr.splitlines()
        if line.strip() and "[UserQueryParser]" not in line
    ]
    return " ".join(lines[-2:])[:300] or f"exit code {ran.code}"


def save_work(ctx: Context, src: str, names: list[str], limit: int) -> str:
    """Copy the file `src` (a temp file of the tool) into the chat's work folder under the first of `names` that is
    free; its path there. The work folder's size limit (the sandbox's disk limit) and the disk's free space count."""
    size = os.path.getsize(src)
    if size > limit:
        raise ToolError(f"The result is {human(size)}; the limit is {human(limit)}.")
    budget = Limits().disk_mb * 1024 * 1024
    used, _ = dir_size(ctx.work_dir) if ctx.work_dir.exists() else (0, 0)
    if used + size > budget:
        raise ToolError(
            f"The work folder holds {human(used)} and may hold {human(budget)}, so the result "
            f"({human(size)}) does not fit. Delete files there with run_python first."
        )
    check_free(ctx.work_dir, size)
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        for name in names:
            parts = safe_fs.work_parts(name, name)
            dfd = safe_fs.work_dir_fd(ctx.work_dir, parts, True, name)
            try:
                if name != names[-1] and taken(dfd, parts[-1]):
                    continue
                safe_fs.write_new(
                    dfd,
                    parts[-1],
                    safe_fs.copy_fd(fd, limit, ctx.check_abort),
                    exists="the work folder has a file with this name. Choose another name with 'to'.",
                )
            finally:
                os.close(dfd)
            return "/".join(parts)
    finally:
        os.close(fd)
    # not reached: the last name is always tried
    raise ToolError("Give the file a name with 'to'.")


def taken(dfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def work_name(given: str | None, default: str, suffixes: tuple[str, ...]) -> str:
    """The work folder path a tool writes: `given` (checked) or `default`."""
    name = (given or "").strip().strip("/") or default
    safe_fs.work_parts(name, name)
    if Path(name).suffix.lower() not in suffixes:
        raise ToolError(f"{name}: the name must end with {', '.join(suffixes)}.")
    return name


# ---------- spotlight_search ----------


def spotlight(args: dict[str, Any], ctx: Context) -> str:
    root = _root(ctx)
    start = resolve(root, args["path"])
    if not os.path.isdir(start):
        raise ToolError(f"{args['path']}: not a folder.")
    query = args["query"].strip()
    if not query or query.startswith("-") or any(ord(c) < 32 for c in query):
        raise ToolError(
            "query: give words or a Spotlight query; it must not start with '-' or hold line breaks."
        )
    argv = [MDFIND, "-0", "-onlyin", start]
    argv += ["-name", query] if args["by"] == "name" else [query]
    ran = run_program(argv, ctx, SPOTLIGHT_SECONDS, "Spotlight")
    if ran.code != 0:
        raise ToolError(f"Spotlight could not search ({why(ran)}).")
    found: list[tuple[str, str]] = []
    for raw in ran.stdout.split(b"\0"):
        ctx.check_abort()
        if not raw:
            continue
        path = os.path.realpath(os.fsdecode(raw))
        if not inside(path, start) or not inside(path, root) or path == start:
            continue
        rel = _rel(path, root)
        if any(blocked(p) for p in rel.split(os.sep)):
            continue  # never say that a hidden or secret file matched
        try:
            st = os.stat(path)
        except OSError:
            continue  # gone since Spotlight indexed it
        found.append(
            (rel, f"{rel}/" if os.path.isdir(path) else f"{rel}  ({human(st.st_size)})")
        )
    where = _rel(start, root)
    limit = args["max_results"]
    if not found:
        return (
            f"Spotlight found no file for {query!r} in {where!r}. Spotlight finds only files it has indexed: a new "
            "file can take a moment, and folders that Spotlight skips (System Settings > Spotlight) are missing. "
            "search_files reads the text files directly."
        )
    found.sort()
    lines = [f"{len(found)} found by Spotlight in {where!r}:"] + [
        line for _, line in found[:limit]
    ]
    if len(found) > limit or ran.more:
        lines.append(
            f"[Showing {min(limit, len(found))}: narrow the query or the path.]"
        )
    return "\n".join(lines)


# ---------- speak ----------


@dataclass(frozen=True)
class Voice:
    name: str  # as `say -v ?` lists it: "Yelda (Turkish (Türkiye))"
    locale: str  # "tr_TR"

    def base(self) -> str:
        return self.name.split(" (", 1)[0]


voice_cache: tuple[float, list[Voice]] | None = None


def voices(ctx: Context) -> list[Voice]:
    """The installed voices (`say -v ?`), kept for VOICES_TTL seconds."""
    global voice_cache
    if voice_cache is not None and time.monotonic() - voice_cache[0] < VOICES_TTL:
        return voice_cache[1]
    ran = run_program([SAY, "-v", "?"], ctx, VOICES_SECONDS, "Listing the voices")
    if ran.code != 0:
        raise ToolError(f"say could not list the voices ({why(ran)}).")
    found, seen = [], set()
    for line in ran.stdout.decode("utf-8", errors="replace").splitlines():
        m = VOICE_LINE.match(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            found.append(Voice(m.group(1), m.group(2)))
    voice_cache = (time.monotonic(), found)
    return found


def language_of(given: str | None, text: str) -> str | None:
    """The language code to pick a voice for: the given one ("tr", "tr-TR", "tr_TR"), Turkish for a text with
    Turkish-only letters, else None (the system voice)."""
    if given and given.strip():
        return re.split(r"[-_]", given.strip().lower(), maxsplit=1)[0]
    return "tr" if TURKISH & set(text) else None


def pick_voice(ctx: Context, given: str | None, language: str | None) -> Voice | None:
    """The voice for the call, or None for the system voice. say uses the system voice for a name it does not know,
    without an error, so a name must match an installed voice."""
    if not given and not language:
        return None
    installed = voices(ctx)
    in_language = [
        v
        for v in installed
        if language is None or v.locale.lower().startswith(language + "_")
    ]
    if given:
        key = given.strip().casefold()
        named = [v for v in in_language if v.name.casefold() == key] or [
            v for v in in_language if v.base().casefold() == key
        ]
        if len(named) == 1:
            return named[0]
        if named:
            raise ToolError(
                f"Several voices are named {given!r}: "
                + "; ".join(v.name for v in named[:12])
                + ". Give the full name, or a language."
            )
        names = sorted({v.base() for v in in_language})
        close = difflib.get_close_matches(given, names, n=5, cutoff=0.5)
        hint = f" Close: {', '.join(close)}." if close else ""
        raise ToolError(
            f"No installed voice is named {given!r}"
            + (f" for the language {language!r}" if language else "")
            + f".{hint} Leave out the voice to use the system voice"
            + (" or the language's first voice." if language else ".")
        )
    usual = [v for v in in_language if v.base().casefold() not in NOVELTY]
    if not usual:
        raise ToolError(
            f"No voice for the language {language!r} is installed. The user can add one in System Settings > "
            "Accessibility > Spoken Content > System Voice > Manage Voices."
        )
    return usual[0]


def speak(args: dict[str, Any], ctx: Context) -> Output:
    text = args["text"]
    if not text.strip():
        raise ToolError("text: give the text to speak.")
    target = work_name(
        args.get("to"), f"speech-{secrets.token_hex(3)}.m4a", AUDIO_TYPES
    )
    language = language_of(args.get("language"), text)
    voice = pick_voice(ctx, args.get("voice"), language)
    suffix = Path(target).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="ws-say-") as tmp:
        source = os.path.join(tmp, "text.txt")
        Path(source).write_text(text, encoding="utf-8")
        out = os.path.join(tmp, "speech" + suffix)
        argv = [SAY, *(["-v", voice.name] if voice else []), "-f", source, "-o", out]
        ran = run_program(argv + SAY_FORMAT[suffix], ctx, SAY_SECONDS, "Speaking")
        if ran.code != 0 or not os.path.isfile(out):
            raise ToolError(f"say could not make the audio ({why(ran)}).")
        size = os.path.getsize(out)
        saved = save_work(ctx, out, [target], 500 * 1024 * 1024)
    who = voice.name if voice else "the system voice"
    return Output(
        f"Saved the speech as {saved} in the work folder ({human(size)}, {len(text)} characters, {who}). "
        "The user can play or download it from the chat.",
        files=[saved],
    )


# ---------- convert_document ----------


def open_document(
    args: dict[str, Any], ctx: Context
) -> tuple[int, os.stat_result, str]:
    given = args["path"].strip()
    if args["location"] == "work":
        fd, st = safe_fs.open_work_file(ctx.work_dir, given)
        return fd, st, f"work folder: {given}"
    root = _root(ctx)
    path = resolve(root, given)
    fd, st = open_file(path, root)
    return fd, st, _rel(path, root)


def convert(args: dict[str, Any], ctx: Context) -> Output:
    given = args["path"].strip()
    suffix = Path(given).suffix.lower()
    if suffix not in DOC_INPUTS:
        raise ToolError(
            f"{given}: not a supported document ({', '.join(sorted(DOC_INPUTS))})."
        )
    source, fmt = DOC_INPUTS[suffix], args["format"]
    if source == fmt:
        raise ToolError(f"{given}: the file is {DOC_KINDS[fmt]} already.")
    stem = Path(given).stem or "document"
    if args.get("to"):
        names = [work_name(args["to"], "", (f".{fmt}",))]
    else:
        names = [f"{stem}.{fmt}"] + [f"{stem}-{n}.{fmt}" for n in range(2, 100)]
        work_name(names[0], "", (f".{fmt}",))
    fd, st, label = open_document(args, ctx)
    with tempfile.TemporaryDirectory(prefix="ws-convert-") as tmp:
        try:
            if st.st_size > MAX_DOCUMENT:
                raise ToolError(
                    f"{label}: {human(st.st_size)}; documents up to {human(MAX_DOCUMENT)} are converted."
                )
            # a copy: a link swapped in after the checks cannot redirect textutil
            src = os.path.join(tmp, "input" + suffix)
            copy_to(src, fd, ctx)
        finally:
            os.close(fd)
        # zips: the guard runs before textutil unpacks them
        if source in ("docx", "odt"):
            try:
                attachments.open_zip(Path(src)).close()
            except attachments.AttachmentError as e:
                raise ToolError(f"{label}: {e}") from None
        out = os.path.join(tmp, f"output.{fmt}")
        argv = [TEXTUTIL, "-convert", fmt, "-format", source, "-noload", "-nostore"]
        if source == "html" and not declares_charset(src):
            argv += ["-inputencoding", "UTF-8"]  # else textutil reads it as Latin-1
        ran = run_program(
            [*argv, "-output", out, "--", src], ctx, CONVERT_SECONDS, "Converting"
        )
        # textutil exits with 0 also when it cannot read the file: then it writes nothing
        if ran.code != 0 or not os.path.isfile(out):
            raise ToolError(
                f"{label}: textutil could not convert it ({why(ran)}). The file may be damaged or of another type."
            )
        size = os.path.getsize(out)
        saved = save_work(ctx, out, names, MAX_CONVERTED)
        head = (
            f"Converted {label} ({DOC_KINDS[source]}) to {DOC_KINDS[fmt]}: {saved} in the work folder "
            f"({human(size)})."
        )
        if fmt != "txt":
            return Output(head, files=[saved])
        with open(out, "rb") as f:
            text = f.read(EXCERPT * 4).decode("utf-8", errors="replace")
    shown = text[:EXCERPT]
    more = len(text) > EXCERPT or size > EXCERPT * 4
    note = (
        f"\nThe text (the first {len(shown):,} characters; the rest is in the file):\n"
        if more
        else "\nThe text:\n"
    )
    return Output(head + note + shown, files=[saved])


def declares_charset(path: str) -> bool:
    """Whether an HTML file names its encoding (a meta charset) near its start."""
    with open(path, "rb") as f:
        return b"charset" in f.read(4096).lower()


def copy_to(path: str, fd: int, ctx: Context) -> None:
    """Copy the open file `fd` from its start to a new file `path` (in the tool's temp folder)."""
    out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        safe_fs.copy_fd(fd, MAX_DOCUMENT, ctx.check_abort)(out)
    finally:
        os.close(out)


# ---------- Shortcuts ----------


@dataclass(frozen=True)
class Shortcut:
    name: str
    ident: str


def shortcuts(ctx: Context) -> list[Shortcut]:
    ran = run_program(
        [SHORTCUTS, "list", "--show-identifiers"],
        ctx,
        LIST_SECONDS,
        "Listing the shortcuts",
    )
    if ran.code != 0:
        raise ToolError(f"shortcuts could not list them ({why(ran)}).")
    found = []
    for line in ran.stdout.decode("utf-8", errors="replace").splitlines():
        m = SHORTCUT_LINE.fullmatch(line.strip())
        if m:
            found.append(Shortcut(m.group(1), m.group(2).upper()))
    return found


def find_shortcut(name: str, ctx: Context) -> Shortcut:
    """The shortcut with this name (exact, else the only one that matches without case), or ToolError with the
    nearest names."""
    every = shortcuts(ctx)
    hits = [s for s in every if s.name == name] or [
        s for s in every if s.name.casefold() == name.casefold()
    ]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(
            f"Several shortcuts are named {name!r}. Ask the user to rename one in the Shortcuts app."
        )
    close = difflib.get_close_matches(name, [s.name for s in every], n=5, cutoff=0.5)
    hint = f" Close names: {', '.join(repr(c) for c in close)}." if close else ""
    raise ToolError(
        f"There is no shortcut named {name!r}.{hint} list_shortcuts shows every name."
    )


def shortcut_card(s: Shortcut, text: str | None) -> str:
    """The approval card's preview: which shortcut runs, and all of its input."""
    lines = [f"Shortcut: {s.name}", f"Identifier: {s.ident}"]
    if text is None:
        lines.append("Input: none")
    else:
        lines += [f"Input text ({len(text)} characters):", text]
    return "\n".join(lines)


def list_shortcuts(args: dict[str, Any], ctx: Context) -> str:
    every = shortcuts(ctx)
    if not every:
        return "The user has no shortcuts in the Shortcuts app."
    names = sorted({s.name for s in every}, key=str.casefold)
    lines = [f"The user's shortcuts ({len(names)}):"] + [
        f"- {n}" for n in names[:MAX_SHORTCUTS]
    ]
    if len(names) > MAX_SHORTCUTS:
        lines.append(f"[Showing the first {MAX_SHORTCUTS}.]")
    return "\n".join(lines)


def shortcut_preview(args: dict[str, Any], ctx: Context) -> str:
    return shortcut_card(find_shortcut(args["name"], ctx), args.get("input"))


def run_shortcut(args: dict[str, Any], ctx: Context) -> str:
    found = find_shortcut(args["name"], ctx)
    text = args.get("input")
    card = shortcut_card(found, text)
    # the shortcut must still be the one the user approved (a rename or a new one with this name changes the card)
    if ctx.preview is not None and digest(card) != ctx.digest:
        raise ToolError(
            f"The shortcut {args['name']!r} changed after the user saw the call, so it did not run. Call it again."
        )
    with tempfile.TemporaryDirectory(prefix="ws-shortcut-") as tmp:
        out = os.path.join(tmp, "output")
        argv = [SHORTCUTS, "run", found.ident, "--output-path", out]
        if text is not None:
            source = os.path.join(tmp, "input.txt")
            Path(source).write_text(text, encoding="utf-8")
            argv += ["--input-path", source]
        ran = run_program(argv, ctx, SHORTCUT_SECONDS, f"The shortcut {found.name!r}")
        if ran.code != 0:
            raise ToolError(f"The shortcut {found.name!r} failed ({why(ran)}).")
        data, size = b"", 0
        if os.path.isfile(out):
            size = os.path.getsize(out)
            with open(out, "rb") as f:
                data = f.read(EXCERPT * 4)
    data = data or ran.stdout
    if not data.strip():
        return f"The shortcut {found.name!r} ran. It gave no output."
    if not looks_text(data[:8192]):
        return (
            f"The shortcut {found.name!r} ran. Its output ({human(size or len(data))}) is not text; this tool "
            "returns text only."
        )
    text_out = data.decode("utf-8", errors="replace")
    cut = len(text_out) > EXCERPT or size > len(data)
    return (
        f"The shortcut {found.name!r} ran. Its output"
        + (f" (the first {EXCERPT:,} characters)" if cut else "")
        + ":\n"
        + text_out[:EXCERPT]
    )


# ---------- the tools ----------


def available(program: str) -> bool:
    return os.access(program, os.X_OK)


def build(exists: Callable[[str], bool] = available) -> list[Tool]:
    """The host tools whose programs are on this Mac."""
    tools: list[Tool] = []
    if exists(MDFIND):
        tools.append(
            Tool(
                name="spotlight_search",
                description=(
                    "Find files in the granted folder and its subfolders with macOS Spotlight. by=content (the "
                    "default) matches words in a file's text, name or metadata (PDF and Office text too) and "
                    "takes Spotlight query expressions such as kMDItemContentType == 'com.adobe.pdf'; by=name "
                    "matches part of the file name. Only indexed files are found. Paths are relative to the "
                    "granted folder."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_QUERY,
                        },
                        "by": {
                            "type": "string",
                            "enum": ["content", "name"],
                            "default": "content",
                        },
                        "path": {"type": "string", "maxLength": 1024, "default": "."},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_RESULTS,
                            "default": 50,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                risk=Risk.R1,
                run=spotlight,
                repeatable=True,
                untrusted_output=True,
                explain=lambda a: (
                    f"Searches Spotlight for {a.get('query', '')!r} "
                    + ("in file names " if a.get("by") == "name" else "")
                    + f"in {a.get('path', '.')!r} of the granted folder."
                ),
                effect=(
                    "Finds files by name or content with Spotlight, only in the folder you grant to this chat. Runs "
                    "without asking once a folder is granted."
                ),
            )
        )
    if exists(SAY):
        tools.append(
            Tool(
                name="speak",
                description=(
                    "Turn text into speech with the macOS voices and save it as an audio file in the work folder "
                    "(m4a; to may end with .aiff or .wav). Nothing plays. Give language (such as 'tr' or 'en') to "
                    "use that language's voice; Turkish text gets the Turkish voice. voice: an installed voice "
                    "name; leave it out for the system voice."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_SPEAK,
                        },
                        "language": {"type": "string", "maxLength": 10},
                        "voice": {"type": "string", "maxLength": 100},
                        "to": {"type": "string", "maxLength": 200},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk=Risk.R0,
                run=speak,
                side_effects=True,
                explain=lambda a: (
                    f"Saves {len(str(a.get('text', '')))} characters of text as speech in the work folder"
                    + (f" with the voice {a['voice']!r}." if a.get("voice") else ".")
                ),
                effect=(
                    "Turns text into speech with the macOS voices and saves an audio file in this chat's work "
                    "folder. Plays nothing. Runs without asking."
                ),
            )
        )
    if exists(TEXTUTIL):
        tools.append(
            Tool(
                name="convert_document",
                description=(
                    "Convert a document (docx, doc, odt, rtf, html, txt) to txt, rtf, html, docx or odt with "
                    "macOS textutil. The new file goes into the work folder; a txt result also shows its text. "
                    "location: folder (the granted folder, the default) or work (the work folder)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                        "format": {"type": "string", "enum": list(DOC_OUTPUTS)},
                        "location": {
                            "type": "string",
                            "enum": ["folder", "work"],
                            "default": "folder",
                        },
                        "to": {"type": "string", "maxLength": 200},
                    },
                    "required": ["path", "format"],
                    "additionalProperties": False,
                },
                risk=Risk.R1,
                run=convert,
                needs_folder=False,
                side_effects=True,
                untrusted_output=True,
                explain=lambda a: (
                    f"Converts {a.get('path', '')!r} "
                    + (
                        "of the work folder "
                        if a.get("location") == "work"
                        else "of the granted folder "
                    )
                    + f"to {a.get('format', '')} in the work folder."
                ),
                effect=(
                    "Converts a document (Word, RTF, HTML, OpenDocument, text) from the folder you grant or this "
                    "chat's work folder into a new file in the work folder. Runs without asking."
                ),
            )
        )
    if exists(SHORTCUTS):
        tools.append(
            Tool(
                name="list_shortcuts",
                description="List the names of the user's macOS Shortcuts, for run_shortcut.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                risk=Risk.R0,
                run=list_shortcuts,
                repeatable=True,
                untrusted_output=True,
                explain=lambda a: "Lists the names of your Shortcuts.",
                effect="Lists the names of your Shortcuts. Runs without asking.",
            )
        )
        tools.append(
            Tool(
                name="run_shortcut",
                description=(
                    "Run one of the user's macOS Shortcuts by its exact name (list_shortcuts shows them), with an "
                    "optional text input. Returns the shortcut's text output. The user approves every run."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 200},
                        "input": {"type": "string", "maxLength": MAX_INPUT},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                risk=Risk.R3,
                run=run_shortcut,
                needs_folder=False,
                side_effects=True,
                repeatable=True,
                untrusted_output=True,
                explain=lambda a: (
                    f"Runs your shortcut {a.get('name', '')!r}"
                    + (
                        f" with {len(str(a['input']))} characters of input text."
                        if a.get("input") is not None
                        else " without input."
                    )
                    + " It can do anything the shortcut does."
                ),
                preview=shortcut_preview,
                effect=(
                    "Runs one of your Shortcuts outside the sandbox. A shortcut can do anything: send messages, "
                    "change files, use the network. Asks before every call and shows the shortcut's name and input."
                ),
            )
        )
    return tools


class HostTools:
    """The provider (app/agent/extensions.py): every tool deferred, no prompt section. Agent chats only: a
    workstation chat has no granted folder, and its tools array stays the media tools alone."""

    name = "host"
    modes = ("agent",)

    def tools(self) -> list[Tool]:
        return build()

    def core(self, chat: dict[str, Any]) -> list[str]:
        return []  # rare in a chat: the model finds them with tool_search

    def section(self, chat: dict[str, Any]) -> Section | None:
        return None
