"""The tools the workstation offers: their form fields (served to the UI) and how a job of each tool runs.

Every model runs from its own environment as a separate process, so a finished job gives all its memory back. Where
each one is installed comes from config.toml (app/config.py, README.md); defaults follow what worked in our tests and
the models' own guides (docs/PROMPTING.md).
"""

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import PATHS
from app.runners import subtitles

APP = Path(__file__).resolve().parent
MFLUX = PATHS.mflux_bin
LTX = PATHS.ltx_bin / "ltx-2-mlx"
LTX_PY = PATHS.ltx_bin / "python"
LTX_MODEL = PATHS.ltx_model
DRAMA_PY = PATHS.drama_python
DRAMA_MODEL = PATHS.drama_model
ACE_DIR = PATHS.ace_dir
ACE_PY = ACE_DIR / ".venv" / "bin" / "python"
# Drama 3 checks each take with this speech recogniser, read from the Hugging Face cache
HF_HUB = Path(
    os.environ.get("HF_HUB_CACHE")
    or Path(os.environ.get("HF_HOME", "~/.cache/huggingface")) / "hub"
).expanduser()
DRAMA_ASR = HF_HUB / "models--mlx-community--Qwen3-ASR-0.6B-8bit"
# what each tool needs on disk; a missing one refuses the job with the path it looked for
REQUIRES: dict[str, list[Path]] = {
    "image.moss": [MFLUX / "mflux-generate-moss-image"],
    "image.klein": [MFLUX / "mflux-generate-flux3"],
    "image.klein_edit": [MFLUX / "mflux-generate-flux3-edit"],
    "voice.drama3": [DRAMA_PY, DRAMA_MODEL, DRAMA_ASR],
    "voice.transcribe": [DRAMA_PY, DRAMA_ASR],
    "music.ace": [ACE_PY, ACE_DIR / "checkpoints"],
    "video.t2v": [LTX, LTX_MODEL],
    "video.i2v": [LTX, LTX_MODEL],
    "video.a2v": [LTX, LTX_MODEL],
    "video.extend": [LTX_PY, LTX_MODEL],
}

# the languages Qwen3-ASR names in its config.json (support_languages)
ASR_LANGUAGES = [
    "Arabic",
    "Cantonese",
    "Chinese",
    "Czech",
    "Danish",
    "Dutch",
    "English",
    "Filipino",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hindi",
    "Hungarian",
    "Indonesian",
    "Italian",
    "Japanese",
    "Korean",
    "Macedonian",
    "Malay",
    "Persian",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Spanish",
    "Swedish",
    "Thai",
    "Turkish",
    "Vietnamese",
]
MOSS_NEGATIVE = "nsfw, worst quality, bad anatomy, watermark, jpeg artifact"
IMAGE_SIZES = [
    ("768x1344", "9:16 (768x1344)"),
    ("1088x1920", "9:16 large (1088x1920), slower, sharper faces"),
    ("896x1152", "4:5 (896x1152)"),
    ("1024x1024", "1:1 (1024x1024)"),
    ("1344x768", "16:9 (1344x768)"),
]
VIDEO_SIZES = [
    ("704x1280", "9:16 (704x1280), sharper faces"),
    ("576x1024", "9:16 fast (576x1024), about 2x faster, softer faces"),
    ("1024x576", "16:9 (1024x576)"),
    ("768x768", "1:1 (768x768)"),
]
A2V_STYLE = (
    "Static frame, locked-off camera, no zoom. Natural lip movement in sync with the words, small head movements, "
    "slow blinks. Clear, sharp, detailed eyes with a steady natural gaze, both eyes looking the same way. "
    "One continuous take."
)
A2V_MIN_SECONDS, A2V_MAX_SECONDS = (
    1.5,
    20.0,
)  # longer clips were never tested and need far more memory and time
EXTEND_MIN_FRAMES = (
    25  # the model carries the source's last 25 frames into the new part
)
# frames of the source given to the model (2 s), as in our tested extend chains; it continues from the last 25
EXTEND_CARRY_MAX = 49
EXTEND_MAX_SECONDS = (
    8.0  # the longest extend we measured (about 35 GB); longer: extend the result again
)
VIDEO_ENC = [
    "-c:v",
    "libx264",
    "-crf",
    "10",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-b:a",
    "192k",
    "-ar",
    "48000",
]
# Mix: the video's own sound counts as speech only from this loudness (quieter sound is room tone or hiss), and only
# when it has pauses (this share of 400 ms windows 10 dB under its loudness) or a wide loudness range (LU); steady
# sound such as rain has neither
MIX_SPEECH_LUFS = -40.0
MIX_SPEECH_DIPS = 0.15
MIX_SPEECH_LRA = 5.0
# dB: music under a steady sound that is not speech, so both are heard
MIX_AMBIENCE_UNDER = 6.0
MIX_MAX_GAIN = 20.0  # dB: a very quiet music track is not raised more than this
MIX_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
MIX_COPY_CODECS = (
    "h264",
    "hevc",
    "av1",
    "vp9",
    "mpeg4",
)  # an MP4 holds these as they are; others (ProRes) are encoded again
# Join: the sizes it offers ("first" = the first clip's), the still images' length (s) and the longest side it takes
CONCAT_SIZES = [
    ("first", "Same as the first clip"),
    *VIDEO_SIZES,
    ("1080x1920", "9:16 HD (1080x1920)"),
    ("1920x1080", "16:9 HD (1920x1080)"),
]
STILL_MIN, STILL_MAX = 0.5, 60.0
CONCAT_MAX_SIDE = 4096
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif")
# Add Subtitles: a sound track an MP4 holds as it is; others (PCM) are encoded as AAC
SUB_COPY_AUDIO = ("aac", "mp3", "alac", "ac3", "eac3", "opus", "flac")
# the subtitle track's language: ISO 639-2 codes (an MP4 stores three letters) for the languages that Transcribe knows
SUB_LANGUAGES = [
    ("und", "Not set"),
    ("ara", "Arabic"),
    ("yue", "Cantonese"),
    ("zho", "Chinese"),
    ("ces", "Czech"),
    ("dan", "Danish"),
    ("nld", "Dutch"),
    ("eng", "English"),
    ("fil", "Filipino"),
    ("fin", "Finnish"),
    ("fra", "French"),
    ("deu", "German"),
    ("ell", "Greek"),
    ("hin", "Hindi"),
    ("hun", "Hungarian"),
    ("ind", "Indonesian"),
    ("ita", "Italian"),
    ("jpn", "Japanese"),
    ("kor", "Korean"),
    ("mkd", "Macedonian"),
    ("msa", "Malay"),
    ("fas", "Persian"),
    ("pol", "Polish"),
    ("por", "Portuguese"),
    ("ron", "Romanian"),
    ("rus", "Russian"),
    ("spa", "Spanish"),
    ("swe", "Swedish"),
    ("tha", "Thai"),
    ("tur", "Turkish"),
    ("vie", "Vietnamese"),
]
# subtitles from Transcribe with word times: Qwen3-ForcedAligner by its Hugging Face name. None (the default) until
# the owner adds the model to the model list: the runner then times the words from short parts of the audio
SUB_ALIGNER: str | None = None


def opts(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"value": v, "label": label} for v, label in pairs]


def field(name: str, label: str, type_: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "type": type_, **kw}


SEED = field(
    "seed",
    "Seed",
    "number",
    default=None,
    help="Empty = random. Same seed and settings = same result.",
)

