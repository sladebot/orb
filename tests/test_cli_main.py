from __future__ import annotations

from argparse import Namespace
from pathlib import Path
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
        connect=None,
        dev=False,
        topology="auto",
        tui=False,
        logs=False,
        exit_after_run=False,
        verbose=False,
        quiet=True,
        no_open=False,
        daemon_action=None,
        host=None,
        port=None,
        workdir=None,
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
async def test_async_main_connects_tui_to_existing_daemon():
    args = _base_args(tui=True, connect="http://127.0.0.1:9090", query="hello")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["connect_url"] == "http://127.0.0.1:9090"
    assert kwargs["initial_query"] == "hello"


@pytest.mark.asyncio
async def test_async_main_tui_subcommand_defaults_to_local_daemon():
    args = _base_args(subcommand="tui", query="hello")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui, \
         patch("orb.cli.main.build_providers", side_effect=AssertionError("should not build providers")):
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["connect_url"] == "http://127.0.0.1:8080"
    assert kwargs["initial_query"] == "hello"


@pytest.mark.asyncio
async def test_async_main_tui_uses_file_only_logging():
    args = _base_args(tui=True, verbose=True, quiet=False)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("logging.basicConfig") as basic_config, \
         patch("orb.cli.tui.run_tui_async", new_callable=AsyncMock):
        await async_main()

    assert basic_config.call_args.kwargs["handlers"] == []
    assert basic_config.call_args.kwargs["force"] is True


@pytest.mark.asyncio
async def test_async_main_topologies_init_writes_sample():
    args = _base_args(subcommand="topologies", topologies_action="init", force=False)
    target = Path("/tmp/topologies.yaml")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._init_topologies_file", return_value=target) as init_topologies:
        await async_main()

    init_topologies.assert_called_once_with(force=False)


@pytest.mark.asyncio
async def test_async_main_topologies_init_existing_file_errors():
    args = _base_args(subcommand="topologies", topologies_action="init", force=False)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._init_topologies_file", side_effect=FileExistsError("exists")), \
         pytest.raises(SystemExit) as exc:
        await async_main()

    assert exc.value.code == 1


def test_init_topologies_file_copies_sample_and_respects_force(tmp_path):
    from orb.cli.main import _init_topologies_file

    target = tmp_path / "topologies.yaml"
    sample_text = "topologies:\n  demo: {}\n"

    class FakeResource:
        def joinpath(self, name: str):
            assert name == "sample-topology.yaml"
            return self

        def read_text(self):
            return sample_text

    with patch("orb.topologies.loader.USER_TOPOLOGIES_PATH", target), \
         patch("orb.cli.main.resources.files", return_value=FakeResource()):
        written = _init_topologies_file()
        assert written == target
        assert target.read_text() == sample_text

        with pytest.raises(FileExistsError):
            _init_topologies_file()

        sample_text = "topologies:\n  demo2: {}\n"
        written = _init_topologies_file(force=True)
        assert written == target
        assert target.read_text() == sample_text


@pytest.mark.asyncio
async def test_async_main_dashboard_connect_starts_remote_run():
    args = _base_args(dashboard=True, connect="http://127.0.0.1:9090", query="write hello world")

    fake_response = MagicMock()
    fake_response.__aenter__ = AsyncMock(return_value=fake_response)
    fake_response.__aexit__ = AsyncMock(return_value=None)
    fake_response.json = AsyncMock(return_value={"ok": True})

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post.return_value = fake_response

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("aiohttp.ClientSession", return_value=fake_session):
        await async_main()

    fake_session.post.assert_called_once()
    assert fake_session.post.call_args.args[0] == "http://127.0.0.1:9090/api/start"


@pytest.mark.asyncio
async def test_async_main_dashboard_subcommand_opens_browser():
    args = _base_args(subcommand="dashboard", connect="127.0.0.1:9090", query="write hello world")

    fake_response = MagicMock()
    fake_response.__aenter__ = AsyncMock(return_value=fake_response)
    fake_response.__aexit__ = AsyncMock(return_value=None)
    fake_response.json = AsyncMock(return_value={"ok": True})

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post.return_value = fake_response

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("aiohttp.ClientSession", return_value=fake_session), \
         patch("webbrowser.open") as open_browser, \
         patch("orb.cli.main.build_providers", side_effect=AssertionError("should not build providers")):
        await async_main()

    fake_session.post.assert_called_once()
    assert fake_session.post.call_args.args[0] == "http://127.0.0.1:9090/api/start"
    open_browser.assert_called_once_with("http://127.0.0.1:9090")


@pytest.mark.asyncio
async def test_async_main_runs_daemon_server():
    args = _base_args(subcommand="daemon", host="127.0.0.1", port=9090)
    stop_event = AsyncMock()

    class FakeEvent:
        async def wait(self):
            return None

        def set(self):
            return None

    class FakeDashboardServer:
        def __init__(self, *_args, **_kwargs):
            self.set_providers = MagicMock()
            self.start = AsyncMock()
            self.stop = AsyncMock()

    fake_loop = MagicMock()

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main.print_header"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("logging.basicConfig") as basic_config, \
         patch("orb.cli.main.tempfile.mkdtemp", return_value="/tmp/orb-daemon-test"), \
         patch("orb.cli.main.os.chdir") as chdir, \
         patch("orb.cli.main._save_daemon_state"), \
         patch("orb.cli.main._clear_daemon_state"), \
         patch("web.server.DashboardServer", FakeDashboardServer), \
         patch("asyncio.Event", return_value=FakeEvent()), \
         patch("asyncio.get_running_loop", return_value=fake_loop):
        await async_main()

    basic_config.assert_not_called()
    chdir.assert_called_once_with(Path("/tmp/orb-daemon-test").resolve())


