"""Live progress of a running job: its stage, how far along it is (0..1) and the seconds left.

The signal is each model's own output in job.log: tqdm bars, LTX's stage headers, ACE-Step's log lines and the
Drama 3 runner's line per take. tqdm redraws its bar after a carriage return, so job.log is read as bytes (reading it
as text turns every \\r into a new line). A field stays None until its signal appears: the UI shows nothing rather
than a made-up number.

The time left blends the live rate with the history: finished jobs of the same tool and the settings that drive its
time (cost()). Early in a job the history counts most; the further the job gets, the more its own rate counts.
"""

import re
import statistics
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.tools import num

# "Denoising (ancestral):  38%|███▊      | 3/8 [00:20<00:33,  6.62s/it]"; mflux bars have no description
BAR = re.compile(r"(?P<desc>.*?):?\s*\d+%\|[^|]*\|\s*(?P<n>\d+)/(?P<total>\d+) \[")
# printed by LTX before each denoising stage, with the stage's size (the time of a stage follows its tokens)
LTX_HEADER = re.compile(
    r"\[estimate\] denoising [^:]*: (\d+) steps x \d+ passes over (\d+) video"
)
TAKE = re.compile(r"take (\d+) seed \d+: [\d.]+ s, (.*?) \|")
PART = re.compile(
    r"part (\d+)/(\d+):"
)  # printed by the Transcribe runner after each part
# printed by the Read Text runners after each page: "render i/n:" (pages.py), then "page i/n:" (ocr.py)
OCR_PAGE = re.compile(r"(render|page) (\d+)/(\d+):")
# speaker tags and cues in a Drama 3 script
SPOKEN = re.compile(r"<\|[^|]*\|>|\[[^\]]*\]")

# Share of a whole job's time per stage, in run order. Measured 2026-11-02 on an M5 Pro from the finished jobs in
# outputs/: stage times from LTX's "done in" lines and the last elapsed time of each tqdm bar, against the job's
# elapsed time; the upscale is what is left between the two bars, and in an extend the source clip is encoded
# before stage 1 (its loading share is larger). Medians of 5 standard text/image-to-video jobs, 7 extends and the
# 2 jobs with the diffusion decoder (both 704x1280, 5 s).
LTX_WEIGHTS = {
    "standard": {
        "load": 0.06,
        "stage1": 0.27,
        "upscale": 0.03,
        "stage2": 0.48,
        "decode": 0.16,
    },
    "extend": {
        "load": 0.09,
        "stage1": 0.26,
        "upscale": 0.02,
        "stage2": 0.46,
        "decode": 0.17,
    },
    "hq": {
        "load": 0.015,
        "stage1": 0.105,
        "upscale": 0.01,
        "stage2": 0.195,
        "decode": 0.675,
    },
}
LTX_LABELS = {
    "load": "Loading",
    "stage1": "Stage 1",
    "upscale": "Upscaling",
    "stage2": "Stage 2",
    "decode": "Decoding",
}
# mflux loads the model before its bar starts and saves the image after it: the job's time outside the bar
# (5-9 s), median share: Klein at 16 steps, MOSS at 50 steps, Klein Edit with one reference
MFLUX_LOAD = {"image.klein": 0.16, "image.moss": 0.03, "image.klein_edit": 0.065}
# from the timestamps of ACE-Step's log lines in 2 jobs (10 s x 1 take, 20 s x 2 takes); loading is most of a short
# job, and the audio codes and the decode grow with the duration
ACE_WEIGHTS = {
    "load": 0.62,
    "plan": 0.085,
    "codes": 0.09,
    "diffusion": 0.15,
    "decode": 0.055,
}
ACE_MARKS = (
    ("[initialize_service]", "load"),
    ("Phase 1:", "plan"),
    ("Phase 2:", "codes"),
    ("Starting generation", "diffusion"),
    ("Decoding latents with VAE", "decode"),
)
ACE_LABELS = {
    "load": "Loading",
    "plan": "Planning",
    "codes": "Audio codes",
    "diffusion": "Diffusion",
    "decode": "Decoding",
}
ACE_BARS = {"codes": "CFG", "diffusion": "DiT diffusion"}  # the bar each stage shows
# Drama 3 prints a line only when a take is done; the load before the first take is about a third of a 10-20 s job
# (the job's time less the audio generated at 2x real time, 2 jobs)
DRAMA_LOAD = 0.33
# the same load in words: a take takes about 0.2 s per word (2.5 words a second, generated at 2x real time), so the
# 6 s load counts as 30 words of work in the history
DRAMA_LOAD_WORDS = 30
# below this the fixed load time and the first step's warm-up make the live rate misleading
MIN_LIVE = 0.1