TOOLS: list[dict[str, Any]] = [
    {
        "id": "image.moss",
        "group": "Image",
        "label": "MOSS-Image",
        "description": "Photoreal, aesthetic people and scenes. About 4 min at 768x1344, 8 min at 1088x1920. Weak at text.",
        "fields": [
            field(
                "prompt",
                "Prompt",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="One paragraph of about 100-200 words: subject, outfit, pose, framing, lens, light, place, style. English. Keep it under about 400 tokens: longer prompts give a black image (mflux bug 920).",
            ),
            field(
                "size", "Size", "select", default="768x1344", options=opts(IMAGE_SIZES)
            ),
            SEED,
            field(
                "negative",
                "Negative prompt",
                "textarea",
                default=MOSS_NEGATIVE,
                advanced=True,
            ),
            field(
                "steps",
                "Steps",
                "number",
                default=50,
                min=10,
                max=100,
                advanced=True,
                help="The guide recommends 50 or more.",
            ),
            field(
                "guidance",
                "Guidance (CFG)",
                "number",
                default=4.0,
                min=1,
                max=8,
                step=0.5,
                advanced=True,
                help="3-6. Higher for short prompts.",
            ),
        ],
    },
    {
        "id": "image.klein",
        "group": "Image",
        "label": "FLUX.3 Klein",
        "description": "Fast (about 40 s). Good at text in images and at following the prompt.",
        "fields": [
            field(
                "prompt",
                "Prompt",
                "textarea",
                assist=True,
                default="",
                required=True,
                help='Put any text to render in double quotes, e.g. a sign that reads "PUSH".',
            ),
            field(
                "size", "Size", "select", default="768x1344", options=opts(IMAGE_SIZES)
            ),
            SEED,
            field("steps", "Steps", "number", default=16, min=4, max=50, advanced=True),
        ],
    },
    {
        "id": "image.klein_edit",
        "group": "Image",
        "label": "Klein Edit (references)",
        "description": "New image from 1-10 reference images: same person in a new place, combine objects. About 1-4 min.",
        "fields": [
            field(
                "references",
                "Reference images",
                "files",
                accept="image/*",
                max=10,
                required=True,
            ),
            field(
                "prompt",
                "Prompt",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="Name them <ref_image_0>, <ref_image_1>, ... in upload order; put the background first. Give each one role and say what must stay unchanged. Add a head-and-shoulders reference for faces.",
            ),
            field(
                "size", "Size", "select", default="768x1344", options=opts(IMAGE_SIZES)
            ),
            SEED,
        ],
    },
    {
        "id": "voice.drama3",
        "group": "Voice",
        "label": "Fish Drama 3",
        "description": "Expressive speech and dialogue. About 2x real time. Takes are checked by speech recognition. Built with Fish Audio.",
        "fields": [
            field(
                "text",
                "Script",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="Speakers: <|speaker:0|> ... <|speaker:1|> ... Cues in brackets mid-sentence: [laughs], "
                "[sighs], [nervous, quiet]. Don't open with a one-word sentence or a cue; no [long pause]; "
                "line breaks are removed. 2-4 short turns per take work best. Length: about 2.5 spoken words a "
                "second, so 8 s is about 20 words and 20 s about 50.",
            ),
            field(
                "takes",
                "Takes",
                "number",
                default=2,
                min=1,
                max=4,
                help="Results of one job, each with another seed; keep the best.",
            ),
            field(
                "ref0",
                "Voice reference, speaker 0",
                "file",
                accept="audio/*",
                advanced=True,
                help="10-20 s, one clean voice, 44.1 kHz or better.",
            ),
            field(
                "ref0_text",
                "Transcript of reference 0",
                "text",
                default="",
                advanced=True,
            ),
            field(
                "ref1",
                "Voice reference, speaker 1",
                "file",
                accept="audio/*",
                advanced=True,
            ),
            field(
                "ref1_text",
                "Transcript of reference 1",
                "text",
                default="",
                advanced=True,
            ),
            field(
                "temperature",
                "Temperature",
                "number",
                default=1.0,
                min=0.3,
                max=1.5,
                step=0.05,
                advanced=True,
            ),
            field(
                "top_p",
                "Top p",
                "number",
                default=0.9,
                min=0.1,
                max=1.0,
                step=0.05,
                advanced=True,
            ),
            field(
                "top_k", "Top k", "number", default=30, min=5, max=200, advanced=True
            ),
        ],
    },
    {
        "id": "voice.transcribe",
        "group": "Voice",
        "label": "Transcribe",
        "description": "Speech to text with Qwen3-ASR 0.6B, the recogniser that checks the Drama 3 takes. "
        "One line of text for about every 30 s, with its time. A few seconds for a minute of speech.",
        "fields": [
            field(
                "audio",
                "Audio or video",
                "file",
                accept="audio/*,video/*",
                required=True,
                help="Its sound track is transcribed. Music under the voice makes the text worse.",
            ),
            field(
                "language",
                "Language",
                "select",
                default="auto",
                options=opts(
                    [("auto", "Detect (per 30 s part)")]
                    + [(n, n) for n in ASR_LANGUAGES]
                ),
            ),
            field(
                "subtitles",
                "Subtitles (SRT and VTT)",
                "checkbox",
                default=False,
                help="Also writes transcript.srt and transcript.vtt; a line can be about 1 s early or late.",
            ),
        ],
    },
    {
        "id": "music.ace",
        "group": "Music",
        "label": "ACE-Step 1.5",
        "description": "Music from a description; instrumental or with lyrics. Two takes in about 30 s. Uses about 23 GB at peak.",
        "fields": [
            field(
                "caption",
                "Description",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="Genre, mood, instruments, tempo feel, use (e.g. 'tense escape room thriller underscore, ticking clock').",
            ),
            field("instrumental", "Instrumental", "checkbox", default=True),
            field(
                "lyrics",
                "Lyrics",
                "textarea",
                assist=True,
                default="",
                needs_off="instrumental",
                help="Sung only with Instrumental off. Use [verse], [chorus] tags.",
            ),
            field("duration", "Duration (s)", "number", default=30, min=10, max=240),
            field(
                "bpm",
                "BPM",
                "number",
                default=None,
                min=30,
                max=300,
                advanced=True,
                help="Empty = automatic.",
            ),
            field(
                "key",
                "Key",
                "text",
                default="",
                advanced=True,
                help="e.g. D minor. Empty = automatic.",
            ),
            field(
                "takes",
                "Takes",
                "number",
                default=2,
                min=1,
                max=4,
                advanced=True,
                help="Results of one job, each with another seed; keep the best.",
            ),
            SEED | {"advanced": True},
        ],
    },
    {
        "id": "video.t2v",
        "group": "Video",
        "label": "Text to Video",
        "description": "LTX-2.5 from a prompt, with sound. About 3 min for 5 s at 704x1280, 2 min at 576x1024.",
        "fields": [
            field(
                "prompt",
                "Prompt",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="4-8 sentences: shot, place and light, action, character, camera (say how the frame ends), "
                "and every sound; dialogue in quotes.",
            ),
            field(
                "size", "Size", "select", default="704x1280", options=opts(VIDEO_SIZES)
            ),
            field(
                "seconds", "Length (s)", "number", default=5, min=1, max=10, step=0.5
            ),
            field("audio", "Generate sound", "checkbox", default=True),
            SEED,
            field(
                "hq_decoder",
                "Final-quality decoder",
                "checkbox",
                default=False,
                advanced=True,
                help="LTX-2.5's diffusion decoder: slightly cleaner skin and faces, about 2.5x slower in total.",
            ),
        ],
    },
    {
        "id": "video.i2v",
        "group": "Video",
        "label": "Image to Video",
        "description": "Animates a start image. Describe what changes, not what the image shows. About 2 min for 4 s.",
        "fields": [
            field(
                "image",
                "Start image",
                "file",
                accept="image/*",
                required=True,
                help="Cropped to the video size; use the same aspect ratio.",
            ),
            field(
                "prompt",
                "Prompt",
                "textarea",
                assist=True,
                default="",
                required=True,
            ),
            field(
                "size", "Size", "select", default="704x1280", options=opts(VIDEO_SIZES)
            ),
            field(
                "seconds", "Length (s)", "number", default=5, min=1, max=10, step=0.5
            ),
            field("audio", "Generate sound", "checkbox", default=True),
            SEED,
            field(
                "hq_decoder",
                "Final-quality decoder",
                "checkbox",
                default=False,
                advanced=True,
                help="LTX-2.5's diffusion decoder: slightly cleaner skin and faces, about 2.5x slower in total.",
            ),
        ],
    },
    {
        "id": "video.a2v",
        "group": "Video",
        "label": "Voice to Video (talking)",
        "description": "A person from an image speaks the audio, lips in sync. One speaker per clip; length = audio. "
        "About 3.5 min for 7 s.",
        "fields": [
            field(
                "image",
                "Person image",
                "file",
                accept="image/*",
                required=True,
                help="Front-facing and sharp; the face should be large (waist-up works best).",
            ),
            field(
                "audio",
                "Voice audio",
                "file",
                accept="audio/*",
                required=True,
                help="One speaker, clean voice, 1.5-20 s.",
            ),
            field(
                "prompt",
                "Who and how",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="e.g. 'A medium shot of a woman in a navy uniform in a dim museum gallery, facing the lens. "
                'She speaks calmly, saying: "..."\'. Camera and eye wording is added for you.',
            ),
            field(
                "size", "Size", "select", default="576x1024", options=opts(VIDEO_SIZES)
            ),
            field(
                "style",
                "Added camera/face wording",
                "textarea",
                default=A2V_STYLE,
                advanced=True,
            ),
            SEED,
        ],
    },
    {
        "id": "video.extend",
        "group": "Video",
        "label": "Extend Video",
        "description": "Continues a clip: LTX-2.5 picks up its last second (picture and sound) and adds new seconds after it. "
        "The result is the whole clip plus the new part. About 2.5 min for 5 s more.",
        "fields": [
            field(
                "video",
                "Video",
                "file",
                accept="video/*",
                required=True,
                help="A clip from the video tools. Both sides must be a multiple of 64 (all the sizes here are). "
                "About 1 s of its sound carries into the new part.",
            ),
            field(
                "prompt",
                "What happens next",
                "textarea",
                assist=True,
                default="",
                required=True,
                help="4-8 sentences. Repeat the words that describe the person, outfit and place in the first clip. "
                "Say how the frame ends, but don't ask the subject to fill the frame (that made the camera push in).",
            ),
            field(
                "seconds",
                "New seconds",
                "number",
                default=5,
                min=1,
                max=EXTEND_MAX_SECONDS,
                step=0.5,
                help="Up to 8 s per step; extend the result again for more.",
            ),
            field(
                "end_at",
                "Use the clip up to (s)",
                "number",
                default=None,
                min=1.1,
                step=0.1,
                advanced=True,
                help="Empty = the whole clip. Cut just after a spoken line or a movement; the new part starts there, "
                "and speech carries over only if the cut is not silent.",
            ),
            SEED,
        ],
    },
    {
        "id": "edit.mix",
        "group": "Edit",
        "label": "Mix (video + music)",
        "description": "Puts music under a video's own sound: levels measured, music ducked while someone speaks "
        "(a steady sound such as rain is not ducked), looped or cut to the video, faded, final mix at about -14 LUFS "
        "(YouTube). The picture is copied when an MP4 can hold it, else encoded as H.264. A few seconds.",
        "fields": [
            field(
                "video",
                "Video",
                "file",
                accept="video/*",
                required=True,
                help="Its own sound (voice, rain, effects) is kept.",
            ),
            field(
                "music",
                "Music",
                "file",
                accept="audio/*",
                required=True,
                help="Looped if shorter than the video, cut if longer.",
            ),
            field(
                "under",
                "Music under the voice (dB)",
                "number",
                default=14,
                min=3,
                max=30,
                step=1,
                help="How much quieter the music is than the voice. 10 = lively, 14 = normal, 20 = quiet background. "
                f"Used only when the video has speech; under a steady sound the music is {MIX_AMBIENCE_UNDER:.0f} dB "
                "quieter than it.",
            ),
            field(
                "duck",
                "Duck under speech",
                "checkbox",
                default=True,
                help="The music dips by about 5 dB while someone speaks, at any voice level. Only when the video has "
                "speech.",
            ),
            field(
                "fade_in",
                "Fade in (s)",
                "number",
                default=0.5,
                min=0,
                max=10,
                step=0.1,
                advanced=True,
            ),
            field(
                "fade_out",
                "Fade out (s)",
                "number",
                default=1.5,
                min=0,
                max=10,
                step=0.1,
                advanced=True,
            ),
            field(
                "music_start",
                "Start the music at (s)",
                "number",
                default=0,
                min=0,
                max=600,
                step=0.5,
                advanced=True,
                help="Skip the start of the music track.",
            ),
        ],
    },
    {
        "id": "edit.concat",
        "group": "Edit",
        "label": "Join clips",
        "description": "Joins 2-10 videos and still images into one H.264 video with cuts or crossfades: one size "
        "and frame rate, silence for a clip without sound, sound at about -14 LUFS. Loads no model; seconds.",
        "fields": [
            field(
                "clips",
                "Videos and still images",
                "files",
                accept="video/*,image/*",
                min=2,
                max=10,
                required=True,
                help="Joined in upload order. The frame rate is the first video's.",
            ),
            field(
                "transition",
                "Transition",
                "select",
                default="cut",
                options=opts([("cut", "Cut"), ("crossfade", "Crossfade")]),
            ),
            field(
                "fade",
                "Crossfade length (s)",
                "number",
                default=0.5,
                min=0.1,
                max=3,
                step=0.1,
                help="Only with Crossfade; at most half the shortest clip.",
            ),
            field(
                "still_seconds",
                "Still image length (s)",
                "text",
                default="3",
                help=f"One number, or one per still in upload order (3, 2.5, 4). {STILL_MIN:g}-{STILL_MAX:g} s.",
            ),
            field(
                "size",
                "Size",
                "select",
                default="first",
                options=opts(CONCAT_SIZES),
            ),
            field(
                "fit",
                "Fit",
                "select",
                default="pad",
                options=opts(
                    [
                        ("pad", "Whole picture, black bars"),
                        ("crop", "Fill the frame, cut the edges"),
                    ]
                ),
                advanced=True,
            ),
            field(
                "loudness",
                "Even loudness",
                "checkbox",
                default=True,
                advanced=True,
                help="Off: the sound keeps its level.",
            ),
        ],
    },
    {
        "id": "edit.subtitle",
        "group": "Edit",
        "label": "Add Subtitles",
        "description": "Adds an SRT or WebVTT file to a video as a subtitle track that the player turns on or "
        "off. Picture and sound are copied. Loads no model; seconds.",
        "fields": [
            field(
                "video",
                "Video",
                "file",
                accept="video/*",
                required=True,
            ),
            field(
                "subtitles",
                "Subtitle file",
                "file",
                accept=".srt,.vtt",
                required=True,
                help="UTF-8. Transcribe writes one with Subtitles on.",
            ),
            field(
                "language",
                "Language",
                "select",
                default="und",
                options=opts(SUB_LANGUAGES),
            ),
        ],
    },
]
BY_ID = {t["id"]: t for t in TOOLS}


