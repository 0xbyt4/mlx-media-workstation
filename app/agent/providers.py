"""The providers this app loads (app/agent/extensions.py). Each provider module adds one line to load()."""

from app.agent import mcp_tools
from app.agent.extensions import Provider


def load() -> list[Provider]:
    """The providers in the order their sections appear in the system prompt."""
    found: list[Provider] = []
    found.append(mcp_tools.provider())
    return found
