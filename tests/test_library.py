"""The Library endpoints on a temp outputs folder: listing, Show in Finder and deleting one output."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import library, server

Job = Callable[..., dict[str, Any]]
REAL_PROBE = library.probe  # the autouse fixture replaces library.probe


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Canned media facts instead of ffprobe; returns the files that were probed."""
    seen: list[Path] = []

    def fake(path: Path) -> dict[str, Any]:
        seen.append(path)
        return {"width": 768, "height": 1344, "duration": 5.04}

    monkeypatch.setattr(library, "probe", fake)
    monkeypatch.setattr(library, "probed", {})
    return seen


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Records `open -R` calls instead of running them."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(library.subprocess, "run", fake_run)
    return calls


def done_job(make_job: Job, state: Path, job_id: str, created: str, **files: str):
    """A finished job whose outputs are `files` (name -> kind), written as small files."""
    outputs = [
        {"url": f"/files/{job_id}/{n}", "kind": k, "name": n, "note": ""}
        for n, k in files.items()
    ]
    job = make_job(job_id, "done", created=created, outputs=outputs)
    for n in files:
        (state / job_id / n).write_bytes(b"x" * 10)
    (state / job_id / "in_image_0.png").write_bytes(b"input")
    return job


# Listing


def test_library_lists_outputs_of_done_jobs_newest_first(
    client: TestClient, make_job: Job, state: Path
) -> None:
    done_job(
        make_job,
        state,
        "20261101-090000-aaaa",
        "2026-11-01T09:00:00",
        **{"image.png": "image"},
    )
    done_job(
        make_job,
        state,
        "20261102-090000-bbbb",
        "2026-11-02T09:00:00",
        **{
            "video.mp4": "video",
            "music.wav": "audio",
        },
    )
    make_job("20261102-100000-cccc", "running")
    make_job("20261102-110000-dddd", "failed")
    items = client.get("/api/library").json()
    assert [(i["job"], i["name"]) for i in items] == [
        ("20261102-090000-bbbb", "video.mp4"),
        ("20261102-090000-bbbb", "music.wav"),
        ("20261101-090000-aaaa", "image.png"),
    ]
    video = items[0]
    assert video == {
        "job": "20261102-090000-bbbb",
        "tool": "image.klein",
        "tool_label": "FLUX.3 Klein",
        "kind": "video",
        "name": "video.mp4",
        "url": "/files/20261102-090000-bbbb/video.mp4",
        "note": "",
        "size": 10,
        "created": "2026-11-02T09:00:00",
        "finished": None,
        "elapsed": None,
        "peak_gb": None,
        "text": "a lighthouse",
        "params": {"prompt": "a lighthouse"},
        "path": str(state / "20261102-090000-bbbb" / "video.mp4"),
        "width": 768,
        "height": 1344,
        "duration": 5.04,
    }
    assert (items[1]["width"], items[1]["duration"]) == (None, 5.04)  # audio
    assert (items[2]["width"], items[2]["duration"]) == (768, None)  # image


def test_library_skips_missing_files_and_symlinks(
    client: TestClient, make_job: Job, state: Path, tmp_path: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-02T09:00:00",
        **{
            "a.png": "image",
            "b.png": "image",
            "c.png": "image",
        },
    )
    (state / job_id / "b.png").unlink()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    (state / job_id / "c.png").unlink()
    (state / job_id / "c.png").symlink_to(outside)
    assert [i["name"] for i in client.get("/api/library").json()] == ["a.png"]


def test_library_main_text_and_cache(
    client: TestClient, make_job: Job, state: Path, no_ffprobe: list[Path]
) -> None:
    job = done_job(
        make_job,
        state,
        "20261102-090000-aaaa",
        "2026-11-02T09:00:00",
        **{"take_1.wav": "audio"},
    )
    job["params"] = {"text": "  ", "caption": "calm piano"}
    assert client.get("/api/library").json()[0]["text"] == "calm piano"
    client.get("/api/library")
    assert len(no_ffprobe) == 1  # the second list uses the cached facts
    (state / job["id"] / "take_1.wav").write_bytes(b"longer file")
    client.get("/api/library")
    assert len(no_ffprobe) == 2  # a changed file is probed again