@dataclass
class Plan:
    """What the worker runs for a job: shell-free commands in order, an optional environment and the output names."""

    commands: list[list[str]]
    env: dict[str, str]
    cwd: Path | None = None


def num(
    params: dict[str, Any], name: str, default: float | None = None
) -> float | None:
    v = params.get(name)
    if v in (None, ""):
        return default
    return float(v)


def size(params: dict[str, Any]) -> tuple[int, int]:
    w, h = str(params.get("size", "768x1344")).split("x")
    return int(w), int(h)


def frames_for(seconds: float) -> int:
    """LTX needs 8k+1 frames at 24 fps."""
    return 8 * max(1, math.ceil((seconds * 24 - 1) / 8)) + 1


def has_audio(path: Path) -> bool:
    return bool(probe(path, "-select_streams", "a", "-show_entries", "stream=index"))


def stream_codec(path: Path, kind: str) -> str | None:
    """The codec of the first picture ("V") or sound ("a") stream, or None when there is none or the file cannot be
    read. "V" skips a cover picture."""
    try:
        out = run_media(
            [
                "ffprobe",
                "-v",
                "error",
                *GUARD,
                "-select_streams",
                f"{kind}:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(path),
            ],
            READ_SECONDS,
        ).stdout
    except ValueError:
        return None
    return out.strip() or None


def ebur128(path: Path, start: float = 0.0, seconds: float | None = None) -> str:
    """ffmpeg's EBU R128 log of a file's audio: a line every 100 ms, then the summary. With seconds: only the part
    from start on, with the file looped as the mix loops it."""
    loop, trim = [], ""
    if seconds is not None:
        loop = ["-stream_loop", "-1"]
        trim = f"atrim=start={start:.3f},asetpts=PTS-STARTPTS,atrim=0:{seconds:.3f},"
    return run_media(
        [
            "ffmpeg",
            "-nostats",
            *GUARD,
            *loop,
            "-i",
            str(path),
            "-vn",
            "-af",
            trim + "ebur128",
            "-f",
            "null",
            "-",
        ],
        PREP_SECONDS,
    ).stderr


def loudness(
    path: Path, start: float = 0.0, seconds: float | None = None
) -> float | None:
    """Integrated loudness (LUFS) of a file's audio, or None when it is silent or has none. With seconds: only the
    part from start on, with the file looped as the mix loops it."""
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", ebur128(path, start, seconds))
    value = float(found[-1]) if found else None
    return value if value is not None and value > -70 else None