finished: dict[str, tuple[tuple[Any, ...], float] | None] = {}  # cost() of done jobs


def read_log(path: Path) -> str:
    return path.read_bytes().decode(errors="replace")


def segments(text: str) -> list[tuple[str, bool]]:
    """The log's pieces in order, split at line ends and carriage returns. The flag marks a piece that ended its
    line, as a tqdm bar does once it is closed."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        parts = line.split("\r")
        for j, part in enumerate(parts):
            if part.strip():
                out.append((part.strip(), i < len(lines) - 1 and j == len(parts) - 1))
    return out


def overall(weights: dict[str, float], stage: str, within: float) -> float:
    """The whole job's fraction at `within` (0..1) of `stage`."""
    names = list(weights)
    return (
        sum(weights[s] for s in names[: names.index(stage)]) + weights[stage] * within
    )


def mflux(text: str, load: float) -> tuple[str | None, float | None]:
    last = None
    for seg, _ in segments(text):
        last = BAR.match(seg) or last
    if last is None:
        return None, None
    n, total = int(last["n"]), int(last["total"])
    return f"Step {n}/{total}", load + (1 - load) * n / total


def ltx(text: str, weights: dict[str, float]) -> tuple[str | None, float | None]:
    stage, headers, n, total = None, 0, 0, 0
    for seg, ended in segments(text):
        if seg.startswith("[Loading text encoder"):
            stage = "load"
        elif m := LTX_HEADER.match(seg):
            headers += 1
            stage, n, total = ("stage1" if headers == 1 else "stage2"), 0, int(m[1])
        elif stage in ("stage1", "stage2") and (m := BAR.match(seg)):
            n = int(m["n"])
            if ended:  # tqdm closed the bar: the stage is done
                stage = "upscale" if stage == "stage1" else "decode"
        elif seg.startswith("Saved to:"):
            return "Finishing", 1.0
    if stage is None:
        return None, None
    if stage in ("stage1", "stage2"):
        return f"{LTX_LABELS[stage]}: {n}/{total}", overall(weights, stage, n / total)
    return LTX_LABELS[stage], overall(weights, stage, 0.0)


def ace(text: str) -> tuple[str | None, float | None]:
    order = list(ACE_WEIGHTS)
    stage, within = None, 0.0
    for seg, _ in segments(text):
        if "takes written" in seg:
            return "Finishing", 1.0
        mark = next((s for key, s in ACE_MARKS if key in seg), None)
        # the LM phases repeat when the takes are split into chunks: never step back
        if mark and (stage is None or order.index(mark) > order.index(stage)):
            stage, within = mark, 0.0
        elif (
            stage in ACE_BARS and (m := BAR.match(seg)) and ACE_BARS[stage] in m["desc"]
        ):
            within = int(m["n"]) / int(m["total"])
    if stage is None:
        return None, None
    return ACE_LABELS[stage], overall(ACE_WEIGHTS, stage, within)


def drama(text: str, takes: int) -> tuple[str | None, float | None]:
    tries: dict[int, int] = {}
    final = set()
    for seg, _ in segments(text):
        if m := TAKE.match(seg):
            k = int(m[1])
            tries[k] = tries.get(k, 0) + 1
            if m[2] == "ok" or tries[k] == 2:  # a take is redrawn once at most
                final.add(k)
    if not tries:
        return None, None
    done = len(final)
    if done == takes:
        return "Finishing", 1.0
    retry = ", retry" if done + 1 in tries else ""
    return (
        f"Take {done + 1} of {takes}{retry}",
        DRAMA_LOAD + (1 - DRAMA_LOAD) * done / takes,
    )


