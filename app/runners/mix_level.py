"""Mix level runner (runs in the app's own Python, standard library and ffmpeg only): the last step of edit.mix.

Measures the integrated loudness of the finished mix and adds it to the file's note in notes.json, so the note
shows the measured level of the encoded file and not the target.

  python mix_level.py mixed.mp4
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def measured(err: str) -> str:
    """The note's ending from ffmpeg's ebur128 summary."""
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    return (
        f"final mix {float(found[-1]):.1f} LUFS" if found else "final mix not measured"
    )


def main() -> None:
    mix = Path(sys.argv[1])
    err = subprocess.run(
        [
            "ffmpeg",
            "-nostats",
            "-hide_banner",
            "-i",
            str(mix),
            "-vn",
            "-af",
            "ebur128",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    notes_file = mix.parent / "notes.json"
    notes = json.loads(notes_file.read_text()) if notes_file.exists() else {}
    level = measured(err)
    notes[mix.name] = f"{notes[mix.name]}; {level}" if notes.get(mix.name) else level
    notes_file.write_text(json.dumps(notes))
    print(level)


if __name__ == "__main__":
    main()
