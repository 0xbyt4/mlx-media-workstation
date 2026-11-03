"""The agent tool registry: argument checks, schemas for the chat template, result caps and error handling."""

import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from app import config
from app.agent import (
    MAX_RESULT,
    Context,
    Registry,
    Risk,
    Settings,
    Tool,
    ToolError,
    build_registry,
    load_settings,
    schema,
)
from app.agent import settings as agent_settings
from app.agent.registry import Cancelled, cap
from tests.fake_workstation import FakeWorkstation

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 10},
        "n": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
        "ratio": {"type": "number"},
        "deep": {"type": "boolean", "default": False},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "fields": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "boolean"]},
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def ctx(tmp_path: Path, abort: threading.Event | None = None) -> Context:
    return Context("chat1", None, abort or threading.Event(), tmp_path / "ws")


def test_check_fills_defaults_and_keeps_given_values() -> None:
    assert schema.check(SCHEMA, {"query": "x"}) == {
        "query": "x",
        "n": 3,
        "deep": False,
    }
    got = schema.check(
        SCHEMA,
        {"query": "x", "n": 5, "ratio": 2, "tags": ["a"], "fields": {"s": 1}},
    )
    assert got["n"] == 5 and got["ratio"] == 2 and got["tags"] == ["a"]


def test_a_whole_float_counts_as_an_integer() -> None:
    assert schema.check(SCHEMA, {"query": "x", "n": 4.0})["n"] == 4


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "must be a JSON object"),
        ({}, "query: required"),
        ({"query": ""}, "query: must not be empty"),
        ({"query": "x" * 11}, "query: at most 10 characters"),
        ({"query": 5}, "query: expected string"),
        ({"query": "x", "n": True}, "n: expected integer"),
        ({"query": "x", "n": 2.5}, "n: expected integer"),
        ({"query": "x", "n": 0}, "n: must be at least 1"),
        ({"query": "x", "n": 6}, "n: must be at most 5"),
        ({"query": "x", "ratio": "1"}, "ratio: expected number"),
        ({"query": "x", "deep": "yes"}, "deep: expected boolean"),
        ({"query": "x", "mode": "c"}, "mode: must be one of 'a', 'b'"),
        ({"query": "x", "tags": ["a", "b", "c"]}, "tags: at most 2 items"),
        ({"query": "x", "tags": [1]}, "tags[0]: expected string"),
        ({"query": "x", "fields": {"k": [1]}}, "fields.k: expected string or number"),
        ({"query": "x", "other": 1}, "other: unknown argument"),
    ],
)
def test_check_refuses_bad_arguments(args: Any, message: str) -> None:
    with pytest.raises(schema.ArgumentError, match=message.replace("[", r"\[")):
        schema.check(SCHEMA, args)


def test_cap_keeps_short_text_and_marks_long_text() -> None:
    assert cap("short") == ("short", False)
    text, cut = cap("x" * 50000)
    assert cut and len(text) == MAX_RESULT
    assert text.endswith("[Truncated: showing the first 12,000 of 50,000 characters.]")


def echo_tool(run: Any = None, **kw: Any) -> Tool:
    return Tool(
        name="echo",
        description="Echo",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        risk=Risk.R0,
        run=run or (lambda a, c: a["text"]),
        **kw,
    )


def test_call_checks_arguments_before_running(tmp_path: Path) -> None:
    ran = []
    reg = Registry([echo_tool(lambda a, c: ran.append(a) or "ok")])
    r = reg.call("echo", {"text": 1}, ctx(tmp_path))
    assert r.status == "error" and "text: expected string" in r.content
    assert ran == [] and r.args is None


def test_call_reports_errors_cancel_and_unknown_tools(tmp_path: Path) -> None:
    def fail(a: Any, c: Any) -> str:
        raise ToolError("No such file: a.txt")

    def crash(a: Any, c: Any) -> str:
        raise KeyError("x")

    def stop(a: Any, c: Any) -> str:
        raise Cancelled

    c = ctx(tmp_path)
    assert Registry([echo_tool(fail)]).call("echo", {"text": "a"}, c).content == (
        "No such file: a.txt"
    )
    r = Registry([echo_tool(crash)]).call("echo", {"text": "a"}, c)
    assert r.status == "error" and "KeyError" in r.content
    assert Registry([echo_tool(stop)]).call("echo", {"text": "a"}, c).status == (
        "cancelled"
    )
    r = Registry([echo_tool()]).call("nope", {}, c)
    assert r.status == "error" and "Tools: echo" in r.content
    stopped = ctx(tmp_path, threading.Event())
    stopped.abort.set()
    assert Registry([echo_tool()]).call("echo", {"text": "a"}, stopped).status == (
        "cancelled"
    )


