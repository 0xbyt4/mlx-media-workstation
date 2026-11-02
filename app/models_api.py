"""The Models screen's endpoints (app/models.py does the work). Writes pass the server's same-origin check."""

from typing import Any

from fastapi import APIRouter, HTTPException

from app import models

router = APIRouter(prefix="/api/models")


def lookup(model_id: str) -> models.Model:
    m = models.BY_ID.get(model_id)
    if m is None:
        raise HTTPException(404, "No such model")
    return m


@router.get("")
def get_models() -> dict[str, Any]:
    return models.catalog()


@router.post("/{model_id}/download")
def download(model_id: str) -> dict[str, str]:
    try:
        models.start(lookup(model_id))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"status": "running"}


@router.post("/{model_id}/cancel")
def cancel(model_id: str) -> dict[str, str]:
    try:
        return {"status": models.cancel(lookup(model_id))}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
