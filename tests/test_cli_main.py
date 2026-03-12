from __future__ import annotations

from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orb.cli.main import async_main


def _base_args(**overrides) -> Namespace:
    data = dict(
        subcommand=None,
        query=None,
        interactive=False,
        trace=True,
        no_trace=False,
        budget=200,
        timeout=30.0,
        max_depth=10,
        model=None,
        local_only=False,
        cloud_only=False,
        ollama_model=None,
        dashboard=False,
        dashboard_port=8080,
        dev=False,
        topology="auto",
        tui=False,
        logs=False,
        exit_after_run=False,
        verbose=False,
        quiet=True,
    )
    data.update(overrides)
    return Namespace(**data)


@pytest.mark.asyncio
async def test_async_main_passes_query_into_tui_mode():
    args = _base_args(tui=True, query="write hello world", exit_after_run=True)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.tui.run_tui_async", new_callable=AsyncMock) as run_tui:
        await async_main()

    run_tui.assert_called_once()
    _, kwargs = run_tui.call_args
    assert kwargs["initial_query"] == "write hello world"
    assert kwargs["exit_after_run"] is True


@pytest.mark.asyncio
async def test_async_main_passes_budget_into_tui_dashboard_mode():
    args = _base_args(tui=True, dashboard=True, budget=321, query="write hello world")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.tui.run_tui_with_dashboard", new_callable=AsyncMock) as run_tui_with_dashboard:
        await async_main()

    run_tui_with_dashboard.assert_awaited_once()
    _, kwargs = run_tui_with_dashboard.call_args
    assert kwargs["budget"] == 321


@pytest.mark.asyncio
async def test_async_main_dashboard_uses_auto_topology_by_default():
    args = _base_args(query="write hello world", dashboard=True, exit_after_run=True)
    instances = []

    class FakeDashboardServer:
        def __init__(self, *_args, **_kwargs):
            self.broadcast = MagicMock()
            self.runtime = MagicMock()
            self.runtime.start_run = AsyncMock(return_value=(200, {"ok": True}))
            self.runtime.wait_for_run = AsyncMock()
            self.runtime.last_result = Namespace(
                error=None,
                completions={"coder": "done"},
                message_count=1,
                timed_out=False,
            )
            instances.append(self)

        def set_providers(self, *_args, **_kwargs):
            return None

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.main.print_header"), \
         patch("orb.cli.main.print_result"), \
         patch("web.server.DashboardServer", FakeDashboardServer):
        await async_main()

    instances[0].runtime.start_run.assert_awaited_once_with(
        "write hello world",
        "auto",
    )


@pytest.mark.asyncio
async def test_async_main_skips_dashboard_prompt_with_exit_after_run():
    args = _base_args(query="write hello world", dashboard=True, exit_after_run=True)

    class FakeBus:
        def __init__(self):
            self.graph = Namespace(edges=[])

        def on_event(self, *_args, **_kwargs):
            return None

    class FakeOrchestrator:
        def __init__(self):
            self.agents = {}
            self.bus = FakeBus()
            self._on_agent_complete = MagicMock()

        async def run(self, _query):
            return Namespace(error=None, completions={"coordinator": "done"}, message_count=1, timed_out=False)

    class FakeDashboardServer:
        def __init__(self, *_args, **_kwargs):
            self.broadcast = MagicMock()
            self.runtime = MagicMock()
            self.runtime.start_run = AsyncMock(return_value=(200, {"ok": True}))
            self.runtime.wait_for_run = AsyncMock()
            self.runtime.last_result = Namespace(
                error=None,
                completions={"coordinator": "done"},
                message_count=1,
                timed_out=False,
            )

        def set_agents(self, _agents):
            return None

        def set_providers(self, *_args, **_kwargs):
            return None

        async def start(self):
            return None

        async def stop(self):
            return None

    class FakeBridge:
        def __init__(self, *_args, **_kwargs):
            return None

        def setup_agents(self, *_args, **_kwargs):
            return None

        def setup_edges(self, *_args, **_kwargs):
            return None

        def setup_budget(self, *_args, **_kwargs):
            return None

        async def on_agent_complete(self, *_args, **_kwargs):
            return None

        def on_message_routed(self, *_args, **_kwargs):
            return None

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.main.print_header"), \
         patch("orb.cli.main.print_result"), \
         patch("orb.cli.main.create_triad", return_value=FakeOrchestrator()), \
         patch("web.server.DashboardServer", FakeDashboardServer), \
         patch("web.bridge.DashboardBridge", FakeBridge), \
         patch("rich.prompt.Prompt.ask") as prompt_ask:
        await async_main()

    prompt_ask.assert_not_called()
