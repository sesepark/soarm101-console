from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Deliberate boundary exception: the lock contract is shared with worker processes. HUBq must not
# import soarm_console.app or any of its manager objects.
from soarm_console.owner_lock import read_lock_ledger

from .claims import decide_claim


app = FastAPI(title="HUBq", version="0.1.0")


class ClaimRequest(BaseModel):
    kind: str
    devices: list[str] = Field(min_length=1, max_length=16)


@app.get("/status")
def status() -> dict[str, object]:
    """Report kernel-backed device ownership without acquiring any device lock."""
    devices = read_lock_ledger()
    return {
        "devices": devices,
        "locked": sum(1 for device in devices if device["locked"]),
    }


@app.post("/claim", response_model=None)
def claim(body: ClaimRequest) -> dict[str, object] | JSONResponse:
    try:
        decision = decide_claim(body.kind, body.devices)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = decision.as_dict()
    if not decision.allowed:
        return JSONResponse(status_code=409, content=payload)
    return payload
