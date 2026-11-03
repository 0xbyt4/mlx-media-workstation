"""Thinking levels of a chat answer: how long the model may think before it answers (docs/API.md, "Thinking").

A level picks the chat template's reasoning effort (Qwen3.8 knows only low, medium and xhigh; any other value makes
the template fail) and a thinking budget: after that many thinking tokens mlx-vlm forces the end-of-thinking token,
so the model must answer. max_tokens is the budget plus ANSWER_TOKENS, so a cut-off thought always leaves room for the
answer. "max" has no budget and takes all the room the context has left.

The numbers come from the speed of this model on an M5 Pro: about 17 tokens a second, so about 1,000 tokens a minute.
An answer may take at most 10 minutes (chat.TURN_SECONDS for an agent answer; a waiting media job stops the model
after llm.JOB_WAIT), so "xhigh" thinks for at most about 8 minutes and leaves about 2 for the answer.

In an agent answer the budget applies to each model step: the first step plans the work and gets the full budget,
later steps get LATER_STEPS of it. The template effort stays the same in every step: it is part of the system
prompt, and a change would make the model read the whole chat again.

This module imports nothing from the app, so app/config.py can use LEVELS.
"""

from typing import Any

LEVELS = ("off", "low", "medium", "high", "xhigh", "max")
DEFAULT = "off"  # a chat that never chose a level, and the Settings default ([app] chat_effort)
THINKING_ON = "high"  # the level of the older form field thinking=true
ANSWER_TOKENS = 4096  # room for the answer after the thinking (also the whole answer with thinking off)
LATER_STEPS = 0.4  # the part of the budget an agent step after the first gets
# level: (template reasoning_effort, thinking budget in tokens); None budget: no limit ("max")
PLANS: dict[str, tuple[str | None, int | None]] = {
    "off": (None, 0),  # thinking off: the template closes the thinking block at once
    "low": ("low", 1024),  # about 1 minute
    "medium": ("medium", 3072),  # about 3 minutes
    "high": ("xhigh", 6144),  # about 6 minutes
    "xhigh": ("xhigh", 8192),  # about 8 minutes
    # until the model stops, the context is full or the answer's time ends
    "max": ("xhigh", None),
}
# the room "max" keeps for its answer when the input is built: the max_tokens of "xhigh", so it keeps as much history
MAX_RESERVE = 8192 + ANSWER_TOKENS


def parse(effort: Any, thinking: Any) -> str | None:
    """The level a message asks for: effort ("off" ... "max") wins over thinking ("true" is THINKING_ON, any other
    value is off, as before levels existed); None when it gives neither. ValueError on an unknown effort."""
    if effort is not None and str(effort) != "":
        level = str(effort).strip().lower()
        if level not in LEVELS:
            raise ValueError(
                f"Unknown thinking level: {effort}. Use {', '.join(LEVELS)}."
            )
        return level
    if thinking is not None and str(thinking) != "":
        return THINKING_ON if str(thinking).strip().lower() == "true" else "off"
    return None


def reserve(level: str) -> int:
    """The tokens the chat's input leaves free for one model request (chat.model_input's max_tokens)."""
    return MAX_RESERVE if level == "max" else options(level)["max_tokens"]


def options(level: str, step: int = 1, room: int = 0) -> dict[str, Any]:
    """The llm.generate arguments of a level: thinking, reasoning_effort, thinking_budget and max_tokens.

    step: the agent step (1 for a chat answer); room: the context tokens the prompt leaves free, which "max" takes
    (at least MAX_RESERVE)."""
    effort, budget = PLANS[level]
    if effort is None:
        return {
            "thinking": False,
            "reasoning_effort": None,
            "thinking_budget": None,
            "max_tokens": ANSWER_TOKENS,
        }
    if budget is None:
        tokens = max(MAX_RESERVE, room)
    else:
        if step > 1:
            budget = int(budget * LATER_STEPS)
        tokens = budget + ANSWER_TOKENS
    return {
        "thinking": True,
        "reasoning_effort": effort,
        "thinking_budget": budget,
        "max_tokens": tokens,
    }
