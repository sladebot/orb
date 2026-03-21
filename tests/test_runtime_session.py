from __future__ import annotations

from pathlib import Path

import pytest

from orb.messaging.message import Message, MessageType
from orb.runtime.graph_runtime import GraphRuntime
from orb.runtime.transcript import ConversationSession


class _DummyTask:
    def done(self) -> bool:
        return False


class _DummyChannel:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, msg: Message) -> None:
        self.messages.append(msg)


class _DummyAgent:
    def __init__(self) -> None:
        self.channel = _DummyChannel()


class TestConversationSession:
    def test_round_trip_preserves_compactions_and_carryover(self, tmp_path: Path):
        session = ConversationSession()
        session.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="build it"))
        session.add_completion("coder", "done")
        session.agent_carryover = {"coder": [{"role": "assistant", "content": "cached"}]}
        session.apply_compaction("Earlier context summary", preserve_recent_turns=1)

        path = tmp_path / "session.json"
        session.save(path)
        loaded = ConversationSession.load(path)

        assert loaded.compactions[-1].summary == "Earlier context summary"
        assert loaded.agent_carryover["coder"][0]["content"] == "cached"
        assert loaded.user_turn_count() == 0


class TestGraphRuntimeSession:
    def test_runtime_default_session_paths_are_per_session(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        runtime = GraphRuntime()
        first_path = runtime._resolved_session_path()  # noqa: SLF001
        first_id = runtime._conversation_session.session_id  # noqa: SLF001
        runtime._persist_session()  # noqa: SLF001

        assert first_path == tmp_path / ".orb" / "sessions" / f"{first_id}.json"
        assert first_path.exists()
        assert (tmp_path / ".orb" / "current_session").read_text().strip() == first_id

    @pytest.mark.asyncio
    async def test_new_session_uses_new_session_file_by_default(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        runtime = GraphRuntime()
        first_path = runtime._resolved_session_path()  # noqa: SLF001
        runtime._persist_session()  # noqa: SLF001

        status, payload = await runtime.new_session()

        second_path = runtime._resolved_session_path()  # noqa: SLF001
        assert status == 200
        assert payload["ok"] is True
        assert second_path != first_path
        assert second_path.exists()
        assert (tmp_path / ".orb" / "current_session").read_text().strip() == runtime._conversation_session.session_id  # noqa: SLF001

    def test_runtime_init_event_uses_persisted_user_turn_count(self, tmp_path: Path):
        path = tmp_path / "session.json"
        session = ConversationSession()
        session.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="task 1"))
        session.add_message(Message(from_="coordinator", to="coder", type=MessageType.TASK, payload="route"))
        session.add_message(Message(from_="user", to="reviewer", type=MessageType.RESPONSE, payload="task 2"))
        session.save(path)

        runtime = GraphRuntime(session_path=path)
        event = runtime.current_init_event()

        assert event["session_turn"] == 2
        assert event["workdir"] == str(Path.cwd())

    def test_runtime_resolves_mentions_server_side(self, tmp_path: Path):
        runtime = GraphRuntime(session_path=tmp_path / "session.json")

        target, text = runtime._resolve_conversation_target(  # noqa: SLF001
            "@coder fix the failing test",
            default_target="coordinator",
            known_targets={"coder", "reviewer"},
        )

        assert target == "coder"
        assert text == "fix the failing test"

    @pytest.mark.asyncio
    async def test_inject_message_routes_to_mentioned_agent_and_persists_session(self, tmp_path: Path):
        session_path = tmp_path / "session.json"
        runtime = GraphRuntime(session_path=session_path)
        runtime._run_task = _DummyTask()  # noqa: SLF001
        runtime._agents = {  # noqa: SLF001
            "coordinator": _DummyAgent(),
            "coder": _DummyAgent(),
        }

        status, payload = await runtime.inject_message("coordinator", "@coder fix src/app.py")

        assert status == 200
        assert payload["ok"] is True
        coder_channel = runtime._agents["coder"].channel  # noqa: SLF001
        assert coder_channel.messages[-1].payload == "fix src/app.py"

        loaded = ConversationSession.load(session_path)
        assert loaded.user_turn_count() == 1
        assert loaded.turns[-1].audience == "coder"
