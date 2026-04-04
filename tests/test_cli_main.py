from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orb.cli.main import DEFAULT_DAEMON_PORT, async_main
from orb.tracing import RunTrace


def _base_args(**overrides) -> Namespace:
    data = dict(
        subcommand=None,
        trace_action=None,
        run_id=None,
        query=None,
        interactive=False,
        trace=True,
        no_trace=False,
        json=False,
        path=False,
        session=None,
        current_session=False,
        interval=0.5,
        budget=200,
        timeout=30.0,
        max_depth=10,
        model=None,
        local_only=False,
        cloud_only=False,
        ollama_model=None,
        connect=None,
        dev=False,
        verbose=False,
        quiet=True,
        topology="auto",
        logs=False,
        exit_after_run=False,
        no_open=False,
        daemon_action=None,
        host=None,
        port=None,
        workdir=None,
    )
    data.update(overrides)
    return Namespace(**data)


def test_cmd_trace_latest_and_list_are_session_aware(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    session_dir = tmp_path / ".orb"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "current_session").write_text("session-a")

    trace = RunTrace(session_id="session-a")
    trace.record_topology_choice("triad", reason="test")
    trace.record_final_outcome(success=True, result="ok")
    trace.save(tmp_path / ".orb" / "traces" / f"{trace.run_id}.json")
    index_dir = tmp_path / ".orb" / "traces" / "by-session"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_dir.joinpath("session-a.json").write_text(
        __import__("json").dumps({"session_id": "session-a", "runs": [trace.summary()]})
    )

    from orb.cli.main import _cmd_trace

    _cmd_trace(_base_args(subcommand="trace", trace_action="latest", current_session=True))
    latest_out = capsys.readouterr().out
    assert "session-a" in latest_out
    assert trace.run_id in latest_out

    _cmd_trace(_base_args(subcommand="trace", trace_action="list", current_session=True))
    list_out = capsys.readouterr().out
    assert "Session session-a" in list_out
    assert trace.run_id in list_out


def test_cmd_trace_tail_streams_current_session_events(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    session_dir = tmp_path / ".orb"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "current_session").write_text("session-a")

    trace = RunTrace(session_id="session-a")
    trace.record_topology_choice("triad", reason="test")
    trace.record_stage_start("planning", actor="router", message="planning started")
    trace.save(tmp_path / ".orb" / "traces" / f"{trace.run_id}.json")
    index_dir = tmp_path / ".orb" / "traces" / "by-session"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_dir.joinpath("session-a.json").write_text(
        __import__("json").dumps({"session_id": "session-a", "runs": [trace.summary()]})
    )

    from orb.cli.main import _cmd_trace

    def _stop(_seconds):
        raise KeyboardInterrupt

    with patch("orb.cli.main.time.sleep", side_effect=_stop):
        _cmd_trace(_base_args(subcommand="trace", trace_action="tail", current_session=True, interval=0.01))

    output = capsys.readouterr().out
    assert "Following traces for session session-a" in output
    assert trace.run_id in output
    assert "topology_choice" in output


def test_current_session_falls_back_to_managed_daemon_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    daemon_workdir = tmp_path / "daemon-workdir"
    daemon_state_dir = tmp_path / "home" / ".orb"
    daemon_state_dir.mkdir(parents=True, exist_ok=True)
    daemon_workdir.joinpath(".orb").mkdir(parents=True, exist_ok=True)
    daemon_workdir.joinpath(".orb", "current_session").write_text("daemon-session")

    with patch("orb.cli.main.DAEMON_STATE_FILE", str(daemon_state_dir / "daemon.json")):
        (daemon_state_dir / "daemon.json").write_text(
            __import__("json").dumps({"workdir": str(daemon_workdir)})
        )
        from orb.cli.main import _current_session_id
        assert _current_session_id() == "daemon-session"


@pytest.mark.asyncio
async def test_async_main_passes_query_into_tui_mode():
    args = _base_args(subcommand="tui", query="write hello world", exit_after_run=True)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["initial_query"] == "write hello world"
    assert kwargs["exit_after_run"] is True


@pytest.mark.asyncio
async def test_async_main_passes_budget_into_tui_dashboard_mode():
    args = _base_args(subcommand="tui", budget=321, query="write hello world")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["budget"] == 321


@pytest.mark.asyncio
async def test_async_main_connects_tui_to_existing_daemon():
    args = _base_args(subcommand="tui", connect="http://127.0.0.1:9090", query="hello")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["connect_url"] == "http://127.0.0.1:9090"
    assert kwargs["initial_query"] == "hello"


@pytest.mark.asyncio
async def test_async_main_tui_accepts_port_shorthand():
    args = _base_args(subcommand="tui", port=9091, query="hello")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["connect_url"] == "http://127.0.0.1:9091"
    assert kwargs["initial_query"] == "hello"


