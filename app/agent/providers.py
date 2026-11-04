"""The providers this app loads (app/agent/extensions.py). Each provider module adds one line to load()."""

from app import memory, skills
from app.agent import host_tools, mcp_tools
from app.agent.extensions import Provider


def load() -> list[Provider]:
    """The providers in the order their sections appear in the system prompt."""
    found: list[Provider] = []
    found.append(host_tools.HostTools())
    found.append(memory.Provider())
    found.append(mcp_tools.provider())
    found.append(skills.SkillsProvider())
    return found
