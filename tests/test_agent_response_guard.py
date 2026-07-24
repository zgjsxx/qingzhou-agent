from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from agent.response_guard import VISIBLE_TEXT_FALLBACK, guard_empty_visible_ai_response


def test_guard_replaces_thinking_only_response():
    response = ModelResponse(
        result=[
            AIMessage(
                content=[{"type": "thinking", "thinking": "internal draft"}],
                response_metadata={"stop_reason": "end_turn"},
            )
        ]
    )

    guarded = guard_empty_visible_ai_response(response)

    assert guarded is response
    assert guarded.result[-1].content == VISIBLE_TEXT_FALLBACK
    assert guarded.result[-1].response_metadata["stop_reason"] == "end_turn"


def test_guard_keeps_visible_text_response():
    message = AIMessage(content=[{"type": "text", "text": "final answer"}])
    response = ModelResponse(result=[message])

    guarded = guard_empty_visible_ai_response(response)

    assert guarded.result[-1] is message


def test_guard_keeps_tool_call_response():
    message = AIMessage(
        content=[{"type": "thinking", "thinking": "need tool"}],
        tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "toolu_1"}],
    )
    response = ModelResponse(result=[message])

    guarded = guard_empty_visible_ai_response(response)

    assert guarded.result[-1] is message
