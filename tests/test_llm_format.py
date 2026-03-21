from orb.llm._format import to_openai_messages


def test_to_openai_messages_coerces_structured_user_content_to_text() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
            ],
        },
    ]

    converted = to_openai_messages(messages)

    assert converted == [
        {
            "role": "user",
            "content": 'hello {"source": {"media_type": "image/png", "type": "base64"}, "type": "image"}',
        },
    ]


def test_to_openai_messages_coerces_dict_content_to_json_string() -> None:
    messages = [
        {
            "role": "user",
            "content": {"kind": "structured", "value": 3},
        },
    ]

    converted = to_openai_messages(messages)

    assert converted == [
        {
            "role": "user",
            "content": '{"kind": "structured", "value": 3}',
        },
    ]


def test_to_openai_messages_keeps_tool_call_arguments_as_object() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling tool"},
                {"type": "tool_use", "id": "call_1", "name": "demo", "input": {"x": 1}},
            ],
        },
    ]

    converted = to_openai_messages(messages, tool_arguments_as_object=True)

    assert converted == [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "demo",
                        "arguments": {"x": 1},
                    },
                },
            ],
        },
    ]


def test_to_openai_messages_stringifies_tool_call_arguments_by_default() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "demo", "input": {"x": 1}},
            ],
        },
    ]

    converted = to_openai_messages(messages)

    assert converted == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "demo",
                        "arguments": '{"x": 1}',
                    },
                },
            ],
        },
    ]