def sound_levels(path: Path) -> tuple[float, float, float] | None:
    """From one EBU R128 pass: the integrated loudness (LUFS), the loudness range (LU) and the share of 400 ms
    windows more than 10 dB below the integrated loudness ("dips"). None when the audio is silent or missing."""
    err = ebur128(path)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    lufs = float(found[-1]) if found else None
    if lufs is None or lufs <= -70:
        return None
    lra = float(re.findall(r"LRA:\s+(-?[\d.]+) LU", err)[-1])
    # the first windows (before 0.4 s) are not full yet; a window of digital silence reads "nan" or "-inf"
    windows = [
        float(m) if m != "nan" else float("-inf")
        for t, m in re.findall(
            r"t:\s*([\d.]+)\s+TARGET:\S+ LUFS\s+M:\s*(-?[\d.]+|nan|-inf)", err
        )
        if float(t) >= 0.39
    ]
    dips = sum(m < lufs - 10 for m in windows) / len(windows) if windows else 0.0
    return lufs, lra, dips


def media_seconds(path: Path) -> float:
    out = probe(path, "-show_entries", "format=duration")
    try:
        return float(out)
    except ValueError:
        raise ValueError("Could not read the length of the input file.") from None


def video_size(path: Path) -> tuple[int, int]:
    out = probe(path, "-select_streams", "v:0", "-show_entries", "stream=width,height")
    try:
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except ValueError:
        raise ValueError("The input file has no video stream.") from None


def frame_count(path: Path) -> int:
    """The frames of the first video stream: the count in the header, or (when it has none) the decoded frames."""
    out = probe(path, "-select_streams", "v:0", "-show_entries", "stream=nb_frames")
    if not out.isdigit():
        out = probe(
            path,
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            seconds=PREP_SECONDS,
        )
    return int(out)


def check_extend_source(path: Path, end_at: float | None) -> float:
    """Checks the clip to extend and returns how many seconds of it are used. Raises ValueError (shown to the user)."""
    try:
        w, h = video_size(path)
        seconds = media_seconds(path)
    except (subprocess.CalledProcessError, ValueError, OSError):
        raise ValueError("The file could not be read as a video.") from None
    if w % 64 or h % 64:
        raise ValueError(
            f"The video is {w}x{h}; LTX extends only sizes that are a multiple of 64 on both sides."
        )
    if end_at is not None and end_at > seconds:
        raise ValueError(
            f"'Use the clip up to' is {end_at:.1f} s, but the clip is only {seconds:.1f} s long."
        )
    used = end_at if end_at is not None else seconds
    if used * 24 < EXTEND_MIN_FRAMES:
        raise ValueError(
            f"The clip must be at least {math.ceil(EXTEND_MIN_FRAMES / 2.4) / 10:.1f} s long (it is {used:.1f} s)."
        )
    return used


# the demuxers the transcription's ffmpeg step may use (audio and video files; nothing that reads other files)
SPEECH_DEMUXERS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,wav,w64,mp3,aac,flac,ogg,aiff,caf,asf,amr,mpeg,mpegts"
# the options before each input file of a job's other ffmpeg and ffprobe calls: the file protocol, and the audio, video
# and image demuxers only (no playlist or concat list that makes them read other files or the network)
GUARD = [
    "-protocol_whitelist",
    "file",
    "-format_whitelist",
    # image2 reads a .jpg or .png by its name (the app names every input file, so no pattern can expand)
    SPEECH_DEMUXERS
    + ",image2,png_pipe,jpeg_pipe,webp_pipe,gif,apng,bmp_pipe,tiff_pipe",
]
# time limits: one ffprobe or sips run (or a still for prompt help), and one ffmpeg step that prepares an input (an
# extend encodes the whole clip)
READ_SECONDS, PREP_SECONDS = 30, 600
SIPS = "/usr/bin/sips"
# the major brands of a HEIC or HEIF file ("ftyp" box): iPhone photos; ffmpeg fails on their tiled grid
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
# the pixel formats with an alpha channel (a palette can hold transparent colours)
ALPHA = re.compile(r"pal8|argb|rgba|abgr|bgra|yuva|ya|gbrap|ayuv|vuya|uyva")
# "[out#0/mp4 @ 0x...] ": the part of ffmpeg that wrote a line, noise to the user
FF_PREFIX = re.compile(r"\[[^\]]+ @ 0x[0-9a-f]+\]\s*")
# lines ffmpeg writes after the error that caused them: they do not name the cause
FF_FOLLOW_UP = re.compile(
    r"Error opening (input|output) files?\b|Error opening input: |Could not write header|"
    r"Error binding filtergraph|Error initializing (bitstream )?filter|Error submitting a packet to the muxer|"
    r"Error muxing a packet|Task finished with error code|Terminating thread with return code|"
    r"Last message repeated|Conversion failed|Nothing was written into output file|"
    r".+: Invalid argument$"
)


def ffmpeg_error(text: str) -> str:
    """The line of ffmpeg's error output that names the cause, without the [name @ 0x...] prefixes: the last line
    that is not a follow-up of an earlier one, else the first line ("" for no output)."""
    lines = [FF_PREFIX.sub("", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    causes = [line for line in lines if not FF_FOLLOW_UP.match(line)]
    cause = causes[-1] if causes else lines[0] if lines else ""
    if cause.startswith("Format not on whitelist"):
        return (
            "this file type is not supported (use a common image, audio or video file)"
        )
    return cause[:300]


def run_media(cmd: list[str], seconds: float) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg, ffprobe or sips with a time limit. Raises ValueError (shown to the user) when it is missing or
    takes too long."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=seconds, check=False
        )
    except FileNotFoundError:
        hint = (
            " Install it with Homebrew: brew install ffmpeg." if "ff" in cmd[0] else ""
        )
        raise ValueError(f"{Path(cmd[0]).name} was not found.{hint}") from None
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"{Path(cmd[0]).name} took more than {seconds:.0f} s on the input file. Use a shorter or smaller file."
        ) from None


def probe(path: Path, *entries: str, seconds: float = READ_SECONDS) -> str:
    """ffprobe's answer (CSV, no keys) about one file. Raises ValueError (shown to the user) when it cannot read it."""
    r = run_media(
        ["ffprobe", "-v", "error", *GUARD, *entries, "-of", "csv=p=0", str(path)],
        seconds,
    )
    if r.returncode != 0:
        why = ffmpeg_error(r.stderr) or f"exit code {r.returncode}"
        raise ValueError(f"Could not read the input file: {why}")
    return r.stdout.strip()


def prep(cmd: list[str]) -> None:
    """Run a quick ffmpeg step while a job is planned; a failure is shown to the user with ffmpeg's own words."""
    r = run_media(cmd, PREP_SECONDS)
    if r.returncode != 0:
        why = ffmpeg_error(r.stderr) or f"exit code {r.returncode}"
        raise ValueError(f"Could not prepare the input file: {why}")


def loudnorm_stats(cmd: list[str]) -> dict[str, str]:
    """Run a loudnorm first pass (print_format=json) while a job is planned and return what it measured."""
    err = run_media(cmd, PREP_SECONDS).stderr
    try:
        return json.loads(err[err.rindex("{") : err.rindex("}") + 1])
    except ValueError:
        why = ffmpeg_error(err) or "no measurement"
        raise ValueError(f"Could not measure the mix: {why}") from None


def is_heif(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS


def readable_image(src: Path, folder: Path, fmt: str = "png") -> Path:
    """The image as ffmpeg can read it: a HEIC or HEIF file becomes a PNG (or a JPEG for fmt "jpeg") in folder
    (macOS's sips keeps its colour profile); any other file is returned as it is. Raises ValueError (shown to the
    user)."""
    if not is_heif(src):
        return src
    dst = folder / f"{src.name}.{'jpg' if fmt == 'jpeg' else fmt}"
    r = run_media(
        [SIPS, "-s", "format", fmt, str(src), "--out", str(dst)], READ_SECONDS
    )
    if r.returncode != 0 or not dst.is_file():
        why = (r.stderr.strip().splitlines() or [f"exit code {r.returncode}"])[0]
        raise ValueError(f"Could not convert the HEIC image: {why}")
    return dst


def image_format(path: Path) -> tuple[int, int, str]:
    """Width, height and pixel format of an image (its first frame)."""
    out = probe(
        path, "-select_streams", "v:0", "-show_entries", "stream=width,height,pix_fmt"
    )
    try:
        w, h, fmt = out.split(",")[:3]
        return int(w), int(h), fmt
    except ValueError:
        raise ValueError("The input file is not an image.") from None


def image_filter(src: Path, vf: str) -> list[str]:
    """ffmpeg's filter options that apply vf to the image. An image with alpha is laid on white first: LTX and the LLM
    drop alpha, and the hidden colours of see-through pixels then show, mostly as dark edges. The white comes from a
    copy of the frame, not from the stored size: ffmpeg turns a frame upright by its EXIF orientation first, and a
    turned frame has the other size."""
    fmt = image_format(src)[2]
    if not ALPHA.match(fmt):
        return ["-vf", vf]
    flat = (
        "[0:v]format=rgba,split[fg][c];[c]format=rgb24,drawbox=c=white:t=fill[bg];"
        "[bg][fg]overlay=format=auto,format=rgb24"
    )
    return ["-filter_complex", f"{flat},{vf}"]


def on_white(src: Path, folder: Path) -> Path:
    """The image for a model that reads it itself and drops alpha (mflux): an image with alpha is laid on white into
    folder as <name>_white.png (an in_* name for an upload, so never an output); any other image is returned as it
    is. One frame: the first of an animated GIF."""
    flat = image_filter(src, "null")
    if flat[0] == "-vf":
        return src
    dst = folder / f"{src.stem}_white.png"
    prep(
        ["ffmpeg", "-v", "error", "-y", *GUARD, "-i", str(src), *flat, "-frames:v", "1", "-update", "1", str(dst)]
    )  # fmt: skip
    return dst


def fit_image(src: Path, dst: Path, w: int, h: int) -> None:
    """Scale to cover, then centre-crop to exactly w x h. One frame: the first of an animated GIF."""
    src = readable_image(src, dst.parent)
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    )
    prep(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *GUARD,
            "-i",
            str(src),
            *image_filter(src, vf),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(dst),
        ]
    )


