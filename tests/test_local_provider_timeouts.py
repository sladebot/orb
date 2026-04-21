"""Local providers must use bounded, split timeouts.

A flat 600s timeout means a request to a model that the server *lists* but
hasn't actually loaded (e.g. OMLX's /v1/models returning a name that isn't
resident) hangs 10 minutes per retry × 3 retries = 30 minutes silent stall.
Split connect/read timeouts surface the failure in ~3 minutes instead.
"""
from __future__ import annotations

import httpx
import pytest

from orb.llm.ollama import OllamaProvider
from orb.llm.omlx import OmlxProvider
from orb.llm.vmlx import VmlxProvider


@pytest.mark.parametrize(
    "provider",
    [
        OllamaProvider(base_url="http://localhost:11434"),
        OmlxProvider(base_url="http://localhost:8000/v1"),
        VmlxProvider(base_url="http://localhost:1234/v1"),
    ],
)
def test_local_provider_uses_split_timeout(provider):
    """Connect should be fast (detect unreachable server in seconds).

    Read can be longer (model load + generation), but must still be bounded.
    """
    timeout = provider._client.timeout  # httpx.Timeout instance
    # httpx.Timeout exposes connect/read/write/pool attributes.
    assert timeout.connect is not None and timeout.connect <= 30.0, (
        f"connect timeout is {timeout.connect!r} — unreachable servers will "
        f"take too long to fail"
    )
    assert timeout.read is not None and timeout.read <= 300.0, (
        f"read timeout is {timeout.read!r} — 5+ minute hangs cause user "
        f"confusion (the original bug: model listed but not loaded)"
    )
