"""The Models screen's endpoints (app/models.py does the work). Writes pass the server's same-origin check."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import models

router = APIRouter(prefix="/api/models")


class QueueBody(BaseModel):
    ids: list[str]


def lookup(model_id: str) -> models.Model:
    m = models.BY_ID.get(model_id)
    if m is None:
        raise HTTPException(404, "No such model")
    return m


@router.get("")
def get_models() -> dict[str, Any]:
    return models.catalog()


@router.post("/queue")
def add_to_queue(body: QueueBody) -> dict[str, Any]:
    if not body.ids:
        raise HTTPException(400, "Pick at least one model.")
    try:
        added = models.add(body.ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {**added, "queue": models.summary()}


@router.post("/queue/clear")
def clear_queue() -> dict[str, Any]:
    return {"removed": models.clear_queue(), "queue": models.summary()}


@router.post("/{model_id}/download")
def download(model_id: str) -> dict[str, Any]:
    m = lookup(model_id)
    try:
        status = models.start(m)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    p = models.progress(m)
    return {"status": status, "position": p["position"] if p else None}


@router.post("/{model_id}/cancel")
def cancel(model_id: str) -> dict[str, str]:
    try:
        return {"status": models.cancel(lookup(model_id))}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