# the colour tags of an LTX video, written into its H.264 header: BT.709 primaries and transfer, the BT.601 matrix of
# LTX's encode, limited range (ffmpeg 9 does not write -color_primaries/-color_trc for libx264; the filter does)
LTX_COLOURS = "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=6"


def finish_video(path: Path) -> list[str]:
    """The step after LTX writes a video: tag its colours and put its index first (app/runners/tag_video.py)."""
    return [sys.executable, str(APP / "runners" / "tag_video.py"), str(path)]


@dataclass
class Clip:
    """One clip of a join: a video, or a still image shown for `seconds`."""

    path: Path
    still: bool
    seconds: float


def video_shape(path: Path) -> tuple[int, int, str]:
    """The upright size of the first picture stream (a phone clip's rotation turned) and its frame rate as ffprobe
    gives it ("30000/1001"; "0/0" for a still)."""
    out = probe(
        path,
        "-select_streams",
        "V:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate:stream_side_data=rotation",
    )
    try:
        w, h, rate, *rest = out.split("\n")[0].split(",")
        turned = bool(rest and rest[0].strip() and abs(int(float(rest[0]))) % 180 == 90)
        return (int(h), int(w), rate) if turned else (int(w), int(h), rate)
    except ValueError:
        raise ValueError("The file has no picture.") from None