@pytest.mark.asyncio
async def test_async_main_tui_prefers_explicit_connect_over_port():
    args = _base_args(subcommand="tui", connect="http://127.0.0.1:9090", port=9091, query="hello")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock) as attach_tui:
        await async_main()

    attach_tui.assert_awaited_once()
    _, kwargs = attach_tui.call_args
    assert kwargs["connect_url"] == "http://127.0.0.1:9090"


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
    assert kwargs["connect_url"] == f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"
    assert kwargs["initial_query"] == "hello"


@pytest.mark.asyncio
async def test_async_main_tui_uses_file_only_logging():
    args = _base_args(subcommand="tui", verbose=True, quiet=False)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("logging.basicConfig") as basic_config, \
         patch("orb.cli.tui.attach_tui", new_callable=AsyncMock):
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
    args = _base_args(subcommand="dashboard", connect="http://127.0.0.1:9090", query="write hello world")

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
         patch("orb.cli.main.build_providers", side_effect=AssertionError("should not build providers")), \
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
async def test_async_main_runs_daemon_server_with_local_only():
    args = _base_args(subcommand="daemon", host="127.0.0.1", port=9090, local_only=True)

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
         patch("orb.cli.main.build_providers", return_value={"mock": object()}) as build_providers, \
         patch("logging.basicConfig"), \
         patch("orb.cli.main.tempfile.mkdtemp", return_value="/tmp/orb-daemon-test"), \
         patch("orb.cli.main.os.chdir"), \
         patch("orb.cli.main._save_daemon_state"), \
         patch("orb.cli.main._clear_daemon_state"), \
         patch("web.server.DashboardServer", FakeDashboardServer), \
         patch("asyncio.Event", return_value=FakeEvent()), \
         patch("asyncio.get_running_loop", return_value=fake_loop):
        await async_main()

    build_providers.assert_called_once_with(local_only=True, cloud_only=False)


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
    args = _base_args(subcommand="daemon", daemon_action="start", host="0.0.0.0", port=DEFAULT_DAEMON_PORT)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main._start_managed_daemon", return_value={
             "pid": 1234,
             "host": "0.0.0.0",
             "port": DEFAULT_DAEMON_PORT,
             "workdir": "/tmp/orb-daemon-x",
         }) as start_daemon, \
         patch("orb.cli.main.build_providers", side_effect=AssertionError("should not build providers")):
        await async_main()

    start_daemon.assert_called_once_with(
        "0.0.0.0",
        DEFAULT_DAEMON_PORT,
        None,
        local_only=False,
        cloud_only=False,
    )


@pytest.mark.asyncio
async def test_start_managed_daemon_fails_fast_when_port_in_use():
    from orb.cli.main import _start_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._port_bind_error", return_value=OSError(98, "Address already in use")):
        with pytest.raises(RuntimeError, match=f"Port {DEFAULT_DAEMON_PORT} is already in use"):
            _start_managed_daemon("0.0.0.0", DEFAULT_DAEMON_PORT, None)


@pytest.mark.asyncio
async def test_start_managed_daemon_reports_permission_error_for_privileged_port():
    from orb.cli.main import _start_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._port_bind_error", return_value=PermissionError(13, "Permission denied")):
        with pytest.raises(RuntimeError, match="Port 80 requires elevated privileges or additional permissions"):
            _start_managed_daemon("0.0.0.0", 80, None)


@pytest.mark.asyncio
async def test_async_main_daemon_stop_uses_managed_stop():
    args = _base_args(subcommand="daemon", daemon_action="stop")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._stop_managed_daemon", new_callable=AsyncMock) as stop_daemon:
        await async_main()

    stop_daemon.assert_awaited_once_with(DEFAULT_DAEMON_PORT)


@pytest.mark.asyncio
async def test_async_main_daemon_restart_restarts_background_process():
    args = _base_args(subcommand="daemon", daemon_action="restart")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._stop_managed_daemon", new_callable=AsyncMock) as stop_daemon, \
         patch("orb.cli.main._start_managed_daemon", return_value={
             "pid": 5678,
             "host": "127.0.0.1",
             "port": DEFAULT_DAEMON_PORT,
             "workdir": "/tmp/orb-daemon-y",
         }) as start_daemon:
        await async_main()

    stop_daemon.assert_awaited_once()
    start_daemon.assert_called_once_with(
        "127.0.0.1",
        DEFAULT_DAEMON_PORT,
        None,
        local_only=False,
        cloud_only=False,
    )


