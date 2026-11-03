"""The media tools (R1): media_info reads the facts of an image, audio or video file with ffprobe; view_image lets the
model see an image in its next step.

A file comes from one of three places ("location"): the granted folder, the chat's work folder (what run_python
wrote) or the Library (a finished job's output). Each is opened by the app with O_NOFOLLOW and the same checks as the
file tools; ffprobe and ffmpeg get the open file as /dev/fd/N, so a link swapped in after the check cannot redirect
them. They run with an argument list (no shell), a time limit, the "file" protocol only, and a list of the
demuxers they may use, so a playlist cannot make them read other files or the network. They parse files that code
in the sandbox may have written, so they run in a Seatbelt sandbox too (media_profile): they read only the system
and Homebrew folders and that one open file, write only view_image's PNG, and have no network and no services.

view_image writes a copy of the image, at most IMAGE_SIDE pixels on its long side, as PNG into the chat's folder
(outputs/chats/<id>/view_<hex>.png; the sandbox cannot write there). The tool result names it (Output.images), and
the chat loop puts that image into the tool message of the model's next request.
"""

import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from app.agent import safe_fs
from app.agent.file_tools import _rel, _root, human, open_file, resolve
from app.agent.policy import Risk
from app.agent.ports import WorkstationPort
from app.agent.registry import Context, Output, Tool, ToolError
from app.agent.sandbox import DEVICES, SANDBOX_EXEC, SYSCTLS, SYSTEM_READ, _quote
from app.agent.shell_sandbox import HOMEBREW_READ, homebrew

FFPROBE = "ffprobe"  # from PATH, like the rest of the app (start.py puts Homebrew's folders first)
FFMPEG = "ffmpeg"
PROBE_SECONDS = 20
CONVERT_SECONDS = 30
IMAGE_SIDE = 1280  # pixels of the long side of the copy the model sees
MAX_IMAGE = 50 * 1024 * 1024  # bytes of an image view_image reads
IMAGE_TYPES = {  # extension -> the demuxer ffmpeg must use (no probing)
    ".png": "png_pipe",
    ".jpg": "jpeg_pipe",
    ".jpeg": "jpeg_pipe",
    ".webp": "webp_pipe",
    ".gif": "gif",
    ".bmp": "bmp_pipe",
    ".tif": "tiff_pipe",
    ".tiff": "tiff_pipe",
}
AUDIO_TYPES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".aif",
    ".aiff",
}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MEDIA_TYPES = set(IMAGE_TYPES) | {".heic"} | AUDIO_TYPES | VIDEO_TYPES
# the demuxers ffprobe may use: the media types above, nothing that reads other files (hls, concat...)
DEMUXERS = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,wav,mp3,aac,flac,ogg,aiff,"
    "png_pipe,jpeg_pipe,webp_pipe,gif,bmp_pipe,tiff_pipe"
)
LOCATIONS = ("folder", "work", "library")
ENTRIES = (
    "format=format_name,duration,bit_rate:stream=codec_type,codec_name,width,height,avg_frame_rate,"
    "r_frame_rate,channels,sample_rate,duration"
)