def frame_rate(rate: str) -> float:
    """ffprobe's "30000/1001" as a number (0 when it is missing)."""
    num_, _, den = rate.partition("/")
    try:
        return float(num_) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def still_seconds(text: str, stills: int) -> list[float]:
    """The length of each still image: one number for all, or one for each. Raises ValueError (shown to the user)."""
    parts = [p for p in re.split(r"[,;\s]+", text.strip()) if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(
            "Still image length: give numbers of seconds, such as 3 or 3, 2.5, 4."
        ) from None
    if not values:
        values = [3.0]
    if len(values) == 1:
        values = values * stills
    if len(values) != stills:
        raise ValueError(
            f"Still image length: give one number, or one for each of the {stills} still images "
            f"({len(values)} given)."
        )
    for v in values:
        if not STILL_MIN <= v <= STILL_MAX:
            raise ValueError(
                f"Still image length: {v:g} s is out of range ({STILL_MIN:g}-{STILL_MAX:g} s)."
            )
    return values


def concat_clips(params: dict[str, Any], files: dict[str, list[Path]]) -> list[Clip]:
    """The clips of a join with their lengths, checked: 2 or more, each a readable video or image, and a crossfade at
    most half the shortest clip. Raises ValueError (shown to the user)."""
    paths = files.get("clips", [])
    if len(paths) < 2:
        raise ValueError("Add at least 2 clips (videos or still images).")
    stills = [p.suffix.lower() in IMAGE_SUFFIXES for p in paths]
    lengths = iter(still_seconds(str(params.get("still_seconds") or ""), sum(stills)))
    clips = []
    for i, (path, still) in enumerate(zip(paths, stills, strict=True), 1):
        # a HEIC photo is read after macOS's sips has converted it (readable_image)
        heif = path.suffix.lower() in (".heic", ".heif")
        if not heif and stream_codec(path, "V") is None:
            raise ValueError(f"Clip {i} has no picture, or it could not be read.")
        if still:
            clips.append(Clip(path, True, next(lengths)))
            continue
        clips.append(Clip(path, False, media_seconds(path)))
    if str(params.get("transition")) == "crossfade":
        fade = num(params, "fade", 0.5) or 0.5
        short = min(clips, key=lambda c: c.seconds)
        if fade > short.seconds / 2:
            raise ValueError(
                f"The crossfade ({fade:g} s) must be at most half the shortest clip (clip "
                f"{clips.index(short) + 1} is {short.seconds:.1f} s)."
            )
    return clips


def concat_size(
    params: dict[str, Any], clips: list[Clip], folder: Path
) -> tuple[int, int]:
    """The joined video's size: the chosen one, or the first clip's upright size made even."""
    if str(params.get("size") or "first") != "first":
        return size(params)
    first = clips[0]
    src = readable_image(first.path, folder) if first.still else first.path
    w, h, _ = video_shape(src)
    if max(w, h) > CONCAT_MAX_SIDE:
        raise ValueError(
            f"The first clip is {w}x{h}; choose a size (at most {CONCAT_MAX_SIDE} on each side)."
        )
    return max(2, w - w % 2), max(2, h - h % 2)


def subtitle_cues(path: Path) -> list[subtitles.Cue]:
    """The cues of an uploaded subtitle file. Raises ValueError (shown to the user)."""
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError("The subtitle file could not be read.") from None
    return subtitles.parse(subtitles.decode(data))


def validate(tool: str, params: dict[str, Any], files: dict[str, list[Path]]) -> None:
    """Checks that need more than the field rules, run when a job is submitted. Raises ValueError (shown to the user)."""
    missing = [p for p in REQUIRES.get(tool, []) if not p.exists()]
    if missing:
        raise ValueError(
            f"Not installed: {', '.join(map(str, missing))}. Open Models to install it, "
            "or set its folder in Settings > Model paths."
        )
    # a text field with needs_off is used only while that checkbox is off (ACE-Step sings no lyrics in an
    # instrumental): the job is refused instead of dropping the text
    fields = {f["name"]: f for f in BY_ID[tool]["fields"]} if tool in BY_ID else {}
    for f in fields.values():
        box = fields.get(f.get("needs_off", ""))
        text = str(params.get(f["name"]) or "").strip()
        if box and text and str(params.get(box["name"])).lower() == "true":
            raise ValueError(
                f"{f['label']}: used only when {box['label']} is off. Turn {box['label']} off, or clear "
                f"{f['label']}."
            )
    if tool == "voice.drama3":
        for i in (0, 1):
            if (
                files.get(f"ref{i}")
                and not str(params.get(f"ref{i}_text") or "").strip()
            ):
                raise ValueError(f"Add the transcript of voice reference {i}.")
    if (
        tool == "voice.transcribe"
        and files.get("audio")
        and not has_audio(files["audio"][0])
    ):
        raise ValueError("The file has no sound track.")
    if tool == "video.a2v" and files.get("audio"):
        try:
            speech = media_seconds(files["audio"][0])
        except (subprocess.CalledProcessError, ValueError, OSError):
            raise ValueError("The voice file could not be read as audio.") from None
        if not A2V_MIN_SECONDS <= speech <= A2V_MAX_SECONDS:
            raise ValueError(
                f"The voice audio must be {A2V_MIN_SECONDS}-{A2V_MAX_SECONDS} s long (it is {speech:.1f} s)."
            )
    if tool == "video.extend" and files.get("video"):
        check_extend_source(files["video"][0], num(params, "end_at"))
    if tool == "edit.mix":
        if files.get("video") and stream_codec(files["video"][0], "V") is None:
            raise ValueError("The video file has no picture, or it could not be read.")
        if files.get("music") and stream_codec(files["music"][0], "a") is None:
            raise ValueError("The music file has no sound, or it could not be read.")
    if tool == "edit.concat":
        concat_clips(params, files)
    if tool == "edit.subtitle":
        if files.get("video") and stream_codec(files["video"][0], "V") is None:
            raise ValueError("The video file has no picture, or it could not be read.")
        if files.get("subtitles"):
            subtitle_cues(files["subtitles"][0])


def plan(
    tool: str, params: dict[str, Any], files: dict[str, list[Path]], out: Path
) -> Plan:
    """Turn a job into commands. Raises ValueError for bad input (shown to the user)."""
    seed = num(params, "seed")
    seed_args = ["--seed", str(int(seed))] if seed is not None else []
    offline = {"HF_HUB_OFFLINE": "1"}

    if tool == "image.moss":
        w, h = size(params)
        return Plan(
            [
                [
                    str(MFLUX / "mflux-generate-moss-image"),
                    "-q",
                    "8",
                    "--width",
                    str(w),
                    "--height",
                    str(h),
                    "--steps",
                    str(int(num(params, "steps", 50))),
                    "--guidance",
                    str(num(params, "guidance", 4.0)),
                    "--negative-prompt=" + str(params.get("negative", "")),
                    "--prompt=" + str(params["prompt"]),
                    *seed_args,
                    "--output",
                    str(out / "image.png"),
                ]
            ],
            offline,
        )
    if tool == "image.klein":
        w, h = size(params)
        return Plan(
            [
                [
                    str(MFLUX / "mflux-generate-flux3"),
                    "-q",
                    "8",
                    "--width",
                    str(w),
                    "--height",
                    str(h),
                    "--steps",
                    str(int(num(params, "steps", 16))),
                    "--prompt=" + str(params["prompt"]),
                    *seed_args,
                    "--output",
                    str(out / "image.png"),
                ]
            ],
            offline,
        )
    if tool == "image.klein_edit":
        refs = files.get("references", [])
        if not refs:
            raise ValueError("Add at least one reference image.")
        # mflux drops alpha itself: the hidden colours of see-through pixels would show
        refs = [on_white(readable_image(r, out), out) for r in refs]
        w, h = size(params)
        return Plan(
            [
                [
                    str(MFLUX / "mflux-generate-flux3-edit"),
                    "-q",
                    "8",
                    "--width",
                    str(w),
                    "--height",
                    str(h),
                    "--image-paths",
                    *map(str, refs),
                    "--prompt=" + str(params["prompt"]),
                    *seed_args,
                    "--output",
                    str(out / "image.png"),
                ]
            ],
            offline,
        )
    if tool == "voice.drama3":
        spec = out / "voice_job.json"
        refs = [
            (
                i,
                files.get(f"ref{i}", [None])[0],
                str(params.get(f"ref{i}_text") or "").strip(),
            )
            for i in (0, 1)
        ]
        # each voice reference becomes a WAV here, through the input whitelist: the runner's audio loader would
        # otherwise open the upload with its own unguarded ffmpeg
        for k, (i, src, text) in enumerate(refs):
            if src is not None:
                wav = out / f"in_ref{i}_voice.wav"
                prep(
                    ["ffmpeg", "-v", "error", "-y", *GUARD, "-i", str(src), "-vn",
                     "-c:a", "pcm_s24le", str(wav)]
                )  # fmt: skip
                refs[k] = (i, wav, text)
        spec.write_text(
            json.dumps(
                {
                    "text": str(params["text"]),
                    "takes": int(num(params, "takes", 2)),
                    "seed": seed,
                    "model": str(DRAMA_MODEL),
                    "temperature": num(params, "temperature", 1.0),
                    "top_p": num(params, "top_p", 0.9),
                    "top_k": int(num(params, "top_k", 30)),
                    "refs": [
                        {"audio": str(a), "text": t, "speaker": i}
                        for i, a, t in refs
                        if a is not None
                    ],
                    "out": str(out),
                }
            )
        )
        return Plan(
            [[str(DRAMA_PY), str(APP / "runners" / "drama3.py"), str(spec)]], offline
        )
    if tool == "voice.transcribe":
        src = files.get("audio", [None])[0]
        if src is None:
            raise ValueError("Add an audio or video file.")
        speech = out / "speech.wav"
        prep(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                # the file protocol and the audio and video demuxers only: no playlist or concat list that makes
                # ffmpeg read other files or the network
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                SPEECH_DEMUXERS,
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(speech),
            ]
        )
        spec = out / "transcribe_job.json"
        language = str(params.get("language") or "auto")
        subs = str(params.get("subtitles", "false")).lower() == "true"
        # the aligner only when it is in the Hugging Face cache: the job runs offline
        aligner = (
            SUB_ALIGNER
            if subs
            and SUB_ALIGNER
            and (HF_HUB / f"models--{SUB_ALIGNER.replace('/', '--')}").is_dir()
            else None
        )
        spec.write_text(
            json.dumps(
                {
                    "audio": str(speech),
                    "language": None if language == "auto" else language,
                    "subtitles": subs,
                    "aligner": aligner,
                    "out": str(out),
                }
            )
        )
        return Plan(
            [[str(DRAMA_PY), str(APP / "runners" / "transcribe.py"), str(spec)]],
            offline,
        )
    if tool == "music.ace":
        spec = out / "music_job.json"
        spec.write_text(
            json.dumps(
                {
                    "caption": str(params["caption"]),
                    "instrumental": str(params.get("instrumental", "true")).lower()
                    == "true",
                    "lyrics": str(params.get("lyrics", "")),
                    "duration": num(params, "duration", 30),
                    "bpm": num(params, "bpm"),
                    "key": str(params.get("key", "")),
                    "takes": int(num(params, "takes", 2)),
                    "seed": seed,
                    "out": str(out),
                }
            )
        )
        return Plan(
            [[str(ACE_PY), str(APP / "runners" / "ace.py"), str(spec)]],
            offline,
            cwd=ACE_DIR,
        )
    if tool in ("video.t2v", "video.i2v"):
        w, h = size(params)
        frames = frames_for(num(params, "seconds", 5) or 5)
        cmd = [
            str(LTX),
            "generate",
            "--distilled",
            "--model",
            str(LTX_MODEL),
            "--prompt=" + str(params["prompt"]),
            "-W",
            str(w),
            "-H",
            str(h),
            "-f",
            str(frames),
            "--frame-rate",
            "24",
            *seed_args,
            "-o",
            str(out / "video.mp4"),
        ]
        if str(params.get("audio", "true")).lower() != "true":
            cmd.append("--no-audio")
        if str(params.get("hq_decoder", "false")).lower() == "true":
            cmd += ["--video-decoder", "diffusion"]
        if tool == "video.i2v":
            img = files.get("image", [None])[0]
            if img is None:
                raise ValueError("Add a start image.")
            fitted = out / "start.png"
            fit_image(img, fitted, w, h)
            cmd += ["--image", str(fitted)]
        return Plan(
            [cmd, finish_video(out / "video.mp4")],
            offline | {"LTX2_COMPUTE_DTYPE": "float16"},
        )
    if tool == "video.a2v":
        img, aud = files.get("image", [None])[0], files.get("audio", [None])[0]
        if img is None or aud is None:
            raise ValueError("Add a person image and a voice audio file.")
        w, h = size(params)
        speech = media_seconds(aud)
        if not A2V_MIN_SECONDS <= speech <= A2V_MAX_SECONDS:
            raise ValueError(
                f"The voice audio must be {A2V_MIN_SECONDS}-{A2V_MAX_SECONDS} s long (it is {speech:.1f} s)."
            )
        # 0.25 s before the voice and 0.5 s after it, then enough silence that the audio covers the whole clip
        frames = frames_for(speech + 0.75)
        padded, fitted = out / "voice_padded.wav", out / "person.png"
        prep(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *GUARD,
                "-i",
                str(aud),
                "-af",
                f"adelay=250:all=1,apad=whole_dur={frames / 24 + 0.3:.3f}",
                "-ar",
                "48000",
                str(padded),
            ]
        )
        fit_image(img, fitted, w, h)
        prompt = f"{str(params['prompt']).strip()} {str(params.get('style', A2V_STYLE)).strip()}".strip()
        return Plan(
            [
                [
                    str(LTX),
                    "a2v",
                    "--distilled",
                    "--model",
                    str(LTX_MODEL),
                    "--audio",
                    str(padded),
                    "-i",
                    str(fitted),
                    "--prompt=" + prompt,
                    "-W",
                    str(w),
                    "-H",
                    str(h),
                    "-f",
                    str(frames),
                    "--frame-rate",
                    "24",
                    *seed_args,
                    "-o",
                    str(out / "video.mp4"),
                ],
                finish_video(out / "video.mp4"),
            ],
            offline | {"LTX2_COMPUTE_DTYPE": "float16"},
        )
    if tool == "video.extend":
        src = files.get("video", [None])[0]
        if src is None:
            raise ValueError("Add a video.")
        used = check_extend_source(src, num(params, "end_at"))
        source, tail, raw = (
            out / "source.mp4",
            out / "tail.mp4",
            out / "extended_raw.mp4",
        )
        # the clip up to `used`, at 24 fps, with a 48 kHz track (silence when it has none) as long as the picture
        audio_in = (
            ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            if not has_audio(src)
            else []
        )
        prep(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *GUARD,
                "-i",
                str(src),
                *audio_in,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0" if audio_in else "0:a:0",
                "-vf",
                "fps=24,setsar=1",
                "-af",
                "aresample=48000,apad",
                "-t",
                f"{used:.3f}",
                *VIDEO_ENC,
                str(source),
            ]
        )
        n = frame_count(source)
        if n < EXTEND_MIN_FRAMES:  # the container can be longer than its video stream
            raise ValueError(
                f"The clip has only {n} video frames; extending needs at least {EXTEND_MIN_FRAMES}."
            )
        # The model reads 8k+1 frames from the start of the file it gets, so the tail is exactly that long and the
        # new part continues from the clip's real last frame.
        carry = 8 * ((min(n, EXTEND_CARRY_MAX) - 1) // 8) + 1
        start = n - carry
        prep(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *GUARD,
                "-i",
                str(source),
                "-vf",
                f"trim=start_frame={start},setpts=PTS-STARTPTS",
                "-af",
                f"atrim=start={start / 24:.6f},asetpts=PTS-STARTPTS",
                *VIDEO_ENC,
                str(tail),
            ]
        )
        latent = math.ceil(
            min(num(params, "seconds", 5) or 5, EXTEND_MAX_SECONDS) * 3
        )  # 8 new frames each
        extend = [
            str(LTX_PY),
            str(APP / "runners" / "ltx_extend.py"),
            "extend",
            "--distilled",
            "--model",
            str(LTX_MODEL),
            "--prompt=" + str(params["prompt"]),
            "--video",
            str(tail),
            "--extend-frames",
            str(latent),
            *seed_args,
            "-o",
            str(raw),
        ]
        # the model's file is the tail again (re-decoded) and then the new frames: keep the clip, add only the new part
        # (the clip's sound is cut to its picture, so the new part's sound stays in sync after the join)
        join = (
            f"[0:a]apad,atrim=end={n / 24:.6f}[ca];"
            f"[1:v]trim=start_frame={carry},setpts=PTS-STARTPTS,setsar=1[nv];"
            f"[1:a]atrim=start={carry / 24:.6f},asetpts=PTS-STARTPTS,aresample=48000[na];"
            "[0:v][ca][nv][na]concat=n=2:v=1:a=1[v][a]"
        )
        splice = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *GUARD,
            "-i",
            str(source),
            *GUARD,
            "-i",
            str(raw),
            "-filter_complex",
            join,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-bsf:v",
            LTX_COLOURS,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out / "video.mp4"),
        ]
        (out / "notes.json").write_text(
            json.dumps(
                {
                    "video.mp4": f"{n / 24:.1f} s of the clip + {8 * latent / 24:.1f} s new"
                }
            )
        )
        return Plan([extend, splice], offline | {"LTX2_COMPUTE_DTYPE": "float16"})
    if tool == "edit.mix":
        video, music = files.get("video", [None])[0], files.get("music", [None])[0]
        if video is None or music is None:
            raise ValueError("Add a video and a music file.")
        dur = media_seconds(video)
        under = num(params, "under", 14) or 14
        fade_in, fade_out = (
            num(params, "fade_in", 0.5) or 0,
            num(params, "fade_out", 1.5) or 0,
        )
        start = num(params, "music_start", 0) or 0
        # the music is measured only on the part the mix uses (a soft intro must not set the level of the song)
        music_lufs = loudness(music, start, dur)
        if music_lufs is None:
            raise ValueError("The music file has no audible sound.")
        levels = sound_levels(video) if has_audio(video) else None
        voice_lufs = levels[0] if levels else None
        speech = bool(
            levels
            and levels[0] >= MIX_SPEECH_LUFS
            and (levels[2] >= MIX_SPEECH_DIPS or levels[1] >= MIX_SPEECH_LRA)
        )
        duck = speech and str(params.get("duck", "true")).lower() == "true"
        # music level: `under` dB below speech; a fixed step below a steady sound (rain, effects); a normal bed level
        # when the video's own sound is near silent or missing
        if speech:
            target = voice_lufs - under
        elif voice_lufs is not None and voice_lufs >= MIX_SPEECH_LUFS:
            target = voice_lufs - MIX_AMBIENCE_UNDER
        else:
            target = -16.0
        gain = min(target - music_lufs, MIX_MAX_GAIN)
        fades = (f",afade=t=in:d={fade_in:.2f}" if fade_in >= 0.01 else "") + (
            f",afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.2f}"
            if fade_out >= 0.01
            else ""
        )  # afade with d=0 would fade over its default 44100 samples
        bed = (
            f"[1:a]atrim=start={start:.3f},asetpts=PTS-STARTPTS,atrim=0:{dur:.3f},aresample=48000,"
            f"aformat=channel_layouts=stereo,volume={gain:.2f}dB{fades}"
        )
        if voice_lufs is None:
            graph = f"{bed}[mix]"
        else:
            # stereo, and as long as the picture, so a short or mono sound track does not cut or narrow the mix
            voice = f"[0:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},asplit=2[voice][key]"
            if duck:
                # the key is scaled to a -16 LUFS voice, so the music dips by the same amount (about 5 dB) at any
                # recording level (80 ms attack, 400 ms release)
                level = min(64.0, max(0.015625, 10 ** ((-16 - voice_lufs) / 20)))
                graph = (
                    f"{voice};{bed}[bed];[bed][key]sidechaincompress=threshold=0.05:ratio=2:attack=80:release=400:"
                    f"level_sc={level:.4f}[ducked];[voice][ducked]amix=inputs=2:normalize=0:duration=first[mix]"
                )
            else:
                graph = f"{voice};{bed}[bed];[key]anullsink;[voice][bed]amix=inputs=2:normalize=0:duration=first[mix]"
        inputs = [
            *GUARD,
            "-i",
            str(video),
            *GUARD,
            "-stream_loop",
            "-1",
            "-i",
            str(music),
        ]
        # two-pass loudness: the first pass measures the mix now; the job's command uses that measurement and keeps one
        # steady gain when the peaks and the loudness range allow it (else loudnorm rides the level as before)
        stats = loudnorm_stats(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                *inputs,
                "-filter_complex",
                f"{graph};[mix]{MIX_LOUDNORM}:print_format=json[out]",
                "-map",
                "[out]",
                "-t",
                f"{dur:.3f}",
                "-f",
                "null",
                "-",
            ]
        )
        measured = (
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true"
        )
        # an MP4 cannot hold every codec as it is (ProRes): such a picture is encoded again
        codec = stream_codec(video, "V")
        if codec in MIX_COPY_CODECS:
            picture = ["-c:v", "copy", *(["-tag:v", "hvc1"] if codec == "hevc" else [])]
        else:
            picture = ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            f"{graph};[mix]{MIX_LOUDNORM}:{measured},aresample=48000[out]",
            "-map",
            "0:V:0",
            "-map",
            "[out]",
            *picture,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{dur:.3f}",
            "-movflags",
            "+faststart",
            str(out / "mixed.mp4"),
        ]
        if speech:
            note = (
                f"speech found ({voice_lufs:.1f} LUFS): music {under:.0f} dB under it, "
                + ("ducked" if duck else "not ducked")
            )
        elif voice_lufs is not None and voice_lufs >= MIX_SPEECH_LUFS:
            note = f"no speech, steady sound ({voice_lufs:.1f} LUFS): music {MIX_AMBIENCE_UNDER:.0f} dB under the sound"
        elif voice_lufs is not None:
            note = f"no speech, near-silent sound ({voice_lufs:.1f} LUFS): music at -16 LUFS"
        else:
            note = "no sound in the video: music at -16 LUFS"
        if gain == MIX_MAX_GAIN:
            note += f"; the music is very quiet: raised by only {MIX_MAX_GAIN:.0f} dB"
        if codec not in MIX_COPY_CODECS:
            note += f"; picture ({codec}) encoded again as H.264"
        # the last step measures the encoded mix and adds its loudness to this note
        (out / "notes.json").write_text(json.dumps({"mixed.mp4": note}))
        measure = [
            sys.executable,
            str(APP / "runners" / "mix_level.py"),
            str(out / "mixed.mp4"),
        ]
        return Plan([cmd, measure], {})
    if tool == "edit.concat":
        return plan_concat(params, files, out)
    if tool == "edit.subtitle":
        return plan_subtitle(params, files, out)
    raise ValueError(f"Unknown tool {tool!r}")


