"""The Models screen: catalog, install status, the fits rule, downloads and their endpoints.

Every Hub call and the download child process are replaced; nothing here touches the network or real weights.
"""

import errno
import re
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError
from huggingface_hub.hf_api import RepoFile

from app import config, models, server, tools

ROOT = Path(__file__).resolve().parents[1]
QWEN = models.BY_ID["qwen3.8-27b-4bit"]
LTX = models.BY_ID["ltx-2.5-mlx-q8"]
ASR = models.BY_ID["qwen3-asr-0.6b-8bit"]
ACE = models.BY_ID["ace-step-1.5"]


class NoNetwork:
    def __init__(self, *args, **kwargs):
        raise AssertionError("a test reached for the network")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    models.state.clear()
    models.sizes_cache.clear()
    monkeypatch.setattr(models, "HfApi", NoNetwork)
    monkeypatch.setattr(models, "snapshot_download", NoNetwork)
    yield
    models.stop()
    wait_for(lambda: models.state.get("proc") is None)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Every configured path and the Hugging Face cache under a temp folder."""
    monkeypatch.setattr(
        models,
        "PATHS",
        replace(models.PATHS, **{k: tmp_path / k for k in config.DEFAULTS}),
    )
    monkeypatch.setattr(models, "HF_HUB", tmp_path / "hub")
    return tmp_path


class FakeProc:
    """Stands in for the download child: it runs until the test ends it or it is killed."""

    def __init__(self, on_exit):
        self.pid = 4242
        self.returncode = None
        self.on_exit = on_exit
        self.ended = threading.Event()
        self.out = ""

    def end(self, code: int, out: str = "") -> None:
        self.returncode, self.out = code, out
        self.ended.set()

    def communicate(self):
        self.ended.wait(5)
        self.on_exit(self)
        return self.out, None


@pytest.fixture
def child(monkeypatch):
    """Replaces Popen and killpg; returns the started fake processes and the signals sent."""
    started: list[FakeProc] = []
    killed: list[tuple[int, int]] = []
    on_exit = {"call": lambda proc: None}

    def popen(cmd, **kwargs):
        assert cmd[1:3] == ["-m", "app.models"]
        assert kwargs["start_new_session"]
        proc = FakeProc(lambda p: on_exit["call"](p))
        started.append(proc)
        return proc

    def killpg(pgid, sig):
        killed.append((pgid, sig))
        started[-1].end(-sig)

    monkeypatch.setattr(models.subprocess, "Popen", popen)
    monkeypatch.setattr(models.os, "killpg", killpg)
    return started, killed, on_exit


def wait_for(cond, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.01)


def write(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def install(m: models.Model) -> None:
    root = models.target(m)
    root.mkdir(parents=True, exist_ok=True)
    for f in m.files:
        write(root / f)


def http_error(cls, status: int):
    response = httpx2.Response(
        status, request=httpx2.Request("GET", "https://huggingface.co/x")
    )
    return cls(f"{status} Client Error", response=response)


def flat(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines())


# catalog


def test_ids_are_unique():
    assert len(models.BY_ID) == len(models.MODELS)


def test_every_required_path_of_every_tool_is_in_the_catalog():
    for tool, required in tools.REQUIRES.items():
        entries = [m for m in models.MODELS if tool in m.tools]
        checked = [
            q
            for m in entries
            for q in [models.target(m), *(models.target(m) / f for f in m.files)]
        ]
        for p in required:
            assert any(q == p or p in q.parents for q in checked), (tool, p)
        assert any(not m.manual for m in entries), tool


def test_catalog_names_only_known_tools():
    for m in models.MODELS:
        assert set(m.tools) <= set(tools.BY_ID), m.id
        assert m.tools or m.features, m.id


def test_download_entries_are_complete():
    for m in (m for m in models.MODELS if not m.manual):
        assert re.fullmatch(r"[0-9a-f]{40}", m.revision), m.id
        assert m.size_gb and m.peak_gb and m.licence and m.files, m.id
        assert m.licence_url.startswith("https://"), m.id
        assert m.path_key is None or m.path_key in config.DEFAULTS, m.id
        assert m.needs is None or models.BY_ID[m.needs].manual, m.id


def test_manual_entries_carry_the_readme_commands():
    readme = flat((ROOT / "README.md").read_text())
    for m in (m for m in models.MODELS if m.manual):
        assert m.install and flat(m.install) in readme, m.id
        assert m.path_key in config.DEFAULTS, m.id


def test_qwen_entry_and_config_keys():
    assert QWEN.repo == "mlx-community/Qwen3.8-27B-4bit"
    assert QWEN.revision == "10c35caafbb80f7dc6a7a432cdd11af10a6d4818"
    assert (QWEN.size_gb, QWEN.licence, QWEN.path_key) == (
        16.1,
        "Apache-2.0",
        "qwen_model",
    )
    assert QWEN.features == ("Prompt help", "Chat")
    assert config.DEFAULTS["qwen_python"] == "~/mlx-media/venv-qwen/bin/python"
    assert config.DEFAULTS["qwen_model"] == "~/mlx-media/models/qwen3.8-27b-4bit"
    example = (ROOT / "config.example.toml").read_text()
    assert 'qwen_model = "~/mlx-media/models/qwen3.8-27b-4bit"' in example


# status


def test_a_folder_without_the_files_is_not_installed(paths):
    models.target(LTX).mkdir(parents=True)
    assert not models.installed(LTX)
    for f in LTX.files[:-1]:
        write(models.target(LTX) / f)
    assert not models.installed(LTX)
    write(models.target(LTX) / LTX.files[-1])
    assert models.installed(LTX)


def test_cache_entries_are_found_in_the_pinned_snapshot(paths):
    snapshot = (
        paths / "hub" / "models--mlx-community--Qwen3-ASR-0.6B-8bit" / "snapshots"
    )
    for f in ASR.files:
        write(snapshot / ("0" * 40) / f)  # another revision does not count
    assert not models.installed(ASR)
    install(ASR)
    assert models.target(ASR) == snapshot / ASR.revision
    assert models.installed(ASR)


def test_subfolder_and_file_targets(paths):
    assert models.target(ACE) == paths / "ace_dir" / "checkpoints"
    venv = models.BY_ID["drama-venv"]
    assert not models.installed(venv)
    write(paths / "drama_python")
    assert models.installed(venv)


def test_view_reports_status_and_machine(paths, monkeypatch):
    install(QWEN)
    monkeypatch.setattr(models, "mem_total_gb", lambda: 32.0)
    c = models.catalog()
    assert c["machine"] == {"mem_total_gb": 32.0, "mem_reserve_gb": 8.0}
    v = {m["id"]: m for m in c["models"]}
    assert v[QWEN.id]["installed"] and v[QWEN.id]["fits"]
    assert v[QWEN.id]["used_by"] == ["Prompt help", "Chat"]
    assert not v[LTX.id]["installed"] and v[LTX.id]["fits"] is False
    assert v[LTX.id]["disk_free_gb"] > 0
    assert v["mflux"]["kind"] == "manual" and v["mflux"]["fits"] is None
    assert v["mflux"]["install"].startswith("git clone")


# fits rule


def test_fits_keeps_a_reserve_for_macos():
    assert models.fits(39.0, 48.0)  # Fish Drama 3 on our 48 GB Mac
    assert models.fits(40.0, 48.0)
    assert not models.fits(40.5, 48.0)
    assert not models.fits(35.0, 36.0)
    assert models.fits(23.0, 32.0)


# progress


def test_bytes_on_disk_count_finished_and_partial_files(paths):
    sizes = {"config.json": 10, "model-00001-of-00003.safetensors": 1000}
    root = models.target(QWEN)
    write(root / "config.json", 10)
    write(root / "README.md", 500)  # not part of this download
    write(models.partial_dir(QWEN) / "ab.cd.incomplete", 300)
    assert models.on_disk(QWEN, sizes) == 310
    assert models.percent(310, 1010) == 30.7
    assert models.percent(2000, 1010) == 100.0


def test_cache_partials_are_in_the_blobs_folder(paths):
    sizes = {f: 100 for f in ASR.files}
    write(models.repo_cache(ASR) / "blobs" / "e1.f2.incomplete", 40)
    assert models.on_disk(ASR, sizes) == 40


def test_remote_sizes_follow_the_patterns_and_are_fetched_once(monkeypatch):
    calls = []

    class Api:
        def list_repo_tree(self, repo, revision, recursive):
            calls.append((repo, revision, recursive))
            return [
                RepoFile(path="transformer-distilled.safetensors", size=7, oid="a"),
                RepoFile(path="transformer-dev.safetensors", size=9, oid="b"),
            ]

    monkeypatch.setattr(models, "HfApi", Api)
    assert models.remote_sizes(LTX) == {"transformer-distilled.safetensors": 7}
    models.remote_sizes(LTX)
    assert calls == [(LTX.repo, LTX.revision, True)]


# downloads


def start_qwen(child) -> FakeProc:
    models.sizes_cache[QWEN.id] = {f: 100 for f in QWEN.files}
    models.start(QWEN)
    wait_for(lambda: child[0] and models.state["proc"] is not None)
    return child[0][-1]


def test_download_runs_to_done(paths, child):
    def finish(proc):
        for f in QWEN.files:
            write(models.target(QWEN) / f, 100)

    child[2]["call"] = finish
    proc = start_qwen(child)
    assert models.progress(QWEN)["percent"] == 0.0
    proc.end(0)
    wait_for(lambda: models.state["status"] != "running")
    p = models.progress(QWEN)
    assert (p["status"], p["percent"], p["error"]) == ("done", 100.0, None)
    assert p["total_gb"] == round(500 / 1e9, 2)


def test_speed_and_time_left_follow_the_bytes_on_disk(paths, child, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(models, "clock", lambda: now[0])
    models.sizes_cache[QWEN.id] = {f: 10**9 for f in QWEN.files}  # 5 GB
    models.start(QWEN)
    wait_for(lambda: models.state.get("proc") is not None)
    partial = models.partial_dir(QWEN) / "a.incomplete"
    partial.parent.mkdir(parents=True, exist_ok=True)

    def at(t: float, size: int) -> dict:
        now[0] = 100.0 + t
        with partial.open("wb") as f:
            f.truncate(size)  # sparse: no real bytes written
        return models.progress(QWEN)

    p = at(0, 0)
    assert (p["speed_mbs"], p["eta_s"]) == (None, None)
    p = at(1, 10**8)  # under MIN_SPAN_S: no speed yet
    assert (p["speed_mbs"], p["eta_s"]) == (None, None)
    p = at(4, 2 * 10**8)
    assert (p["speed_mbs"], p["eta_s"]) == (50.0, 96)  # 4.8 GB left at 50 MB/s
    # samples older than the window drop out, so a change of speed shows within about SPEED_WINDOW_S
    at(14, 7 * 10**8)
    p = at(24, 12 * 10**8)
    assert p["speed_mbs"] == 50.0
    p = at(34, 22 * 10**8)
    # (2.2 - 1.2) GB over the 10 s since the anchor at t=24
    assert p["speed_mbs"] == 100.0
    assert p["eta_s"] == 28
    # a stall gives speed 0 and no time left
    at(46, 22 * 10**8)
    p = at(58, 22 * 10**8)
    assert (p["speed_mbs"], p["eta_s"]) == (0.0, None)


def test_a_finished_download_has_no_speed(paths, child):
    proc = start_qwen(child)
    models.progress(QWEN)
    proc.end(1, "failed")
    wait_for(lambda: models.state["status"] != "running")
    p = models.progress(QWEN)
    assert (p["status"], p["speed_mbs"], p["eta_s"]) == ("failed", None, None)


def test_one_download_at_a_time(paths, child):
    start_qwen(child)
    with pytest.raises(ValueError, match="is downloading"):
        models.start(LTX)


def test_cancel_kills_the_child_and_deletes_partial_files(paths, child):
    proc = start_qwen(child)
    finished = models.target(QWEN) / "config.json"
    write(finished, 100)
    partial = models.partial_dir(QWEN) / "x.y.incomplete"
    write(partial, 50)
    assert models.cancel(QWEN) == "cancelled"
    wait_for(lambda: models.state["proc"] is None)
    assert child[1] == [(proc.pid, signal.SIGKILL)]
    assert models.state["status"] == "cancelled"
    assert not partial.exists() and finished.exists()
    assert models.cancel(QWEN) == "cancelled"
    models.start(QWEN)  # the next download may start
    wait_for(lambda: len(child[0]) == 2)


def test_cancel_before_the_child_starts(paths, child, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(models, "remote_sizes", lambda m: gate.wait(5) and {"a": 1})
    models.start(QWEN)
    assert models.cancel(QWEN) == "cancelled"
    gate.set()
    time.sleep(0.1)
    assert child[0] == [] and child[1] == []


def test_cancel_of_a_model_that_is_not_downloading():
    with pytest.raises(ValueError, match="not downloading"):
        models.cancel(QWEN)


def test_child_failure_shows_its_last_line(paths, child):
    proc = start_qwen(child)
    proc.end(1, "warning\nCould not reach Hugging Face. Check the network.\n")
    wait_for(lambda: models.state["status"] != "running")
    assert models.state["error"] == "Could not reach Hugging Face. Check the network."


def test_a_finished_child_with_missing_files_fails(paths, child):
    proc = start_qwen(child)
    proc.end(0)
    wait_for(lambda: models.state["status"] != "running")
    assert models.state["status"] == "failed"
    assert "config.json" in models.state["error"]


def test_gated_repo_fails_before_the_child(paths, child, monkeypatch):
    class Gated:
        def list_repo_tree(self, *args, **kwargs):
            raise http_error(GatedRepoError, 403)

    monkeypatch.setattr(models, "HfApi", Gated)
    models.start(LTX)
    wait_for(lambda: models.state["status"] != "running")
    assert models.state["status"] == "failed"
    assert "https://huggingface.co/dgrauet/ltx-2.5-mlx-q8" in models.state["error"]
    assert "hf auth login" in models.state["error"]
    assert child[0] == []


def test_no_space_fails_before_the_child(paths, child, monkeypatch):
    models.sizes_cache[QWEN.id] = {"a": 5 * 10**9}
    stale = models.partial_dir(QWEN) / "old.run.incomplete"
    write(stale, 10**6)  # from an earlier run: deleted, and not counted as progress
    monkeypatch.setattr(models, "disk_free", lambda m: 10**9)
    models.start(QWEN)
    wait_for(lambda: models.state["status"] != "running")
    assert models.state["error"].startswith("Not enough disk space")
    assert "5.0 GB more" in models.state["error"]
    assert not stale.exists()
    assert child[0] == []


def test_refusals(paths):
    with pytest.raises(ValueError, match="commands"):
        models.start(models.BY_ID["mflux"])
    with pytest.raises(ValueError, match="checkout first"):
        models.start(ACE)
    install(QWEN)
    with pytest.raises(ValueError, match="already installed"):
        models.start(QWEN)


# error messages


@pytest.mark.parametrize(
    "error",
    [
        http_error(GatedRepoError, 403),
        http_error(GatedRepoError, 401),
        http_error(HfHubHTTPError, 401),
    ],
)
def test_gated_or_logged_out_message(error):
    msg = models.explain(error, LTX)
    assert "Accept its licence on https://huggingface.co/dgrauet/ltx-2.5-mlx-q8" in msg
    assert "`.venv/bin/hf auth login`" in msg


def test_other_error_messages():
    no_space = OSError(errno.ENOSPC, "No space left on device")
    assert models.explain(no_space, QWEN).startswith("The disk is full")
    offline = httpx2.ConnectError("connection refused")
    assert models.explain(offline, QWEN).startswith("Could not reach Hugging Face")
    assert models.explain(ValueError("odd"), QWEN) == "Download failed: ValueError: odd"


# the child's work


def test_fetch_into_the_configured_folder(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(
        models, "snapshot_download", lambda *a, **kw: calls.append((a, kw))
    )
    models.fetch(LTX)
    assert calls == [
        (
            (LTX.repo,),
            {
                "revision": LTX.revision,
                "allow_patterns": None,
                "ignore_patterns": ["transformer-dev.safetensors"],
                "local_dir": paths / "ltx_model",
            },
        )
    ]


def test_fetch_into_the_cache_writes_the_main_ref(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(models, "snapshot_download", lambda *a, **kw: calls.append(kw))
    models.fetch(ASR)
    assert calls[0]["cache_dir"] == paths / "hub"
    ref = models.repo_cache(ASR) / "refs" / "main"
    assert ref.read_text() == ASR.revision
    ref.write_text("f" * 40)  # a ref that is already there stays
    models.fetch(ASR)
    assert ref.read_text() == "f" * 40


# endpoints


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:7870")


def test_get_models(client, paths, monkeypatch):
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body["machine"]["mem_total_gb"] == 48.0
    assert [m["id"] for m in body["models"]] == [m.id for m in models.MODELS]


def test_download_and_cancel_endpoints(client, paths, child):
    models.sizes_cache[QWEN.id] = {f: 100 for f in QWEN.files}
    assert client.post("/api/models/nope/download").status_code == 404
    r = client.post("/api/models/mflux/download")
    assert r.status_code == 400 and "commands" in r.json()["detail"]
    r = client.post(f"/api/models/{QWEN.id}/download")
    assert r.json() == {"status": "running"}
    wait_for(lambda: models.state["proc"] is not None)
    r = client.post(f"/api/models/{LTX.id}/download")
    assert r.status_code == 400 and "is downloading" in r.json()["detail"]
    assert client.post(f"/api/models/{LTX.id}/cancel").status_code == 400
    r = client.post(f"/api/models/{QWEN.id}/cancel")
    assert r.json() == {"status": "cancelled"}


def test_writes_from_another_site_are_refused(client, paths, child):
    r = client.post(
        f"/api/models/{QWEN.id}/download", headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert models.state == {}