@pytest.mark.asyncio
async def test_async_main_daemon_honors_explicit_workdir():
    args = _base_args(subcommand="daemon", host="127.0.0.1", port=9090, workdir="/tmp/orb-fixed")

    class FakeEvent:
        async def wait(self):
            return None

        def set(self):
            return None

    class FakeDashboardServer:
        def __init__(self, *_args, **_kwargs):
            self.set_providers = MagicMock()
            self.start = AsyncMock()
            self.stop = AsyncMock()

    fake_loop = MagicMock()

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main.print_header"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.main.Path.mkdir") as mkdir, \
         patch("orb.cli.main.tempfile.mkdtemp", side_effect=AssertionError("should not create temp dir")), \
         patch("orb.cli.main.os.chdir") as chdir, \
         patch("orb.cli.main._save_daemon_state"), \
         patch("web.server.DashboardServer", FakeDashboardServer), \
         patch("asyncio.Event", return_value=FakeEvent()), \
         patch("asyncio.get_running_loop", return_value=fake_loop):
        await async_main()

    mkdir.assert_called()
    chdir.assert_called_once_with(Path("/tmp/orb-fixed").resolve())


@pytest.mark.asyncio
async def test_async_main_daemon_start_starts_background_process():
    args = _base_args(subcommand="daemon", daemon_action="start", host="0.0.0.0", port=8080)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main._start_managed_daemon", return_value={
             "pid": 1234,
             "host": "0.0.0.0",
             "port": 8080,
             "workdir": "/tmp/orb-daemon-x",
         }) as start_daemon, \
         patch("orb.cli.main.build_providers", side_effect=AssertionError("should not build providers")):
        await async_main()

    start_daemon.assert_called_once_with("0.0.0.0", 8080, None)


@pytest.mark.asyncio
async def test_start_managed_daemon_fails_fast_when_port_in_use():
    from orb.cli.main import _start_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._port_in_use", return_value=True):
        with pytest.raises(RuntimeError, match="Port 8080 is already in use"):
            _start_managed_daemon("0.0.0.0", 8080, None)


@pytest.mark.asyncio
async def test_async_main_daemon_stop_uses_managed_stop():
    args = _base_args(subcommand="daemon", daemon_action="stop")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._stop_managed_daemon", new_callable=AsyncMock) as stop_daemon:
        await async_main()

    stop_daemon.assert_awaited_once_with(8080)


@pytest.mark.asyncio
async def test_async_main_daemon_restart_restarts_background_process():
    args = _base_args(subcommand="daemon", daemon_action="restart")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._stop_managed_daemon", new_callable=AsyncMock) as stop_daemon, \
         patch("orb.cli.main._start_managed_daemon", return_value={
             "pid": 5678,
             "host": "127.0.0.1",
             "port": 8080,
             "workdir": "/tmp/orb-daemon-y",
         }) as start_daemon:
        await async_main()

    stop_daemon.assert_awaited_once()
    start_daemon.assert_called_once_with("127.0.0.1", 8080, None)


@pytest.mark.asyncio
async def test_async_main_daemon_status_reports_running_process():
    args = _base_args(subcommand="daemon", daemon_action="status")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value={
             "pid": 4321,
             "host": "0.0.0.0",
             "port": 8080,
             "workdir": "/tmp/orb-daemon-z",
         }), \
         patch("orb.cli.main._pid_is_alive", return_value=True), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=True), \
         patch("orb.cli.main._clear_daemon_state") as clear_state:
        await async_main()

    clear_state.assert_not_called()


@pytest.mark.asyncio
async def test_async_main_daemon_status_reports_unmanaged_listener():
    args = _base_args(subcommand="daemon", daemon_action="status", port=8080)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=True), \
         patch("orb.cli.main._clear_daemon_state") as clear_state:
        await async_main()

    clear_state.assert_called_once()


@pytest.mark.asyncio
async def test_async_main_daemon_status_refuses_non_orb_listener(capsys):
    args = _base_args(subcommand="daemon", daemon_action="status", port=8080)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=False):
        await async_main()

    assert "not an Orb daemon" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_stop_managed_daemon_falls_back_to_port_listener():
    from orb.cli.main import _stop_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=True), \
         patch("orb.cli.main.os.kill") as kill, \
         patch("orb.cli.main._pid_is_alive", side_effect=[False]):
        stopped = await _stop_managed_daemon(8080)

    assert stopped is True
    kill.assert_called_once_with(9999, __import__("signal").SIGTERM)


@pytest.mark.asyncio
async def test_stop_managed_daemon_rejects_non_orb_listener():
    from orb.cli.main import _stop_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=False):
        with pytest.raises(RuntimeError, match="does not look like an Orb daemon"):
            await _stop_managed_daemon(8080)


def test_daemon_state_file_uses_stable_home_path():
    import orb.cli.main as main_mod

    assert str(main_mod._daemon_state_path()).endswith(".orb/daemon.json")


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
         patch("orb.cli.main.create_orchestrator", return_value=FakeOrchestrator()), \
         patch("web.server.DashboardServer", FakeDashboardServer), \
         patch("web.bridge.DashboardBridge", FakeBridge), \
         patch("rich.prompt.Prompt.ask") as prompt_ask:
        await async_main()

    prompt_ask.assert_not_called()
