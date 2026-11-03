"""A WorkstationPort stand-in for the agent tool tests: two tools, a few jobs and Library entries, and recorded
create_job calls."""

from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "id": "image.klein",
        "group": "Image",
        "label": "FLUX.3 Klein",
        "description": "Fast images.",
        "installed": True,
        "fields": [
            {"name": "prompt", "label": "Prompt", "type": "textarea", "required": True},
            {
                "name": "size",
                "label": "Size",
                "type": "select",
                "default": "768x1344",
                "options": [{"value": "768x1344"}, {"value": "1024x1024"}],
            },
            {
                "name": "steps",
                "label": "Steps",
                "type": "number",
                "default": 4,
                "min": 1,
                "max": 50,
            },
            {"name": "seed", "label": "Seed", "type": "number", "default": None},
        ],
    },
    {
        "id": "video.i2v",
        "group": "Video",
        "label": "Image to video",
        "description": "Animate a still.",
        "installed": True,
        "fields": [
            {"name": "image", "label": "Image", "type": "file", "required": True},
            {"name": "prompt", "label": "Prompt", "type": "textarea"},
        ],
    },
    {
        "id": "music.ace",
        "group": "Music",
        "label": "ACE-Step",
        "description": "Songs.",
        "installed": False,
        "fields": [{"name": "caption", "label": "Caption", "type": "textarea"}],
    },
]


class FakeWorkstation:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, str]]] = []
        self.jobs: dict[str, dict[str, Any]] = {
            "20261103-120000-abcd": {
                "id": "20261103-120000-abcd",
                "tool": "image.klein",
                "tool_label": "FLUX.3 Klein",
                "status": "running",
                "progress": {"stage": "Denoising", "fraction": 0.42, "eta_s": 31},
                "elapsed": 12.5,
                "outputs": [],
                "error": None,
            },
            "20261103-110000-beef": {
                "id": "20261103-110000-beef",
                "tool": "image.klein",
                "tool_label": "FLUX.3 Klein",
                "status": "done",
                "elapsed": 40.0,
                "outputs": [{"name": "image.png", "kind": "image"}],
                "error": None,
            },
        }
        self.library = [
            {
                "job": "20261103-110000-beef",
                "tool": "image.klein",
                "tool_label": "FLUX.3 Klein",
                "kind": "image",
                "name": "image.png",
                "created": "2026-11-03T11:00:00",
                "text": "A red fox in the snow at dusk",
            },
            {
                "job": "20261102-090000-cafe",
                "tool": "voice.drama3",
                "tool_label": "Fish Drama 3",
                "kind": "audio",
                "name": "voice.wav",
                "created": "2026-11-02T09:00:00",
                "text": "Hello there",
            },
        ]

    def list_tools(self) -> list[dict[str, Any]]:
        return TOOLS

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [
            i
            for i in self.library
            if q in i["text"].lower() or q in i["tool"] or q in i["name"]
        ]
        return hits[:limit]

    def create_job(self, tool: str, fields: dict[str, str]) -> str:
        if fields.get("steps") == "999":
            raise ValueError("Steps must be between 1 and 50")
        self.created.append((tool, fields))
        return f"20261103-130000-{len(self.created):04x}"
