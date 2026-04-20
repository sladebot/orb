"""Provider-registry config loaders must log failures, not swallow them silently."""
from __future__ import annotations

import logging
import pytest

from orb.llm import registry as reg


def _force_config_error(monkeypatch, exc: Exception) -> None:
    """Make ``orb.cli.config.get`` raise ``exc`` for registry callers."""
    def _raising_get(_key):
        raise exc

    monkeypatch.setattr("orb.cli.config.get", _raising_get)


@pytest.mark.parametrize(
    "fn",
    [
        reg._ollama_keep_alive,
        reg._vmlx_base_url,
        reg._vmlx_api_key,
        reg._omlx_base_url,
        reg._omlx_api_key,
    ],
)
def test_config_loader_logs_on_unexpected_error(monkeypatch, caplog, fn):
    """Unexpected config-loading errors must be logged (not silently swallowed)."""
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.delenv("VMLX_BASE_URL", raising=False)
    monkeypatch.delenv("VMLX_API_KEY", raising=False)
    monkeypatch.delenv("OMLX_BASE_URL", raising=False)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)

    _force_config_error(monkeypatch, RuntimeError("boom"))

    caplog.set_level(logging.DEBUG, logger=reg.__name__)
    # Must not raise.
    fn()

    # Must leave a trace in the logger (any level) so debugging is possible.
    records_from_registry = [r for r in caplog.records if r.name == reg.__name__]
    assert records_from_registry, (
        f"{fn.__name__} swallowed RuntimeError with no log output"
    )


def test_registry_enabled_checks_log_on_unexpected_error(monkeypatch, caplog):
    _force_config_error(monkeypatch, RuntimeError("boom"))
    caplog.set_level(logging.DEBUG, logger=reg.__name__)

    assert reg._vmlx_enabled() is False
    assert reg._omlx_enabled() is False
    assert reg._ollama_enabled() is False

    records = [r for r in caplog.records if r.name == reg.__name__]
    assert records, "_*_enabled() swallowed RuntimeError with no log output"
