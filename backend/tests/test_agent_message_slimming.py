import sys
from pathlib import Path

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_message_slimming import SLIMMED_MARKER, slim_message


def test_slim_message_redacts_large_write_file_content(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_ARG_MAX_CHARS", "10")
    message = AIMessage(
        content=[
            {
                "type": "tool_use",
                "name": "write_file",
                "partial_json": '{"path": "demo.py", "content": "abcdefghijklmnopqrstuvwxyz"}',
            }
        ],
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "demo.py", "content": "abcdefghijklmnopqrstuvwxyz"},
                "id": "toolu_test",
                "type": "tool_call",
            }
        ],
    )

    slimmed, changed = slim_message(message)

    assert changed is True
    assert slimmed.tool_calls[0]["args"]["path"] == "demo.py"
    assert slimmed.tool_calls[0]["args"]["content"].startswith(SLIMMED_MARKER)
    assert "abcdefghijklmnopqrstuvwxyz" not in slimmed.content[0]["partial_json"]


def test_slim_message_keeps_small_write_file_content(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_ARG_MAX_CHARS", "100")
    message = AIMessage(
        content="writing a tiny file",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "demo.py", "content": "tiny"},
                "id": "toolu_test",
                "type": "tool_call",
            }
        ],
    )

    slimmed, changed = slim_message(message)

    assert changed is False
    assert slimmed is message
    assert slimmed.tool_calls[0]["args"]["content"] == "tiny"
