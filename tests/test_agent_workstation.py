"""The Workstation agent tools through a fake port: list tools, job status, Library search and create_job."""

import threading
from pathlib import Path

import pytest

from app.agent import Context, Registry, Settings, build_registry
from tests.fake_workstation import FakeWorkstation


@pytest.fixture
def port() -> FakeWorkstation:
    return FakeWorkstation()


@pytest.fixture
def reg(port: FakeWorkstation) -> Registry:
    return build_registry(port, Settings())


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    return Context("c1", None, threading.Event(), tmp_path)


def test_list_tools_shows_ids_fields_and_install_state(
    reg: Registry, ctx: Context
) -> None:
    text = reg.call("list_workstation_tools", {}, ctx).content
    assert "image.klein: FLUX.3 Klein (Image), installed." in text
    assert "prompt (textarea, required)" in text
    assert "size (one of 768x1344 | 1024x1024, default 768x1344)" in text
    assert "steps (number, 1..50, default 4)" in text
    assert "music.ace: ACE-Step (Music), NOT installed." in text
    assert "needs an uploaded file" in text
    assert "image (file" not in text  # upload fields are not offered


def test_job_status(reg: Registry, ctx: Context) -> None:
    text = reg.call("job_status", {"job_id": "20261103-120000-abcd"}, ctx).content
    assert "status: running" in text
    assert "stage: Denoising" in text and "progress: 42%" in text
    assert "about 31 s left" in text
    done = reg.call("job_status", {"job_id": "20261103-110000-beef"}, ctx).content
    assert "output: image.png (image)" in done
    r = reg.call("job_status", {"job_id": "nope"}, ctx)
    assert r.status == "error" and "No job" in r.content


def test_search_library(reg: Registry, ctx: Context) -> None:
    text = reg.call("search_library", {"query": "fox"}, ctx).content
    assert "image.png (image), job 20261103-110000-beef" in text
    assert "text: A red fox in the snow at dusk" in text
    assert (
        "No outputs match" in reg.call("search_library", {"query": "zz"}, ctx).content
    )


def test_create_job_queues_and_returns_at_once(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    r = reg.call(
        "create_job",
        {
            "tool": "image.klein",
            "fields": {"prompt": "a fox", "steps": 8.0, "size": "1024x1024"},
        },
        ctx,
    )
    assert r.status == "ok"
    assert "Queued job 20261103-130000-0001 (FLUX.3 Klein)" in r.content
    assert "Do not wait" in r.content
    # values go to the port as form strings
    assert port.created == [
        ("image.klein", {"prompt": "a fox", "steps": "8", "size": "1024x1024"})
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"tool": "image.nope"}, "Unknown tool 'image.nope'"),
        ({"tool": "music.ace"}, "not installed"),
        ({"tool": "video.i2v", "fields": {"prompt": "x"}}, "needs an uploaded file"),
        ({"tool": "image.klein", "fields": {"colour": "red"}}, "no field 'colour'"),
        ({"tool": "image.klein", "fields": {"steps": 999}}, "The job was refused"),
    ],
)
def test_create_job_refusals(
    reg: Registry, ctx: Context, port: FakeWorkstation, args: dict, message: str
) -> None:
    r = reg.call("create_job", args, ctx)
    assert r.status == "error" and message in r.content
    assert port.created == []


def test_create_job_explains_itself(reg: Registry) -> None:
    line = reg.get("create_job").describe(
        {"tool": "image.klein", "fields": {"prompt": "a fox"}}
    )
    assert "image.klein" in line and "'a fox'" in line
