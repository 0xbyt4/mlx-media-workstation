"""The approval policy: run, ask or refuse by risk level, chat grants and untrusted content in the chat."""

from pathlib import Path
from typing import Any

import pytest

from app.agent import Action, Grants, Risk, Tool, decide, taints


def tool(name: str, risk: Risk, **kw: Any) -> Tool:
    return Tool(name, "", {"type": "object"}, risk, lambda a, c: "", **kw)


R0 = tool("job_status", Risk.R0)
R1 = tool("read_file", Risk.R1, untrusted_output=True)
SEARCH = tool("web_search", Risk.R2, network=True, untrusted_output=True)
JOB = tool("create_job", Risk.R2)
R3 = tool("run_python", Risk.R3)
WRITE = tool("write_file", Risk.R2, writes=True, needs_folder=True)
EDIT = tool("edit_file", Risk.R3, writes=True, needs_folder=True)
VIEW = tool("view_image", Risk.R1, untrusted_output=True, needs_folder=False)
FOLDER = Path("/Users/someone/Documents/project")


@pytest.mark.parametrize(
    ("t", "grants", "tainted", "action", "chat_scope"),
    [
        (R0, Grants(), False, Action.RUN, False),
        (R0, Grants(), True, Action.RUN, False),
        (R1, Grants(), False, Action.REFUSE, False),
        (R1, Grants(FOLDER), False, Action.RUN, False),
        (R1, Grants(FOLDER), True, Action.RUN, False),
        # its result taints the chat, so a chat-wide yes would not hold: only once or deny
        (SEARCH, Grants(), False, Action.ASK, False),
        (SEARCH, Grants().allow("web_search"), False, Action.RUN, False),
        # after untrusted content, a network call asks again despite the chat grant
        (SEARCH, Grants().allow("web_search"), True, Action.ASK, False),
        (SEARCH, Grants(), True, Action.ASK, False),
        (JOB, Grants(), False, Action.ASK, True),
        (JOB, Grants().allow("create_job"), False, Action.RUN, False),
        (JOB, Grants().allow("create_job"), True, Action.ASK, False),
        # R3 asks every time; a chat grant changes nothing
        (R3, Grants(), False, Action.ASK, False),
        (R3, Grants(FOLDER).allow("run_python"), False, Action.ASK, False),
        (R3, Grants(), True, Action.ASK, False),
        # writing a new file: asks first, may be allowed for the chat, asks every time once the chat is tainted
        (WRITE, Grants(), False, Action.REFUSE, False),
        (WRITE, Grants(FOLDER), False, Action.ASK, True),
        (WRITE, Grants(FOLDER).allow("write_file"), False, Action.RUN, False),
        (WRITE, Grants(FOLDER).allow("write_file"), True, Action.ASK, False),
        # changing a file asks every time
        (EDIT, Grants(), False, Action.REFUSE, False),
        (EDIT, Grants(FOLDER), False, Action.ASK, False),
        (EDIT, Grants(FOLDER).allow("edit_file"), False, Action.ASK, False),
        # an R1 tool that also reads the work folder and the Library runs without a folder
        (VIEW, Grants(), False, Action.RUN, False),
        (VIEW, Grants(FOLDER), True, Action.RUN, False),
    ],
)
def test_decide(
    t: Tool, grants: Grants, tainted: bool, action: Action, chat_scope: bool
) -> None:
    v = decide(t, grants, tainted)
    assert v.action == action
    assert v.chat_scope == chat_scope
    assert v.reason


def test_unknown_and_disabled_tools_are_refused() -> None:
    assert decide(None, Grants(), False).action == Action.REFUSE
    v = decide(R0, Grants(), False, enabled=frozenset({"web_search"}))
    assert v.action == Action.REFUSE and "off in this chat" in v.reason
    assert decide(R0, Grants(), False, enabled=frozenset({"job_status"})).action == (
        Action.RUN
    )


def test_tainted_reason_names_the_data_risk() -> None:
    assert "off this Mac" in decide(SEARCH, Grants(), True).reason
    assert "writes to your files" in decide(WRITE, Grants(FOLDER), True).reason
    assert "No folder" in decide(WRITE, Grants(), False).reason


def test_taint_sticks() -> None:
    assert taints(R0, False) is False
    assert taints(R1, False) is True
    assert taints(SEARCH, False) is True
    assert taints(R0, True) is True


def test_grants_are_immutable_values() -> None:
    g = Grants(FOLDER)
    g2 = g.allow("web_search")
    assert g.tools == frozenset() and g2.tools == {"web_search"}
    assert g2.folder == FOLDER
    assert g2.revoke("web_search").tools == frozenset()
