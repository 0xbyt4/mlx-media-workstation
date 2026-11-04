from dataclasses import dataclass, field
from typing import Any

from app.agent import extensions, providers
from app.agent.extensions import Section, collect
from app.agent.policy import Risk
from app.agent.registry import Tool


def tool(name: str) -> Tool:
    return Tool(
        name,
        "A test tool.",
        {"type": "object", "properties": {}},
        Risk.R0,
        lambda args, ctx: "ok",
    )


@dataclass
class Fake:
    name: str
    names: list[str]
    core_names: list[str] = field(default_factory=list)
    text: str | None = None
    budget: int = 100
    fail: bool = False

    def tools(self) -> list[Tool]:
        if self.fail:
            raise OSError("no folder")
        return [tool(n) for n in self.names]

    def core(self, chat: dict[str, Any]) -> list[str]:
        return self.core_names

    def section(self, chat: dict[str, Any]) -> Section | None:
        return (
            None
            if self.text is None
            else Section(self.name.title(), self.text, self.budget)
        )


def test_providers_give_tools_core_names_and_sections_in_order() -> None:
    got = collect(
        [
            Fake("skills", ["load_skill"], ["load_skill"], "a catalog"),
            Fake("mcp", ["git__log", "git__diff"]),
        ],
        {},
    )
    assert [t.name for t in got.tools] == ["load_skill", "git__log", "git__diff"]
    assert got.core == ["load_skill"]
    assert [s.title for s in got.sections] == ["Skills"]
    assert got.problems == []


def test_a_core_name_that_is_not_a_tool_of_the_provider_is_dropped() -> None:
    assert collect([Fake("mcp", ["git__log"], ["git__log", "read_file"])], {}).core == [
        "git__log"
    ]


def test_a_broken_provider_is_left_out_and_named() -> None:
    got = collect(
        [Fake("memory", ["memory"], fail=True), Fake("skills", ["load_skill"])], {}
    )
    assert [t.name for t in got.tools] == ["load_skill"]
    assert got.problems == ["memory: OSError: no folder"]


def test_a_provider_whose_tool_names_clash_is_left_out() -> None:
    got = collect([Fake("a", ["x"]), Fake("b", ["x", "y"])], {})
    assert [t.name for t in got.tools] == ["x"]
    assert got.problems == ["b: tool names already used: x"]


def test_a_section_over_its_budget_is_left_out_with_its_tools() -> None:
    got = collect([Fake("memory", ["memory"], ["memory"], "x" * 400, budget=100)], {})
    assert got.tools == [] and got.core == [] and got.sections == []
    assert "133 tokens, over its budget of 100" in got.problems[0]
    assert extensions.tokens("x" * 300) == 100


def test_the_app_providers_load_without_problems() -> None:
    assert collect(providers.load(), {}).problems == []


def test_the_app_s_providers_have_unique_names() -> None:
    names = [p.name for p in providers.load()]
    assert len(names) == len(set(names))