def test_call_caps_the_model_text_and_keeps_the_full_text(tmp_path: Path) -> None:
    r = Registry([echo_tool()]).call("echo", {"text": "y" * 20000}, ctx(tmp_path))
    assert r.status == "ok" and r.truncated
    assert len(r.content) == MAX_RESULT and len(r.full) == 20000


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Registry([echo_tool(), echo_tool()])


def test_schemas_are_openai_function_schemas() -> None:
    reg = build_registry(FakeWorkstation(), Settings())
    names = reg.names()
    assert names[:4] == [
        "list_workstation_tools",
        "job_status",
        "search_library",
        "create_job",
    ]
    assert {"list_files", "read_file", "search_files", "web_search", "fetch_url"} <= (
        set(names)
    )
    assert "run_shell" not in names and "shell" not in names
    for s in reg.schemas():
        assert s["type"] == "function"
        f = s["function"]
        assert set(f) == {"name", "description", "parameters"}
        assert f["parameters"]["type"] == "object"
        assert f["parameters"]["additionalProperties"] is False
    assert [s["function"]["name"] for s in reg.schemas(["fetch_url", "x"])] == [
        "fetch_url"
    ]


def test_risk_levels_of_the_tools() -> None:
    reg = build_registry(FakeWorkstation(), Settings())
    risks = {n: reg.get(n).risk for n in reg.names()}
    assert risks["list_workstation_tools"] == risks["job_status"] == Risk.R0
    assert risks["search_library"] == Risk.R0
    assert risks["create_job"] == Risk.R2
    assert risks["list_files"] == risks["read_file"] == risks["search_files"] == Risk.R1
    assert risks["web_search"] == risks["fetch_url"] == Risk.R2
    if "run_python" in risks:
        assert risks["run_python"] == Risk.R3
    assert reg.get("web_search").network and reg.get("fetch_url").network
    assert not reg.get("create_job").network
    untrusted = {n for n in reg.names() if reg.get(n).untrusted_output}
    assert untrusted == {
        "search_library",
        "list_files",
        "read_file",
        "search_files",
        "web_search",
        "fetch_url",
    } | ({"run_python"} if "run_python" in risks else set())


def test_chat_only_setup_has_no_workstation_tools() -> None:
    names = build_registry(None, Settings()).names()
    assert not {"list_workstation_tools", "create_job"} & set(names)


def test_settings_from_config(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    assert load_settings(file) == Settings()
    file.write_text(
        '[agent]\npython = "/opt/sandbox/bin/python"\n'
        'searxng_url = "http://127.0.0.1:8888/"\nwikipedia_language = "de"\nmarginalia = false\n'
    )
    s = load_settings(file)
    assert s.python == Path("/opt/sandbox/bin/python")
    assert s.searxng_url == "http://127.0.0.1:8888"
    assert (s.wikipedia_language, s.marginalia) == ("de", False)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('searxng_url = "ftp://x"', "searxng_url"),
        ('wikipedia_language = "en/../x"', "wikipedia_language"),
        ('marginalia = "yes"', "marginalia"),
        ("shell = true", "unknown key"),
    ],
)
def test_bad_agent_settings_stop_the_start(
    tmp_path: Path, line: str, message: str
) -> None:
    file = tmp_path / "config.toml"
    file.write_text(f"[agent]\n{line}\n")
    with pytest.raises(SystemExit, match=message):
        load_settings(file)


def test_example_config_holds_the_agent_defaults() -> None:
    example = config.ROOT / "config.example.toml"
    assert load_settings(example) == Settings()
    assert tomllib.loads(example.read_text())["agent"] == agent_settings.DEFAULTS