def plan_concat(
    params: dict[str, Any], files: dict[str, list[Path]], out: Path
) -> Plan:
    """edit.concat: each clip scaled (and padded or cropped) to one size and frame rate, with a stereo 48 kHz track
    (silence for a clip without sound), then joined with cuts (concat) or crossfades (xfade and acrossfade). The
    sound gets a two-pass loudness step as in edit.mix."""
    clips = concat_clips(params, files)
    w, h = concat_size(params, clips, out)
    rates = [video_shape(c.path)[2] for c in clips if not c.still][:1]
    rate = rates[0] if rates and 1 <= frame_rate(rates[0]) <= 120 else "24"
    fps = frame_rate(rate)
    crossfade = str(params.get("transition")) == "crossfade"
    fade = (num(params, "fade", 0.5) or 0.5) if crossfade else 0.0
    # BT.601 matrix, limited range for the video: the colours that LTX_COLOURS names (as in the LTX videos)
    colour = ":out_color_matrix=bt601:out_range=tv"
    if str(params.get("fit") or "pad") == "crop":
        shape = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos{{}},crop={w}:{h}"
    else:
        shape = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos{{}},"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    inputs: list[str] = []
    video: list[str] = []
    sound: list[str] = []
    silent: list[int] = []
    lengths: list[float] = []
    for i, clip in enumerate(clips):
        # whole frames, so the crossfade offsets and the sound match the picture
        seconds = max(1, round(clip.seconds * fps)) / fps
        lengths.append(seconds)
        if clip.still:
            # the image as one frame of the video's size (an image with alpha laid on white), then looped
            src = readable_image(clip.path, out)
            still = out / f"in_clips_{i}_still.png"
            prep(
                ["ffmpeg", "-v", "error", "-y", *GUARD, "-i", str(src), *image_filter(src, shape.format("")),
                 "-frames:v", "1", "-update", "1", str(still)]
            )  # fmt: skip
            inputs += [*GUARD, "-loop", "1", "-framerate", rate, "-t", f"{seconds:.6f}", "-i", str(still)]  # fmt: skip
        else:
            inputs += [*GUARD, "-i", str(clip.path)]
        video.append(
            f"[{i}:V:0]setpts=PTS-STARTPTS,fps={rate},{shape.format(colour)},setsar=1,"
            f"format=yuv420p,tpad=stop_mode=clone:stop_duration={seconds:.6f},trim=end={seconds:.6f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        if not clip.still and has_audio(clip.path):
            source = f"[{i}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
        else:
            source = "anullsrc=r=48000:cl=stereo,"
            silent.append(i + 1)
        sound.append(
            f"{source}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad,"
            f"atrim=end={seconds:.6f}[a{i}]"
        )
    n = len(clips)
    if crossfade:
        at = lengths[0]
        for i in range(1, n):
            at -= fade
            v_in, a_in = ("v0", "a0") if i == 1 else (f"x{i - 1}", f"y{i - 1}")
            v_out, a_out = ("v", "a") if i == n - 1 else (f"x{i}", f"y{i}")
            video.append(
                f"[{v_in}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={at:.6f}[{v_out}]"
            )
            sound.append(
                f"[{a_in}][a{i}]acrossfade=d={fade:.3f}:c1=tri:c2=tri[{a_out}]"
            )
            at += lengths[i]
        graph = ";".join(video + sound)
        audio_graph = ";".join(sound)
    else:
        pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
        graph = ";".join([*video, *sound, f"{pairs}concat=n={n}:v=1:a=1[v][a]"])
        sounds = "".join(f"[a{i}]" for i in range(n))
        audio_graph = ";".join([*sound, f"{sounds}concat=n={n}:v=0:a=1[a]"])
    total = sum(lengths) - fade * (n - 1)
    note = f"{n} clips, " + (f"crossfades of {fade:g} s" if crossfade else "cuts")
    note += f", {w}x{h} at {fps:.3g} fps, {total:.1f} s"
    if silent:
        note += f"; silence for clip {', '.join(map(str, silent))}"
    finish = "[a]anull[out]"
    measure = []
    if len(silent) == n:
        note += "; no sound"
    elif str(params.get("loudness", "true")).lower() == "true":
        # two-pass loudness: the first pass measures the joined sound now (its own graph, without the picture)
        stats = loudnorm_stats(
            ["ffmpeg", "-hide_banner", "-nostats", *inputs, "-filter_complex",
             f"{audio_graph};[a]{MIX_LOUDNORM}:print_format=json[out]", "-map", "[out]", "-f", "null", "-"]
        )  # fmt: skip
        try:
            level = float(stats["input_i"])
        except (KeyError, ValueError):
            level = float("-inf")
        if level <= -70:
            note += "; the sound is silent: left as it is"
        elif -14 - level > MIX_MAX_GAIN:
            note += f"; the sound is very quiet ({level:.1f} LUFS): left as it is"
        else:
            finish = (
                f"[a]{MIX_LOUDNORM}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"offset={stats['target_offset']}:linear=true,aresample=48000[out]"
            )
            measure = [[sys.executable, str(APP / "runners" / "mix_level.py"), str(out / "joined.mp4")]]  # fmt: skip
    cmd = [
        "ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", f"{graph};{finish}",
        "-map", "[v]", "-map", "[out]", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
        "-bsf:v", LTX_COLOURS, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.6f}",
        "-movflags", "+faststart", str(out / "joined.mp4"),
    ]  # fmt: skip
    (out / "notes.json").write_text(json.dumps({"joined.mp4": note}))
    return Plan([cmd, *measure], {})


