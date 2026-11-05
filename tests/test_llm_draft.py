"""Faster answers with the DFlash2 draft model: the switch, the refusal without the draft, and the worker's pure parts
of speculative decoding (verify_walk, ThinkingBudget, Tokens), without MLX. The fake worker stands in for the model."""

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, llm, models, settings_api
from app.llm_worker import ThinkingBudget, Tokens, verify_walk

FAKE = Path(__file__).with_name("fake_llm_worker.py")
ORIGIN = {"origin": "http://127.0.0.1:7870"}


@pytest.fixture
def draft_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The draft folder of the config, empty: the draft is not installed until a test writes its files."""
    folder = tmp_path / "qwen3.8-27b-dflash2"
    paths = replace(config.PATHS, qwen_draft=folder)
    monkeypatch.setattr(models, "PATHS", paths)
    return folder


def install(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in llm.DRAFT.files:
        (folder / name).write_text("{}")


@pytest.fixture
def fake_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft_dir: Path):
    monkeypatch.setattr(
        llm,
        "PATHS",
        SimpleNamespace(
            qwen_python=Path(sys.executable),
            qwen_model=Path("ok"),
            qwen_draft=draft_dir,
        ),
    )
    monkeypatch.setattr(llm, "WORKER", [str(FAKE)])
    monkeypatch.setattr(llm, "installed", lambda: True)
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.log")
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(llm, "closed", False)
    yield draft_dir
    llm.close("test over")
    llm.release()


def draft_on(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    monkeypatch.setattr(config, "APP", replace(config.APP, llm_draft=on))


def seen(**kw) -> dict:
    return llm.complete([{"role": "user", "content": "hi"}], **kw)["stats"]["seen"]


# ---------- the switch ----------


def test_the_draft_is_off_by_default(fake_llm):
    assert config.APP_DEFAULTS["llm_draft"] is False
    assert seen().get("draft") is None
    assert llm.peak_gb() == llm.MODEL.peak_gb
    assert llm.status()["draft"] == {"on": False, "installed": False}


def test_on_sends_the_draft_folder_and_counts_its_memory(fake_llm, monkeypatch):
    install(fake_llm)
    draft_on(monkeypatch)
    assert seen()["draft"] == str(fake_llm)
    assert seen(tools=[])["draft"] == str(fake_llm)
    assert llm.peak_gb() == llm.MODEL.peak_gb + llm.DRAFT.peak_gb
    assert llm.status()["draft"] == {"on": True, "installed": True}
    assert llm.status()["peak_gb"] == llm.peak_gb()


def test_the_draft_memory_counts_for_media_jobs(fake_llm, monkeypatch):
    install(fake_llm)
    job = 48.0 - models.RESERVE_GB - llm.MODEL.peak_gb  # fits next to the model alone
    assert llm.fits_with(job)
    draft_on(monkeypatch)
    assert not llm.fits_with(job)


def test_on_without_the_draft_is_refused_with_the_way_out(fake_llm, monkeypatch):
    draft_on(monkeypatch)
    with pytest.raises(llm.LLMError) as e:
        seen()
    assert str(fake_llm) in str(e.value)
    assert "Models screen" in str(e.value) and "Faster answers" in str(e.value)
    assert llm.worker is None  # refused before a model loads


def test_turning_it_off_applies_to_the_next_request(fake_llm, monkeypatch):
    install(fake_llm)
    draft_on(monkeypatch)
    assert seen()["draft"] == str(fake_llm)
    draft_on(monkeypatch, False)
    assert (
        seen().get("draft") is None
    )  # the worker drops a loaded draft at such a request


# ---------- Settings ----------


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    file = tmp_path / "config.toml"
    file.write_text("[app]\nnotify = true\n")
    monkeypatch.setattr(config, "FILE", file)
    monkeypatch.setattr(config, "APP", config.load_app(file))
    return file


def test_settings_refuse_the_switch_without_the_draft(
    client: TestClient, cfg: Path, draft_dir: Path
) -> None:
    r = client.put("/api/settings", json={"app": {"llm_draft": True}}, headers=ORIGIN)
    assert r.status_code == 400
    message = r.json()["detail"]["fields"]["app.llm_draft"]
    assert str(draft_dir) in message and "Models screen" in message
    assert "llm_draft" not in cfg.read_text() and config.APP.llm_draft is False
    assert client.get("/api/settings").json()["draft"] == {
        "installed": False,
        "gb": llm.DRAFT.peak_gb,
    }


def test_settings_turn_the_draft_on_and_off(
    client: TestClient, cfg: Path, draft_dir: Path
) -> None:
    install(draft_dir)
    r = client.put("/api/settings", json={"app": {"llm_draft": True}}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["app"]["llm_draft"] is True and r.json()["draft"]["installed"]
    assert "llm_draft = true" in cfg.read_text() and config.APP.llm_draft is True
    assert llm.draft_on()
    # turning it off never needs the draft
    draft_dir.joinpath("model.safetensors").unlink()
    r = client.put("/api/settings", json={"app": {"llm_draft": False}}, headers=ORIGIN)
    assert r.status_code == 200 and config.APP.llm_draft is False


def test_the_draft_has_a_path_and_a_pinned_download() -> None:
    assert config.DEFAULTS["qwen_draft"] == "~/mlx-media/models/qwen3.8-27b-dflash2"
    assert "qwen_draft" in settings_api.LABELS
    assert llm.DRAFT.repo == "z-lab/Qwen3.8-27B-DFlash2"
    assert llm.DRAFT.revision == "50307d4c4cde6860d4eee73e2547cd786fe8e8a4"
    assert llm.DRAFT.allow == llm.DRAFT.files == ("config.json", "model.safetensors")
    # the setup card never picks it and the Models dot does not wait for it; the chat profile shows it
    assert llm.DRAFT.optional and "Chat" in llm.DRAFT.features
    assert models.view(llm.DRAFT, 48.0)["optional"] is True
    assert not llm.MODEL.optional


# ---------- one speculative round (verify_walk) ----------


def walk(drafts, targets, stops=(), limit=99):
    """verify_walk with the model's tokens at each position given; records the positions it sampled."""
    asked = []

    def sample(i):
        asked.append(i)
        return targets[i]

    out = verify_walk(drafts, sample, lambda t, n: t in stops or n >= limit)
    return (*out, asked)


