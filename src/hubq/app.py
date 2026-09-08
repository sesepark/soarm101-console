from __future__ import annotations

from fastapi import FastAPI

# Deliberate boundary exception: the lock contract is shared with worker processes. HUBq must not
# import soarm_console.app or any of its manager objects.
from soarm_console.owner_lock import read_lock_ledger


app = FastAPI(title="HUBq", version="0.1.0")


@app.get("/status")
def status() -> dict[str, object]:
    """Report kernel-backed device ownership without acquiring any device lock."""
    devices = read_lock_ledger()
    return {
        "devices": devices,
        "locked": sum(1 for device in devices if device["locked"]),
    }