def plan_subtitle(
    params: dict[str, Any], files: dict[str, list[Path]], out: Path
) -> Plan:
    """edit.subtitle: the video's picture and sound copied (encoded again only when an MP4 cannot hold them) with
    the subtitle file as a mov_text track. The file is read here and written again as a clean SRT (UTF-8, tags other
    than <i>, <b> and <u> removed, cues past the video's end left out)."""
    video, sub = files.get("video", [None])[0], files.get("subtitles", [None])[0]
    if video is None or sub is None:
        raise ValueError("Add a video and a subtitle file.")
    length = media_seconds(video)
    cues = subtitle_cues(sub)
    kept = [c for c in cues if c.start < length]
    if not kept:
        raise ValueError(
            f"Every subtitle starts after the video ends ({length:.1f} s). Check that the file is for this video."
        )
    for c in kept:
        c.end = min(c.end, length)
    clean = out / "in_subtitles_clean.srt"
    clean.write_text(subtitles.srt(kept))
    codec = stream_codec(video, "V")
    if codec in MIX_COPY_CODECS:
        picture = ["-c:v", "copy", *(["-tag:v", "hvc1"] if codec == "hevc" else [])]
    else:
        picture = ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"]
    sound = stream_codec(video, "a")
    audio = (
        []
        if sound is None
        else ["-c:a", "copy"]
        if sound in SUB_COPY_AUDIO
        else ["-c:a", "aac", "-b:a", "192k"]
    )
    language = str(params.get("language") or "und")
    if language not in {v for v, _ in SUB_LANGUAGES}:
        raise ValueError(f"Unknown subtitle language {language!r}.")
    cmd = [
        "ffmpeg", "-v", "error", "-y", *GUARD, "-i", str(video),
        "-protocol_whitelist", "file", "-format_whitelist", "srt", "-f", "srt", "-i", str(clean),
        "-map", "0:V:0", "-map", "0:a?", "-map", "1:0", *picture, *audio,
        "-c:s", "mov_text", "-metadata:s:s:0", f"language={language}",
        "-movflags", "+faststart", str(out / "subtitled.mp4"),
    ]  # fmt: skip
    note = f"{len(kept)} subtitles"
    if language != "und":
        note += f", language {dict(SUB_LANGUAGES)[language]}"
    if len(kept) < len(cues):
        note += f"; {len(cues) - len(kept)} after the video's end left out"
    if codec not in MIX_COPY_CODECS:
        note += f"; picture ({codec}) encoded again as H.264"
    (out / "notes.json").write_text(json.dumps({"subtitled.mp4": note}))
    return Plan([cmd], {})


if __name__ == "__main__":
    json.dump(TOOLS, sys.stdout, indent=1)
