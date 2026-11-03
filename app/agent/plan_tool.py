"""update_plan (R0): the model writes or updates a short checklist for the answer. The user sees it in the chat.

The tool only checks and formats the plan. The chat loop (app/chat.py) stores the checked items with the call's tool
message and sends them to the page as a "plan" event, so the page shows one checklist per answer and updates it in
place. Its result holds no outside text, so it never taints the chat.
"""

from typing import Any

from app.agent.policy import Risk
from app.agent.registry import Context, Tool, ToolError

NAME = "update_plan"
STATUSES = ("pending", "in_progress", "done")
MAX_ITEMS = 10
MAX_STEP = 200  # characters of one item

PARAMETERS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "The whole plan, in order. Send every item each time, also the done ones.",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "string",
                        "description": "One short step, in the user's language.",
                        "minLength": 1,
                        "maxLength": MAX_STEP,
                    },
                    "status": {"type": "string", "enum": list(STATUSES)},
                },
                "required": ["step", "status"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Write or update the plan of this answer: a short checklist that the user sees. Use it for a task with three or "
    "more steps. Call it before the first step, then again when a step is done. Send the whole list each time. At "
    "most one item is in_progress."
)


def items(args: dict[str, Any]) -> list[dict[str, str]]:
    """The plan of checked arguments, as the page shows it: [{"step", "status"}]."""
    return [
        {"step": " ".join(i["step"].split()), "status": i["status"]}
        for i in args["items"]
    ]


def run(args: dict[str, Any], ctx: Context) -> str:
    plan = items(args)
    if not plan:
        raise ToolError("The plan is empty. Send at least one item.")
    if sum(1 for i in plan if i["status"] == "in_progress") > 1:
        raise ToolError(
            "More than one item is in_progress. Mark only the current step."
        )
    done = sum(1 for i in plan if i["status"] == "done")
    now = next((i["step"] for i in plan if i["status"] == "in_progress"), None)
    text = f"Plan saved: {done} of {len(plan)} done."
    if now:
        text += f" Current step: {now}"
    elif done == len(plan):
        text += " Every step is done: give the user the final answer."
    return text


def build() -> list[Tool]:
    return [
        Tool(
            NAME,
            DESCRIPTION,
            PARAMETERS,
            Risk.R0,
            run,
            explain=lambda args: "Updates the plan of this answer.",
        )
    ]
