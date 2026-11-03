"""A WorkstationPort stand-in for the agent tool tests: a few tools, jobs and Library entries, recorded create_job
calls with the input files they copied, and waits that a test steers (finish, block)."""

import threading
from pathlib import Path
from typing import Any

from app.agent.ports import InputFile

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
        "id": "image.klein_edit",
        "group": "Image",
        "label": "Klein Edit",
        "description": "Edit with references.",
        "installed": True,
        "fields": [
            {
                "name": "references",
                "label": "Reference images",
                "type": "files",
                "accept": "image/*",
                "max": 10,
                "required": True,
            },
            {"name": "prompt", "label": "Prompt", "type": "textarea", "required": True},
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
TRANSCRIBE = {
    "id": "voice.transcribe",
    "group": "Voice",
    "label": "Transcribe",
    "description": "Speech to text.",
    "installed": True,
    "fields": [
        {
            "name": "audio",
            "label": "Audio or video",
            "type": "file",
            "accept": "audio/*,video/*",
            "required": True,
        },
        {
            "name": "language",
            "label": "Language",
            "type": "select",
            "default": "auto",
            "options": [{"value": "auto"}, {"value": "English"}, {"value": "German"}],
        },
    ],
}


class FakeWorkstation:
    """root: where the outputs of finished jobs are (output_file) and where create_job copies its input files."""

    def __init__(self, root: Path | None = None, transcribe: bool = False) -> None:
        self.root = root
        self.tools = TOOLS + ([TRANSCRIBE] if transcribe else [])
        self.created: list[tuple[str, dict[str, str]]] = []
        # per created job: {field: [(the name the file came with, the bytes copied)]}
        self.inputs: list[dict[str, list[tuple[str, bytes]]]] = []
        self.waits: list[tuple[str, float]] = []
        # job id -> what the job becomes during the next wait for it (keys merged into the job)
        self.finish: dict[str, dict[str, Any]] = {}
        self.block = False  # a wait lasts its whole time unless abort is set
        self.cancelled: list[str] = []
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
        return self.tools

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int) -> list[dict[str, Any]]:
        return sorted(self.jobs.values(), key=lambda j: j["id"], reverse=True)[:limit]

    def wait_job(
        self, job_id: str, seconds: float, abort: threading.Event
    ) -> dict[str, Any] | None:
        self.waits.append((job_id, seconds))
        if self.block:
            abort.wait(seconds)
        elif job_id in self.finish and job_id in self.jobs:
            self.jobs[job_id].update(self.finish.pop(job_id))
        return self.jobs.get(job_id)

    def cancel_job(self, job_id: str) -> str:
        if job_id not in self.jobs:
            raise ValueError("No such job")
        self.cancelled.append(job_id)
        self.jobs[job_id]["status"] = "cancelled"
        return "cancelled"

    def output_file(self, job_id: str, name: str) -> Path | None:
        job = self.jobs.get(job_id)
        if self.root is None or job is None or job["status"] != "done":
            return None
        if name not in [o["name"] for o in job["outputs"]]:
            return None
        return self.root / job_id / name

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [
            i
            for i in self.library
            if q in i["text"].lower() or q in i["tool"] or q in i["name"]
        ]
        return hits[:limit]

    def create_job(
        self,
        tool: str,
        fields: dict[str, str],
        inputs: dict[str, list[InputFile]] | None = None,
        chat: str | None = None,
    ) -> str:
        if fields.get("steps") == "999":
            raise ValueError("Steps must be between 1 and 50")
        copied: dict[str, list[tuple[str, bytes]]] = {}
        for name, given in (inputs or {}).items():
            for i, inp in enumerate(given):
                dst = self.root / f"copy-{len(self.inputs)}-{name}-{i}"
                inp.write(dst)
                copied.setdefault(name, []).append((inp.name, dst.read_bytes()))
        self.created.append((tool, fields))
        self.inputs.append(copied)
        job_id = f"20261103-130000-{len(self.created):04x}"
        label = next(t["label"] for t in self.tools if t["id"] == tool)
        self.jobs[job_id] = {
            "id": job_id,
            "tool": tool,
            "tool_label": label,
            "status": "queued",
            "position": 1,
            "created": "2026-11-03T13:00:00",
            "outputs": [],
            "error": None,
            "chat": chat,
        }
        return job_id