def test_probe_parses_and_survives_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [
        json.dumps(
            {
                "streams": [
                    {"codec_type": "audio"},
                    {"codec_type": "video", "width": 704, "height": 1280},
                ],
                "format": {"duration": "5.041667"},
            }
        ),
        "not json",
        subprocess.TimeoutExpired("ffprobe", 10),
    ]

    def fake_run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        assert cmd[0] == "ffprobe" and cmd[-1] == "/x/clip.mp4"
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(stdout=a)

    monkeypatch.setattr(library.subprocess, "run", fake_run)
    empty = {"width": None, "height": None, "duration": None}
    assert REAL_PROBE(Path("/x/clip.mp4")) == {
        "width": 704,
        "height": 1280,
        "duration": 5.04,
    }
    assert REAL_PROBE(Path("/x/clip.mp4")) == empty
    assert REAL_PROBE(Path("/x/clip.mp4")) == empty


# Show in Finder


def test_reveal_opens_finder_at_the_output(
    client: TestClient, make_job: Job, state: Path, opened: list[list[str]]
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.post("/api/library/reveal", json={"job": job_id, "name": "image.png"})
    assert r.status_code == 200
    assert opened == [["open", "-R", str(state / job_id / "image.png")]]


@pytest.mark.parametrize(
    ("job", "name", "status"),
    [
        ("20261102-090000-aaaa", "in_image_0.png", 400),  # an input
        ("20261102-090000-aaaa", "../other/image.png", 404),
        ("20261102-090000-aaaa", "missing.png", 404),  # not a listed output
        ("20261102-090000-aaaa", ".hidden.png", 404),
        ("../20261102-090000-aaaa", "image.png", 404),
        ("20990101-000000-ffff", "image.png", 404),  # no such job
    ],
)
def test_reveal_refuses_anything_but_a_listed_output(
    client: TestClient,
    make_job: Job,
    state: Path,
    opened: list[list[str]],
    job: str,
    name: str,
    status: int,
) -> None:
    done_job(
        make_job,
        state,
        "20261102-090000-aaaa",
        "2026-11-02T09:00:00",
        **{"image.png": "image"},
    )
    r = client.post("/api/library/reveal", json={"job": job, "name": name})
    assert r.status_code == status
    assert opened == []


def test_reveal_refuses_a_symlinked_output(
    client: TestClient,
    make_job: Job,
    state: Path,
    tmp_path: Path,
    opened: list[list[str]],
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    (state / job_id / "image.png").unlink()
    (state / job_id / "image.png").symlink_to(outside)
    r = client.post("/api/library/reveal", json={"job": job_id, "name": "image.png"})
    assert r.status_code == 404
    assert opened == []


def test_reveal_from_another_site_is_refused(
    client: TestClient, make_job: Job, state: Path, opened: list[list[str]]
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.post(
        "/api/library/reveal",
        json={"job": job_id, "name": "image.png"},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert opened == []


# Deleting one output


def test_delete_output_removes_the_file_and_the_listing(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-02T09:00:00",
        **{
            "take_1.wav": "audio",
            "take_2.wav": "audio",
        },
    )
    r = client.delete(f"/api/library/{job_id}/take_1.wav")
    assert r.status_code == 200
    assert not (state / job_id / "take_1.wav").exists()
    assert (state / job_id / "take_2.wav").exists()
    assert (state / job_id / "in_image_0.png").exists()
    saved = json.loads((state / job_id / "job.json").read_text())
    assert [o["name"] for o in saved["outputs"]] == ["take_2.wav"]
    assert [o["name"] for o in server.jobs[job_id]["outputs"]] == ["take_2.wav"]
    assert [i["name"] for i in client.get("/api/library").json()] == ["take_2.wav"]
    assert client.delete(f"/api/library/{job_id}/take_1.wav").status_code == 404


def test_delete_output_refuses_inputs(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.delete(f"/api/library/{job_id}/in_image_0.png")
    assert r.status_code == 400
    assert (state / job_id / "in_image_0.png").exists()


def test_delete_output_refuses_a_running_job(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    outputs = [{"url": f"/files/{job_id}/a.png", "kind": "image", "name": "a.png"}]
    make_job(job_id, "running", outputs=outputs)
    (state / job_id / "a.png").write_bytes(b"x")
    r = client.delete(f"/api/library/{job_id}/a.png")
    assert r.status_code == 409
    assert (state / job_id / "a.png").exists()


def test_delete_output_from_another_site_is_refused(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.delete(
        f"/api/library/{job_id}/image.png", headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert (state / job_id / "image.png").exists()