@pytest.mark.asyncio
async def test_async_main_daemon_restart_preserves_local_only():
    args = _base_args(subcommand="daemon", daemon_action="restart", local_only=True, host="0.0.0.0")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._stop_managed_daemon", new_callable=AsyncMock) as stop_daemon, \
         patch("orb.cli.main._start_managed_daemon", return_value={
             "pid": 5678,
             "host": "0.0.0.0",
             "port": DEFAULT_DAEMON_PORT,
             "workdir": "/tmp/orb-daemon-y",
         }) as start_daemon:
        await async_main()

    stop_daemon.assert_awaited_once()
    start_daemon.assert_called_once_with(
        "0.0.0.0",
        DEFAULT_DAEMON_PORT,
        None,
        local_only=True,
        cloud_only=False,
    )


@pytest.mark.asyncio
async def test_parse_args_accepts_daemon_local_only():
    from orb.cli.main import parse_args

    with patch("sys.argv", ["orb", "daemon", "restart", "--local-only", "--host", "0.0.0.0"]):
        args = parse_args()

    assert args.subcommand == "daemon"
    assert args.daemon_action == "restart"
    assert args.local_only is True
    assert args.host == "0.0.0.0"


@pytest.mark.asyncio
async def test_parse_args_preserves_root_local_only_for_daemon():
    from orb.cli.main import parse_args

    with patch("sys.argv", ["orb", "--local-only", "daemon", "run", "--host", "0.0.0.0"]):
        args = parse_args()

    assert args.subcommand == "daemon"
    assert args.daemon_action == "run"
    assert args.local_only is True
    assert args.host == "0.0.0.0"


@pytest.mark.asyncio
async def test_async_main_daemon_status_reports_running_process():
    args = _base_args(subcommand="daemon", daemon_action="status")

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value={
             "pid": 4321,
             "host": "0.0.0.0",
             "port": DEFAULT_DAEMON_PORT,
             "workdir": "/tmp/orb-daemon-z",
         }), \
         patch("orb.cli.main._pid_is_alive", return_value=True), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=True), \
         patch("orb.cli.main._clear_daemon_state") as clear_state:
        await async_main()

    clear_state.assert_not_called()


@pytest.mark.asyncio
async def test_async_main_daemon_status_reports_unmanaged_listener():
    args = _base_args(subcommand="daemon", daemon_action="status", port=DEFAULT_DAEMON_PORT)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=True), \
         patch("orb.cli.main._clear_daemon_state") as clear_state:
        await async_main()

    clear_state.assert_called_once()


@pytest.mark.asyncio
async def test_async_main_daemon_status_refuses_non_orb_listener(capsys):
    args = _base_args(subcommand="daemon", daemon_action="status", port=DEFAULT_DAEMON_PORT)

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=False), \
         patch("orb.cli.main._clear_daemon_state"):
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
        stopped = await _stop_managed_daemon(DEFAULT_DAEMON_PORT)

    assert stopped is True
    kill.assert_called_once_with(9999, __import__("signal").SIGTERM)


@pytest.mark.asyncio
async def test_stop_managed_daemon_rejects_non_orb_listener():
    from orb.cli.main import _stop_managed_daemon

    with patch("orb.cli.main._load_daemon_state", return_value=None), \
         patch("orb.cli.main._find_listening_pid", return_value=9999), \
         patch("orb.cli.main._port_looks_like_orb_daemon", return_value=False):
        with pytest.raises(RuntimeError, match="does not look like an Orb daemon"):
            await _stop_managed_daemon(DEFAULT_DAEMON_PORT)


def test_daemon_state_file_uses_stable_home_path():
    import orb.cli.main as main_mod

    assert str(main_mod._daemon_state_path()).endswith(".orb/daemon.json")


@pytest.mark.asyncio
async def test_async_main_direct_run_uses_default_triad_topology():
    args = _base_args(query="write hello world")

    fake_result = Namespace(
        error=None,
        completions={"coder": "done"},
        message_count=1,
        timed_out=False,
    )

    class FakeBus:
        def __init__(self):
            self.graph = Namespace(edges=[])

        def on_event(self, *_args, **_kwargs):
            return None

    class FakeOrchestrator:
        def __init__(self):
            self.agents = {}
            self.bus = FakeBus()

        async def run(self, _query):
            return fake_result

    fake_orchestrator = FakeOrchestrator()

    with patch("orb.cli.main.parse_args", return_value=args), \
         patch("orb.cli.main._setup_log_file"), \
         patch("orb.cli.main.build_providers", return_value={"mock": object()}), \
         patch("orb.cli.main.print_header"), \
         patch("orb.cli.main.print_result"), \
         patch("orb.cli.main.create_orchestrator", return_value=fake_orchestrator) as create_orchestrator:
        await async_main()

    assert create_orchestrator.call_args.args[0] == "triad"
