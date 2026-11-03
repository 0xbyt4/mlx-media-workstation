"""Progress parsing, the time-left math and the job view's `progress` field.

The fixtures are trimmed logs of real jobs (prompts, heard text and paths replaced). A running job's log is the
same file cut short, so most tests cut a fixture at a marker.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app import progress, server
from tests.conftest import app_client

FIXTURES = Path(__file__).parent / "fixtures"


def log(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode()


def upto(text: str, marker: str, nth: int = 1) -> str:
    """The text up to the end of the nth `marker`."""
    i = -1
    for _ in range(nth):
        i = text.index(marker, i + 1)
    return text[: i + len(marker)]


def before(text: str, marker: str) -> str:
    return text[: text.index(marker)]


@pytest.fixture(autouse=True)
def no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "finished", {})


def test_segments_split_carriage_returns_and_mark_line_ends() -> None:
    text = "a\n\r 1/8 \r   \r 2/8\nb\r c"
    assert progress.segments(text) == [
        ("a", True),
        ("1/8", False),
        ("2/8", True),
        ("b", False),
        ("c", False),
    ]


def test_log_is_read_as_bytes(tmp_path: Path) -> None:
    path = tmp_path / "job.log"
    path.write_bytes(b"\r 1/8 [\r 2/8 [")
    assert progress.read_log(path) == "\r 1/8 [\r 2/8 ["


# ---------- mflux ----------


def test_mflux_before_the_bar_shows_nothing() -> None:
    text = log("mflux_klein.log").split("\n")[0] + "\n"
    assert progress.parse("image.klein", {}, text) == (None, None)


def test_mflux_mid_bar() -> None:
    text = upto(log("mflux_klein.log"), "5/16 [")
    stage, fraction = progress.parse("image.klein", {}, text)
    assert stage == "Step 5/16"
    assert fraction == pytest.approx(0.16 + 0.84 * 5 / 16)


def test_mflux_half_written_segment_keeps_the_last_whole_one() -> None:
    text = before(log("mflux_klein.log"), "5/16 [") + "5/1"
    assert progress.parse("image.klein", {}, text)[0] == "Step 4/16"


def test_mflux_load_share_per_tool() -> None:
    text = upto(log("mflux_klein.log"), "8/16 [")
    assert progress.parse("image.moss", {}, text)[1] == pytest.approx(0.03 + 0.97 / 2)
    assert progress.parse("image.klein_edit", {}, text)[1] == pytest.approx(
        0.065 + 0.935 / 2
    )


def test_mflux_done() -> None:
    assert progress.parse("image.klein", {}, log("mflux_klein.log")) == (
        "Step 16/16",
        1.0,
    )


# ---------- LTX ----------

T2V_STAGE2 = "[estimate] denoising (ancestral): 3 steps x 1 passes over 9216 video"


@pytest.mark.parametrize(
    ("cut", "stage", "fraction"),
    [
        (lambda t: t.split("\n")[0] + "\n", None, None),
        (lambda t: upto(t, "[Loading text encoder (Gemma)] ..."), "Loading", 0.0),
        (lambda t: upto(t, "3/8 ["), "Stage 1: 3/8", 0.06 + 0.27 * 3 / 8),
        # the bar's last redraw, before tqdm closes it with a line end
        (
            lambda t: before(t, T2V_STAGE2).rstrip("\n"),
            "Stage 1: 7/8",
            0.06 + 0.27 * 7 / 8,
        ),
        (lambda t: before(t, T2V_STAGE2), "Upscaling", 0.33),
        (lambda t: upto(t, T2V_STAGE2), "Stage 2: 0/3", 0.36),
        (lambda t: upto(t, "1/3 ["), "Stage 2: 1/3", 0.36 + 0.48 / 3),
        (lambda t: upto(t, "[Loading decoders"), "Decoding", 0.84),
        (lambda t: t, "Finishing", 1.0),
    ],
)
def test_ltx_t2v_stages(cut: Any, stage: str | None, fraction: float | None) -> None:
    got = progress.parse("video.t2v", {"hq_decoder": "false"}, cut(log("ltx_t2v.log")))
    assert got[0] == stage
    assert got[1] == (None if fraction is None else pytest.approx(fraction))


def test_ltx_diffusion_decoder_weights() -> None:
    text = upto(log("ltx_i2v_hq.log"), "[Decoding video + audio + muxing] ...")
    stage, fraction = progress.parse("video.i2v", {"hq_decoder": "true"}, text)
    assert stage == "Decoding"
    assert fraction == pytest.approx(0.325)  # the diffusion decoder is 2/3 of the job


def test_ltx_a2v_without_a_decode_header() -> None:
    text = upto(log("ltx_a2v.log"), "[vae-decode tiling] frames")
    assert progress.parse("video.a2v", {}, text) == ("Decoding", pytest.approx(0.84))


def test_ltx_extend_weights_and_join() -> None:
    text = log("ltx_extend.log")
    stage, fraction = progress.parse("video.extend", {}, upto(text, "2/3 ["))
    assert stage == "Stage 2: 2/3"
    assert fraction == pytest.approx(0.09 + 0.26 + 0.02 + 0.46 * 2 / 3)
    assert progress.parse("video.extend", {}, upto(text, "$ ffmpeg")) == (
        "Finishing",
        1.0,
    )


def test_ltx_fraction_never_goes_back() -> None:
    text = log("ltx_extend.log")
    seen = [progress.parse("video.extend", {}, text[:k])[1] for k in range(len(text))]
    values = [f for f in seen if f is not None]
    assert values == sorted(values)


def test_unknown_output_gives_nulls() -> None:
    assert progress.parse("video.t2v", {}, "$ ltx-2-mlx\nsome other output\n") == (
        None,
        None,
    )
    assert progress.parse("edit.mix", {}, "$ ffmpeg -v error\n") == (None, None)


# ---------- ACE-Step ----------


@pytest.mark.parametrize(
    ("cut", "stage", "fraction"),
    [
        (lambda t: t.split("\n")[0] + "\n", None, None),
        (lambda t: upto(t, "[initialize_service]"), "Loading", 0.0),
        (lambda t: upto(t, "Phase 1:"), "Planning", 0.62),
        (lambda t: upto(t, "48/60 ["), "Audio codes", 0.705 + 0.09 * 48 / 60),
        (lambda t: upto(t, "Starting generation"), "Diffusion", 0.795),
        (lambda t: upto(t, "5/8 ["), "Diffusion", 0.795 + 0.15 * 5 / 8),
        (lambda t: upto(t, "Decoding latents with VAE"), "Decoding", 0.945),
        (lambda t: t, "Finishing", 1.0),
    ],
)
def test_ace_stages(cut: Any, stage: str | None, fraction: float | None) -> None:
    got = progress.parse("music.ace", {}, cut(log("ace.log")))
    assert got[0] == stage
    assert got[1] == (None if fraction is None else pytest.approx(fraction))


def test_ace_lm_bar_of_phase_1_is_ignored() -> None:
    # its total is the token limit, not the length of the answer
    text = upto(log("ace.log"), "162/3500 [")
    assert progress.parse("music.ace", {}, text) == ("Planning", 0.62)


def test_ace_repeated_phase_does_not_step_back() -> None:
    text = upto(log("ace.log"), "48/60 [") + "\n2026-11-01 | INFO | - Phase 1: again\n"
    assert progress.parse("music.ace", {}, text)[0] == "Audio codes"


# ---------- Fish Drama 3 ----------


def test_drama_before_the_first_take_shows_nothing() -> None:
    text = log("drama3_two_takes.log").split("\n")[0] + "\n"
    assert progress.parse("voice.drama3", {"takes": "2"}, text) == (None, None)


def test_drama_take_redrawn() -> None:
    text = log("drama3_retry.log")
    first = "\n".join(text.split("\n")[:2]) + "\n"
    assert progress.parse("voice.drama3", {"takes": "1"}, first) == (
        "Take 1 of 1, retry",
        0.33,
    )
    assert progress.parse("voice.drama3", {"takes": "1"}, text) == ("Finishing", 1.0)


def test_drama_takes() -> None:
    lines = log("drama3_two_takes.log").split("\n")
    stage, fraction = progress.parse(
        "voice.drama3", {"takes": "2"}, "\n".join(lines[:2]) + "\n"
    )
    assert stage == "Take 2 of 2"
    assert fraction == pytest.approx(0.33 + 0.67 / 2)


# ---------- history ----------


def job(
    id_: str, tool: str, status: str = "done", elapsed: float | None = None, **params
) -> dict[str, Any]:
    return {
        "id": id_,
        "tool": tool,
        "status": status,
        "elapsed": elapsed,
        "params": params,
        "files": {},
    }


def test_cost_normalises_stored_values() -> None:
    a = progress.cost(job("a", "image.klein", size="768x1344", steps=16), "")
    b = progress.cost(job("b", "image.klein", size="768x1344", steps="16"), "")
    assert a == b == (("768x1344", 16.0), 1.0)


def test_cost_of_ltx_waits_for_the_stage_size() -> None:
    j = job("a", "video.extend", seconds="8")
    assert progress.cost(j, "$ python ltx_extend.py\n") is None
    assert progress.cost(j, log("ltx_extend.log")) == ((False,), 6160.0)


def test_cost_of_drama_counts_spoken_words_per_take() -> None:
    text = "<|speaker:0|> One two [laughs] three. <|speaker:1|> Four."
    assert progress.cost(job("a", "voice.drama3", takes="2", text=text), "") == (
        (),
        30 + 4 * 2,
    )


def test_expected_is_the_median_of_matching_jobs(tmp_path: Path) -> None:
    jobs = [
        job("a", "image.klein", elapsed=40, size="768x1344", steps="16"),
        job("b", "image.klein", elapsed=44, size="768x1344", steps="16"),
        job("c", "image.klein", elapsed=50, size="768x1344", steps="16"),
        job("d", "image.klein", elapsed=25, size="768x1344", steps="8"),
        job(
            "e", "image.klein", status="failed", elapsed=3, size="768x1344", steps="16"
        ),
        job("f", "image.moss", elapsed=240, size="768x1344", steps="16"),
    ]
    mine = job("g", "image.klein", status="running", size="768x1344", steps="16")
    assert progress.expected(mine, "", jobs, tmp_path) == 44


def test_expected_scales_ltx_by_tokens(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "job.log").write_bytes(log("ltx_t2v.log").encode())  # 2304
    done = job("a", "video.t2v", elapsed=104.5, hq_decoder="false")
    mine = job("b", "video.t2v", status="running", hq_decoder="false")
    text = "[estimate] denoising (ancestral): 8 steps x 1 passes over 4608 video\n"
    assert progress.expected(mine, text, [done], tmp_path) == pytest.approx(209)
    hq = job("c", "video.t2v", status="running", hq_decoder="true")
    assert progress.expected(hq, text, [done], tmp_path) is None


def test_no_history() -> None:
    mine = job("a", "music.ace", status="running", duration="30", takes="2")
    assert progress.expected(mine, "", [], Path("/nonexistent")) is None


# ---------- time left ----------


@pytest.mark.parametrize(
    ("fraction", "elapsed", "updated", "total", "left"),
    [
        (None, 30, None, None, None),  # nothing to go on
        (None, 30, None, 100, 70),  # history only
        (0.0, 30, 29, 100, 70),  # still loading: history only
        (None, 150, None, 100, None),  # ran past the history: no number
        (0.05, 10, 10, None, None),  # too early for the live rate
        (0.05, 10, 10, 200, 190),
        (0.5, 50, 50, None, 50),  # live rate only
        (0.5, 60, 50, None, 40),  # no new output for 10 s
        (0.5, 100, 100, 100, 75),  # half done in the history's whole time
        (0.9, 90, 90, 100, 10),
        (1.0, 80, 79, 100, None),  # done, finishing up
    ],
)
def test_eta(
    fraction: float | None,
    elapsed: float,
    updated: float | None,
    total: float | None,
    left: int | None,
) -> None:
    assert progress.eta(fraction, elapsed, updated, total) == left


# ---------- the job view ----------


def running_job(tmp_path: Path, text: str, age: float) -> dict[str, Any]:
    """A running t2v job started `age` seconds ago whose log is `text`, written just now."""
    started = datetime.fromtimestamp(datetime.now().timestamp() - age)
    j = job("run", "video.t2v", status="running", hq_decoder="false", prompt="x")
    j |= {
        "tool_label": "Text to Video",
        "created": started.isoformat(timespec="seconds"),
        "started": started.isoformat(timespec="seconds"),
        "finished": None,
        "outputs": [],
        "peak_gb": None,
        "error": None,
        "log_tail": "",
    }
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "job.log").write_bytes(text.encode())
    return j


def test_estimate(tmp_path: Path) -> None:
    j = running_job(tmp_path, upto(log("ltx_t2v.log"), "1/3 ["), age=60)
    now = datetime.now().timestamp()
    os.utime(tmp_path / "run" / "job.log", (now, now))
    got = progress.estimate(j, [j], tmp_path, now)
    assert got["stage"] == "Stage 2: 1/3"
    assert got["fraction"] == 0.52
    assert 50 <= got["eta_s"] <= 57  # 60 s for 52 %: about 55 s left


def test_view_and_api_expose_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = running_job(tmp_path, upto(log("ltx_t2v.log"), "3/8 ["), age=20)
    done = job("old", "video.t2v", elapsed=104.5, hq_decoder="false")
    done |= {k: running[k] for k in ("tool_label", "created", "started", "finished")}
    done |= {"status": "done", "outputs": [], "peak_gb": 21.2, "error": None}
    done["log_tail"] = ""
    monkeypatch.setattr(server, "OUT", tmp_path)
    monkeypatch.setattr(server, "jobs", {"run": running, "old": done})

    v = server.view(running)
    assert set(v["progress"]) == {"stage", "fraction", "eta_s"}
    assert v["progress"]["stage"] == "Stage 1: 3/8"
    assert server.view(done)["progress"] is None

    client = app_client()
    body = client.get("/api/jobs").json()
    by_id = {j["id"]: j for j in body}
    assert by_id["run"]["progress"]["fraction"] == pytest.approx(0.161, abs=0.001)
    assert by_id["old"]["progress"] is None
    for key in ("id", "status", "elapsed", "position", "params", "outputs", "log_tail"):
        assert key in by_id["run"]
