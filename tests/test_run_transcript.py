from __future__ import annotations

from orb.messaging.message import Message, MessageType
from orb.runtime.transcript import RunTranscript


class TestRunTranscript:
    def test_add_message_records_turn(self):
        transcript = RunTranscript()
        msg = Message(from_="user", to="coder", type=MessageType.TASK, payload="Build an app")
        transcript.add_message(msg)

        assert len(transcript.turns) == 1
        turn = transcript.turns[0]
        assert turn.speaker == "user"
        assert turn.audience == "coder"
        assert turn.kind == "task"

    def test_render_for_model_includes_recent_turns(self):
        transcript = RunTranscript()
        transcript.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="Build an app"))
        transcript.add_message(Message(from_="coordinator", to="coder", type=MessageType.TASK, payload="Build an app"))
        transcript.add_message(Message(from_="coder", to="reviewer", type=MessageType.RESPONSE, payload="Please review src/app.py"))

        rendered = transcript.render_for_model("reviewer")

        assert "Shared session transcript" in rendered
        assert "user -> coordinator" in rendered
        assert "coder -> reviewer" in rendered

    def test_render_for_model_includes_full_shared_conversation(self):
        transcript = RunTranscript()
        transcript.add_message(Message(from_="user", to="coordinator", type=MessageType.TASK, payload="Main task"))
        transcript.add_message(Message(from_="reviewer", to="coder", type=MessageType.FEEDBACK, payload="Need tests"))
        transcript.add_message(Message(from_="tester", to="reviewer", type=MessageType.FEEDBACK, payload="Unrelated branch"))

        rendered = transcript.render_for_model("coder")

        assert "Main task" in rendered
        assert "Need tests" in rendered
        assert "Unrelated branch" in rendered

    def test_render_for_model_shows_all_turns_by_default(self):
        transcript = RunTranscript(max_turns=100)
        for i in range(60):
            transcript.add_message(Message(from_="user", to="coder", type=MessageType.TASK, payload=f"task {i}"))

        rendered = transcript.render_for_model("coder")

        assert "task 0" in rendered
        assert "task 59" in rendered
