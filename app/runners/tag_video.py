"""Tags an LTX video's colours and puts its index first (runs in the app's own Python, after LTX wrote the file).

ltx-2-mlx writes H.264 with no colour tags and the index (moov) at the end. Players then guess the colours, and a
browser must read the whole file before it can play it. This step is a stream copy, so the pixels and the sound stay
as they are: the H.264 header gets BT.709 primaries and transfer, the BT.601 matrix that LTX's encode uses, and
limited range; -movflags +faststart moves the index to the front. It takes well under a second.

The video is replaced only when ffmpeg succeeds. On any failure the original file stays and the job goes on: this
step always exits 0 and writes the reason to the job's log.

  python tag_video.py video.mp4
"""

import os
import re
import subprocess
import sys
from pathlib import Path

TAGS = "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=6"
SECONDS = 120
PREFIX = re.compile(r"\[[^\]]+ @ 0x[0-9a-f]+\]\s*")  # "[h264_metadata @ 0x...] "


def tag(src: Path) -> str | None:
    """Tag src in place. Returns None when done, else why the file was kept as it was."""
    # not a media name, so never listed as an output
    tmp = src.with_name(src.name + ".part")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-i",
        str(src),
        "-map",
        "0",
        "-c",
        "copy",
        "-bsf:v",
        TAGS,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(tmp),
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SECONDS, check=False
        )
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            lines = r.stderr.strip().splitlines()
            return (
                PREFIX.sub("", lines[0])
                if lines
                else f"ffmpeg exit code {r.returncode}"
            )
        os.replace(tmp, src)
        return None
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    src = Path(sys.argv[1])
    why = tag(src)
    if why is None:
        print(f"tag_video: {src.name} tagged BT.709, index first")
    else:
        print(f"tag_video: kept {src.name} as it is: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
