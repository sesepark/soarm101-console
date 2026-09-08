from __future__ import annotations

import os
import signal
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Deliberate boundary exception: the lock contract is shared with worker processes. HUBq must not
# import soarm_console.app or any of its manager objects.
from soarm_console.owner_lock import read_lock_ledger

from .claims import decide_claim
from .jobs import JobConflict, JobError, JobRegistry


app = FastAPI(title="HUBq", version="0.1.0")
jobs = JobRegistry()
registrations: dict[str, dict[str, object]] = {}


class ClaimRequest(BaseModel):
    kind: str
    devices: list[str] = Field(min_length=1, max_length=16)


class JobRequest(BaseModel):
    kind: str
    owner: str
    devices: dict[str, str]
    env: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    sidecars: dict[str, list[str]] = Field(default_factory=dict)
    confirmed: bool = False


class StopRequest(BaseModel):
    timeout: float | None = Field(default=None, gt=0, le=120)


class RegistrationRequest(BaseModel):
    owner: str
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


@app.post("/jobs", status_code=201)
def start_job(body: JobRequest) -> dict[str, object]:
    try:
        return jobs.start(
            kind=body.kind,
            owner=body.owner,
            devices=body.devices,
            env=body.env,
            metadata=body.metadata,
            sidecars=body.sidecars,
            confirmed=body.confirmed,
        )
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except JobError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/jobs")
def list_jobs(kind: str | None = None, active: bool = False) -> list[dict[str, object]]:
    return jobs.list(kind=kind, active=active)


@app.get("/jobs/{job_id}")
def describe_job(job_id: str) -> dict[str, object]:
    try:
        return jobs.describe(job_id)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str, body: StopRequest | None = None) -> dict[str, object]:
    try:
        return jobs.stop(job_id, timeout=body.timeout if body is not None else None)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/registrations/{name}")
def register(name: str, body: RegistrationRequest) -> dict[str, object]:
    registration = {"name": name, "owner": body.owner, "devices": body.devices}
    registrations[name] = registration
    return registration


@app.delete("/registrations/{name}")
def unregister(name: str) -> dict[str, bool]:
    registrations.pop(name, None)
    return {"released": True}


@app.post("/shutdown")
def shutdown() -> dict[str, bool]:
    """Stop only the HUBq daemon; its child process groups deliberately survive."""
    threading.Timer(0.1, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
    return {"stopping": True}
