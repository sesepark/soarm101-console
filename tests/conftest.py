from __future__ import annotations

import pytest

from soarm_console import hubq_client


@pytest.fixture(autouse=True)
def allow_hubq_claims_in_hardware_free_tests(monkeypatch):
    """Unit tests do not require a separately running HUBq daemon.

    Claim behavior and console propagation have focused tests that replace this permissive stub.
    """
    monkeypatch.setattr(hubq_client, "claim", lambda _kind, _devices: None)
