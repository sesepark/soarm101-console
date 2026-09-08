from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HubQError(RuntimeError):
    pass


class HubQConflict(HubQError):
    pass


def _detail(raw: bytes, fallback: str) -> str:
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return fallback


def claim(kind: str, devices: list[str]) -> None:
    base_url = os.getenv("HUBQ_URL", "http://127.0.0.1:8094").rstrip("/")
    request = Request(
        f"{base_url}/claim",
        data=json.dumps({"kind": kind, "devices": devices}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=1.0) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        detail = _detail(exc.read(), f"HUBq returned HTTP {exc.code}")
        if exc.code == 409:
            raise HubQConflict(detail) from exc
        raise HubQError(detail) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise HubQError(f"HUBq is unavailable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("allowed") is not True:
        raise HubQError("HUBq returned an invalid claim response")