def parse(
    tool: str, params: dict[str, Any], text: str
) -> tuple[str | None, float | None]:
    """The stage and the whole job's fraction (0..1) the log shows so far; None for what it does not show."""
    if tool in MFLUX_LOAD:
        return mflux(text, MFLUX_LOAD[tool])
    if tool == "video.extend":
        return ltx(text, LTX_WEIGHTS["extend"])
    if tool.startswith("video."):
        hq = str(params.get("hq_decoder", "false")).lower() == "true"
        return ltx(text, LTX_WEIGHTS["hq" if hq else "standard"])
    if tool == "music.ace":
        return ace(text)
    if tool == "voice.drama3":
        return drama(text, int(num(params, "takes", 2)))
    if tool == "voice.transcribe":
        parts = PART.findall(text)
        if not parts:
            return None, None
        done, total = parts[-1]
        return "Transcribing", int(done) / int(total)
    if tool == "doc.ocr":
        steps = OCR_PAGE.findall(text)
        if not steps:
            return None, None
        step, done, total = steps[-1]
        if step == "render":  # seconds a page; the reading is most of the job
            return "Preparing the pages", None
        return "Reading", int(done) / int(total)
    return None, None


def cost(job: dict[str, Any], text: str) -> tuple[tuple[Any, ...], float] | None:
    """What drives a job's time: the settings a finished job must share with it, and the work its time grows in
    proportion to. None for an LTX job until its log shows the size of stage 1."""
    tool, p = job["tool"], job["params"]
    if tool in ("image.moss", "image.klein"):
        return (p.get("size"), num(p, "steps")), 1.0
    if tool == "image.klein_edit":
        return (p.get("size"), len(job.get("files", {}).get("references", []))), 1.0
    if tool == "voice.drama3":
        words = len(SPOKEN.sub(" ", str(p.get("text", ""))).split())
        return (), float(DRAMA_LOAD_WORDS + words * num(p, "takes", 2))
    if tool == "voice.transcribe":  # Nemotron takes several times as long as Qwen3-ASR
        return (p.get("model") or "qwen3-asr",), 1.0
    if tool == "music.ace":
        return (num(p, "duration"), num(p, "takes")), 1.0
    if tool.startswith("video."):
        m = LTX_HEADER.search(text)
        if m is None:
            return None
        return (str(p.get("hq_decoder", "false")).lower() == "true",), float(m[2])
    if tool == "doc.ocr":  # the time grows with the pages, which the log names first
        steps = OCR_PAGE.findall(text)
        if not steps:
            return None
        return (p.get("output"),), float(steps[-1][2])
    return (), 1.0


def expected(
    job: dict[str, Any], text: str, jobs: Iterable[dict[str, Any]], out: Path
) -> float | None:
    """The time history predicts: the median seconds per unit of work of finished jobs like this one, times its work."""
    mine = cost(job, text)
    if mine is None:
        return None
    rates = []
    for j in jobs:
        if j["tool"] != job["tool"] or j["status"] != "done" or not j.get("elapsed"):
            continue
        if j["id"] not in finished:
            log = out / j["id"] / "job.log"
            finished[j["id"]] = cost(j, read_log(log) if log.exists() else "")
        theirs = finished[j["id"]]
        if theirs is not None and theirs[0] == mine[0]:
            rates.append(j["elapsed"] / theirs[1])
    return statistics.median(rates) * mine[1] if rates else None


def eta(
    fraction: float | None,
    elapsed: float,
    updated: float | None,
    total: float | None,
) -> int | None:
    """Seconds left. `updated` is when the log last changed (seconds after the start): the fraction was true then.
    `total` is the history's prediction for the whole job. None when neither gives an answer, or the job has run
    past it."""
    if not fraction:  # nothing done yet that the log shows: only the history can tell
        left = total - elapsed if total is not None else None
    else:
        live = updated / fraction if fraction >= MIN_LIVE else None
        if live is not None and total is not None:
            total = fraction * live + (1 - fraction) * total
        elif live is not None:
            total = live
        left = (
            total * (1 - fraction) - (elapsed - updated) if total is not None else None
        )
    return round(left) if left is not None and left > 0 else None


def estimate(
    job: dict[str, Any], jobs: Iterable[dict[str, Any]], out: Path, now: float
) -> dict[str, Any]:
    """The job view's `progress` for a running job."""
    started = datetime.fromisoformat(job["started"]).timestamp()
    log = out / job["id"] / "job.log"
    text, updated = "", None
    if log.exists():  # not yet while the job's inputs are prepared
        text, updated = read_log(log), log.stat().st_mtime - started
    stage, fraction = parse(job["tool"], job["params"], text)
    return {
        "stage": stage,
        "fraction": None if fraction is None else round(fraction, 3),
        "eta_s": eta(fraction, now - started, updated, expected(job, text, jobs, out)),
    }