def test_all_drafts_kept_and_the_model_adds_one():
    assert walk([5, 6, 7], [5, 6, 7, 8]) == ([5, 6, 7, 8], 3, False, [0, 1, 2, 3])


def test_the_walk_stops_at_the_first_token_that_differs():
    # the model's token replaces the draft's; nothing after it is sampled
    assert walk([5, 6, 7], [5, 9, 7, 8]) == ([5, 9], 1, False, [0, 1])
    assert walk([5, 6, 7], [4, 6, 7, 8]) == ([4], 0, False, [0])


def test_a_stop_token_from_the_draft_stays_in_the_cache():
    # 2 is a stop token that the draft guessed: the cache keeps it, the rest of the block goes
    assert walk([5, 2, 7], [5, 2, 7, 8], stops={2}) == ([5, 2], 2, True, [0, 1])


def test_a_stop_token_from_the_model_is_not_in_the_cache_yet():
    assert walk([5, 6, 7], [5, 2, 7, 8], stops={2}) == ([5, 2], 1, False, [0, 1])
    assert walk([5, 6], [5, 6, 2], stops={2}) == ([5, 6, 2], 2, False, [0, 1, 2])


def test_max_tokens_ends_the_walk():
    assert walk([5, 6, 7], [5, 6, 7, 8], limit=2) == ([5, 6], 2, True, [0, 1])


# ---------- the processors of the speculative path ----------


class Row(list):
    @property
    def shape(self):
        return (len(self),)


class Lists:
    @staticmethod
    def array(value):
        return value

    @staticmethod
    def where(mask, x, y):
        return Row(x if m else v for m, v in zip(mask, y, strict=True))


class Criteria:
    """Records the tokens it gets and forces 7 after token 3, like ThinkingBudgetCriteria past its budget."""

    def __init__(self, preopens):
        self.preopens, self.got = preopens, []

    def __call__(self, token):
        self.got.append(token)
        return 7 if token == 3 else None


def test_thinking_budget_sees_each_new_token_once_and_forces_by_ban():
    made = []
    proc = ThinkingBudget(Lists, lambda pre: made.append(Criteria(pre)) or made[-1], 1)
    logits = Row([0.0] * 9)
    assert (
        proc([0, 1], logits) is logits
    )  # the prompt part opens the thinking (token 1)
    assert made[0].preopens is True
    assert proc([0, 1, 2], logits) is logits
    forced = proc([0, 1, 2, 3], logits)
    assert [i for i, v in enumerate(forced) if v != -float("inf")] == [7]
    assert made[0].got == [2, 3]  # the prompt is not a new token


def test_thinking_budget_without_the_start_in_the_prompt():
    made = []
    proc = ThinkingBudget(Lists, lambda pre: made.append(Criteria(pre)) or made[-1], 1)
    proc([0, 4], Row([0.0] * 9))
    assert made[0].preopens is False


def test_tokens_keeps_the_last_history_and_changes_nothing():
    rec = Tokens()
    logits = Row([1.0, 2.0])
    assert rec([1, 2], logits) is logits and rec.tokens == [1, 2]