def open_media(
    args: dict[str, Any], ctx: Context, port: WorkstationPort | None, types: set[str]
) -> tuple[int, os.stat_result, str]:
    """The file the arguments name, open read-only, with its stat and a label for the result."""
    given = args["path"].strip()
    if Path(given).suffix.lower() not in types:
        kinds = ", ".join(sorted(types))
        raise ToolError(f"{given}: not a supported type ({kinds}).")
    where = args["location"]
    if where == "folder":
        root = _root(ctx)
        path = resolve(root, given)
        fd, st = open_file(path, root)
        return fd, st, _rel(path, root)
    if where == "work":
        fd, st = safe_fs.open_work_file(ctx.work_dir, given)
        return fd, st, f"work folder: {given}"
    if port is None:
        raise ToolError("This app has no Library in this profile.")
    job, _, name = given.partition("/")
    path = port.output_file(job, name) if job and name else None
    if path is None:
        raise ToolError(
            f"{given}: no such Library output. Give '<job id>/<file name>' as search_library shows them."
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ToolError(f"{given}: cannot open ({e.strerror}).") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ToolError(f"{given}: not a regular file.")
    return fd, st, f"Library: {given}"


def media_profile(exe: str, fd: int, output: str | None) -> str:
    """The Seatbelt profile (SBPL) for one ffprobe or ffmpeg run: exec the program, read the system folders,
    Homebrew's program folders, the program's own prefix and the one open file (/dev/fd/N), write only `output`. No
    network, no services (mach-lookup), no other processes, no fork."""
    brew = homebrew()
    reads = [
        *SYSTEM_READ,
        *([f"{brew}/{d}" for d in HOMEBREW_READ] if brew else []),
        os.path.dirname(
            os.path.dirname(exe)
        ),  # a program outside Homebrew: its own lib folder
    ]
    sysctls = " ".join(
        ['(sysctl-name-prefix "hw.")'] + [f"(sysctl-name {_quote(n)})" for n in SYSCTLS]
    )
    lines = [
        "(version 1)",
        "(deny default)",
        "(deny process-info*)",
        "(allow process-info* (target self))",
        f"(allow process-exec (literal {_quote(exe)}))",
        f"(allow sysctl-read {sysctls})",
        "(allow file-read-metadata)",
        "(allow file-read* "
        + " ".join(
            [f"(subpath {_quote(r)})" for r in reads]
            + [f"(literal {_quote(r)})" for r in ("/", f"/dev/fd/{fd}", *DEVICES)]
        )
        + ")",
        '(allow file-write-data (literal "/dev/null"))',
    ]
    if output is not None:  # the sandbox sees real paths (/private/var, not /var)
        real = os.path.join(
            os.path.realpath(os.path.dirname(output)), os.path.basename(output)
        )
        lines.append(f"(allow file-write* (literal {_quote(real)}))")
    return "\n".join(lines) + "\n"


def run_media(
    cmd: list[str], fd: int, seconds: float, what: str, output: str | None = None
) -> str:
    """Run ffprobe or ffmpeg on the open file (as /dev/fd/N, from its start) in its sandbox (media_profile), which
    may write only `output`; its stdout, or ToolError."""
    os.lseek(fd, 0, os.SEEK_SET)
    found = shutil.which(cmd[0])
    if found is None:
        raise ToolError(f"{cmd[0]} is not installed (brew install ffmpeg).")
    if not os.path.exists(SANDBOX_EXEC):
        raise ToolError(
            f"{cmd[0]} runs only in a sandbox, and sandbox-exec is not on this system."
        )
    exe = os.path.realpath(found)
    try:
        done = subprocess.run(
            [SANDBOX_EXEC, "-p", media_profile(exe, fd, output), exe, *cmd[1:]],
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} is not installed (brew install ffmpeg).") from None
    except subprocess.TimeoutExpired:
        raise ToolError(f"{what}: {cmd[0]} took longer than {seconds:.0f} s.") from None
    if done.returncode != 0:
        why = (
            " ".join(done.stderr.strip().splitlines()[-2:])[:300]
            or f"exit code {done.returncode}"
        )
        raise ToolError(f"{what}: {cmd[0]} could not read it ({why}).")
    return done.stdout


def probe(fd: int, label: str) -> dict[str, Any]:
    out = run_media(
        [
            FFPROBE,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            DEMUXERS,
            "-show_entries",
            ENTRIES,
            "-of",
            "json",
            f"/dev/fd/{fd}",
        ],
        fd,
        PROBE_SECONDS,
        label,
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        raise ToolError(f"{label}: ffprobe gave no facts.") from None
    return data if isinstance(data, dict) else {}


def rate(text: Any) -> str | None:
    """A frame rate such as "30000/1001" as "29.97"."""
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return f"{value:.3f}".rstrip("0").rstrip(".") if value > 0 else None


def seconds(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def describe_facts(data: dict[str, Any], label: str, size: int) -> str:
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    lines = [f"{label}: {human(size)}, format {fmt.get('format_name', 'unknown')}"]
    length = seconds(fmt.get("duration"))
    still = all(
        s.get("codec_type") == "video" and not seconds(s.get("duration"))
        for s in streams
    ) or fmt.get("format_name", "").endswith("_pipe")
    if length and not still:
        lines.append(f"duration: {length:.2f} s")
    if fmt.get("bit_rate") and str(fmt["bit_rate"]).isdigit() and not still:
        lines.append(f"bit rate: {int(fmt['bit_rate']) // 1000} kb/s")
    for s in streams:
        kind = s.get("codec_type")
        if kind == "video":
            bits = [f"video: {s.get('codec_name', '?')}"]
            if s.get("width") and s.get("height"):
                bits.append(f"{s['width']}x{s['height']}")
            fps = rate(s.get("avg_frame_rate")) or rate(s.get("r_frame_rate"))
            if fps and not still:
                bits.append(f"{fps} fps")
            lines.append(", ".join(bits))
        elif kind == "audio":
            bits = [f"audio: {s.get('codec_name', '?')}"]
            if s.get("channels"):
                n = s["channels"]
                bits.append(f"{n} channel" + ("" if n == 1 else "s"))
            if s.get("sample_rate"):
                bits.append(f"{s['sample_rate']} Hz")
            lines.append(", ".join(bits))
    if not streams:
        lines.append("no audio or video streams")
    return "\n".join(lines)


def build(port: WorkstationPort | None) -> list[Tool]:
    def media_info(args: dict[str, Any], ctx: Context) -> str:
        fd, st, label = open_media(args, ctx, port, MEDIA_TYPES)
        try:
            data = probe(fd, label)
        finally:
            os.close(fd)
        return describe_facts(data, label, st.st_size)

    def view_image(args: dict[str, Any], ctx: Context) -> Output:
        fd, st, label = open_media(args, ctx, port, set(IMAGE_TYPES))
        chat_dir = ctx.work_dir.parent
        name = f"view_{secrets.token_hex(6)}.png"
        tmp = chat_dir / f".{name}.tmp.png"
        try:
            if st.st_size > MAX_IMAGE:
                raise ToolError(
                    f"{label}: {human(st.st_size)}; view_image reads images up to {human(MAX_IMAGE)}."
                )
            facts = probe(fd, label)
            video = next(
                (
                    s
                    for s in facts.get("streams") or []
                    if s.get("codec_type") == "video"
                ),
                {},
            )
            chat_dir.mkdir(parents=True, exist_ok=True)
            side = f"min({IMAGE_SIDE},iw)"
            run_media(
                [
                    FFMPEG,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    "file",
                    "-f",
                    IMAGE_TYPES[Path(args["path"].strip()).suffix.lower()],
                    "-i",
                    f"/dev/fd/{fd}",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale='{side}':'min({IMAGE_SIDE},ih)':force_original_aspect_ratio=decrease",
                    "-update",
                    "1",
                    "-f",
                    "image2",
                    "-c:v",
                    "png",
                    str(tmp),
                ],
                fd,
                CONVERT_SECONDS,
                label,
                str(tmp),
            )
            if not tmp.is_file() or tmp.is_symlink():
                raise ToolError(f"{label}: ffmpeg made no image.")
            os.replace(tmp, chat_dir / name)
        finally:
            os.close(fd)
            tmp.unlink(missing_ok=True)
        size = (
            f" ({video['width']}x{video['height']})"
            if video.get("width") and video.get("height")
            else ""
        )
        return Output(
            f"{label}{size}: the image is attached to this result (at most {IMAGE_SIDE} pixels on its long "
            "side). Look at it and answer from what you see.",
            images=[name],
        )

    location = {
        "type": "string",
        "enum": list(LOCATIONS),
        "default": "folder",
        "description": "folder: the granted folder; work: this chat's work folder (files run_python wrote); "
        "library: a finished output, path '<job id>/<file name>'",
    }
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "location": location,
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def where(a: dict[str, Any]) -> str:
        return {
            "work": "the chat's work folder",
            "library": "the Library",
        }.get(str(a.get("location")), "the granted folder")

    return [
        Tool(
            name="media_info",
            description=(
                "Read the facts of an image, audio or video file: duration, size, resolution, frame rate, codecs, "
                "audio channels and sample rate."
            ),
            parameters=params,
            risk=Risk.R1,
            run=media_info,
            repeatable=True,
            untrusted_output=True,
            needs_folder=False,
            explain=lambda a: (
                f"Reads the media facts of {a.get('path', '')!r} in {where(a)}."
            ),
            effect=(
                "Reads the facts (length, size, codecs) of a media file in the folder you grant, the chat's work "
                "folder or the Library. Runs without asking."
            ),
        ),
        Tool(
            name="view_image",
            description=(
                "Look at an image (PNG, JPEG, WebP, GIF, BMP, TIFF): it is attached to the result so you can see "
                "it. View one image at a time, only when you need to see it."
            ),
            parameters=params,
            risk=Risk.R1,
            run=view_image,
            repeatable=True,
            untrusted_output=True,  # an image can hold written instructions
            needs_folder=False,
            explain=lambda a: (
                f"Shows the image {a.get('path', '')!r} in {where(a)} to the model."
            ),
            effect=(
                "Lets the model see an image in the folder you grant, the chat's work folder or the Library. Runs "
                "without asking."
            ),
        ),
    ]
