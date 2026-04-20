"""Pre-warm tests: session creation with explicit topology paints the graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiohttp.test_utils import TestClient, TestServer

from orb.runtime import RuntimeManager
from web.server import DashboardServer
from web.state import DashboardState


@pytest.fixture
async def client(tmp_path: Path):
    state = DashboardState()
    server = DashboardServer(state, host="127.0.0.1", port=18300)
    server.runtime._session_path = tmp_path / "default.json"  # noqa: SLF001
    server.runtime._session_path_explicit = True  # noqa: SLF001
    ts = TestServer(server._app)  # noqa: SLF001
    await ts.start_server()
    async with TestClient(ts) as c:
        yield c, server
    await ts.close()


class TestManagerPrewarm:
    def test_create_session_with_workdir_surfaces_in_init_event(self, tmp_path: Path):
        """The first /state fetch after a prewarmed session creation must
        include the workdir at the top-level + plan — the dashboard reads
        ``data.workdir`` / ``data.plan.workdir`` and falls back to ``—``
        when both are empty. Regression for "picked folder not showing up
        in the UI".
        """
        workdir = tmp_path / "proj"
        workdir.mkdir()
        mgr = RuntimeManager()
        session = mgr.create_session(
            workdir=str(workdir),
            session_path=tmp_path / "a.json",
            topology="triad",
        )
        init = session.current_init_event(
            session_id=session._conversation_session.session_id  # noqa: SLF001
        )
        assert init.get("workdir") == str(workdir)
        assert (init.get("plan") or {}).get("workdir") == str(workdir)
        assert (init.get("session") or {}).get("workdir") == str(workdir)

    def test_create_session_with_triad_pins_topology(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(
            session_path=tmp_path / "a.json",
            topology="triad",
            agent_models={"coder": "claude-haiku-4-5-20251001"},
        )
        cs = session._conversation_session  # noqa: SLF001
        assert cs.locked_topology == "triad"
        assert cs.locked_agent_models == {"coder": "claude-haiku-4-5-20251001"}

    def test_prewarm_populates_agents_from_topology_schema(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(
            session_path=tmp_path / "a.json",
            topology="triad",
        )
        # Triad ships coordinator, coder, reviewer, tester — all painted
        # as idle before any run fires.
        agent_ids = set(session.state.agents.keys())
        assert {"coordinator", "coder", "reviewer", "tester"} <= agent_ids
        for agent in session.state.agents.values():
            assert agent.status == "idle"
        # Edges come straight from the topology spec
        assert len(session.state.edges) >= 3

    def test_auto_topology_leaves_state_unwarmed(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json", topology="auto")
        assert session._conversation_session.locked_topology == ""  # noqa: SLF001
        assert session.state.agents == {}

    def test_no_topology_kwarg_leaves_state_unwarmed(self, tmp_path: Path):
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json")
        assert session.state.agents == {}

    def test_invalid_topology_is_silently_ignored_at_manager_layer(self, tmp_path: Path):
        """The v1 route validates topology before calling manager; the
        manager itself stays defensive — unknown ids don't crash the call.
        """
        mgr = RuntimeManager()
        session = mgr.create_session(session_path=tmp_path / "a.json", topology="nonexistent")
        assert session.state.agents == {}
        assert session._conversation_session.locked_topology == ""  # noqa: SLF001


@pytest.mark.asyncio
class TestPrewarmAPI:
    async def test_create_with_topology_paints_agents_in_state(self, client):
        c, server = client
        resp = await c.post("/api/v1/sessions", json={"topology": "triad"})
        assert resp.status == 201
        env = await resp.json()
        sid = env["data"]["session_id"]

        state_resp = await c.get(f"/api/v1/sessions/{sid}/state")
        state = (await state_resp.json())["data"]
        agent_ids = {a["id"] for a in state.get("agents", [])}
        assert {"coordinator", "coder", "reviewer", "tester"} <= agent_ids
        # Plan block shows the pinned topology right away
        assert state["plan"]["topology"]["id"] == "triad"

    async def test_create_with_agent_models_requires_topology(self, client):
        c, _ = client
        resp = await c.post(
            "/api/v1/sessions",
            json={"agent_models": {"coder": "claude-opus-4-7"}},
        )
        assert resp.status == 400
        env = await resp.json()
        assert env["code"] == "INVALID_AGENT_MODELS"

    async def test_create_with_invalid_topology_returns_400(self, client):
        c, _ = client
        resp = await c.post("/api/v1/sessions", json={"topology": "nonexistent"})
        assert resp.status == 400
        env = await resp.json()
        assert env["code"] == "INVALID_TOPOLOGY"

    async def test_create_with_auto_does_not_prewarm(self, client):
        c, _ = client
        resp = await c.post("/api/v1/sessions", json={"topology": "auto"})
        assert resp.status == 201
        sid = (await resp.json())["data"]["session_id"]
        state = (await (await c.get(f"/api/v1/sessions/{sid}/state")).json())["data"]
        # No agents painted — the classifier will populate them on first run
        assert state.get("agents") == [] or state.get("agents") is None
