import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_context import _snip_compact_state


def _updated_messages(update):
    return update["messages"][1:]


class SnipCompactStateTest(unittest.TestCase):
    def test_snip_compact_skips_when_message_count_is_under_trigger(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_SNIP_COMPACT_ENABLED": "true",
                "AGENT_SNIP_TRIGGER_MESSAGES": "50",
            },
        ):
            messages = [HumanMessage(content=f"message {index}") for index in range(50)]

            update = _snip_compact_state({"messages": messages})

        self.assertEqual(update, {})

    def test_snip_compact_removes_middle_messages(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_SNIP_COMPACT_ENABLED": "true",
                "AGENT_SNIP_TRIGGER_MESSAGES": "50",
                "AGENT_SNIP_KEEP_HEAD_MESSAGES": "3",
                "AGENT_SNIP_KEEP_TAIL_MESSAGES": "47",
            },
        ):
            messages = [HumanMessage(content=f"message {index}") for index in range(51)]

            update = _snip_compact_state({"messages": messages})
            compacted = _updated_messages(update)

        self.assertEqual(len(compacted), 50)
        self.assertEqual([message.content for message in compacted[:3]], ["message 0", "message 1", "message 2"])
        self.assertNotIn("message 3", [message.content for message in compacted])
        self.assertEqual(compacted[3].content, "message 4")
        self.assertEqual(update["snip_compact_metadata"]["removed_message_count"], 1)

    def test_snip_compact_expands_tail_to_preserve_tool_pair(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_SNIP_COMPACT_ENABLED": "true",
                "AGENT_SNIP_TRIGGER_MESSAGES": "50",
                "AGENT_SNIP_KEEP_HEAD_MESSAGES": "3",
                "AGENT_SNIP_KEEP_TAIL_MESSAGES": "47",
            },
        ):
            messages = [HumanMessage(content=f"head {index}") for index in range(3)]
            messages.extend(HumanMessage(content=f"middle {index}") for index in range(2))
            messages.append(
                AIMessage(
                    content="calling tool",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "demo.txt"},
                            "id": "toolu_read",
                            "type": "tool_call",
                        }
                    ],
                )
            )
            messages.append(ToolMessage(content="tool result", tool_call_id="toolu_read"))
            messages.extend(HumanMessage(content=f"tail {index}") for index in range(46))

            update = _snip_compact_state({"messages": messages})
            compacted = _updated_messages(update)

        self.assertEqual(len(messages), 53)
        self.assertEqual(len(compacted), 51)
        self.assertIsInstance(compacted[3], AIMessage)
        self.assertIsInstance(compacted[4], ToolMessage)
        self.assertTrue(update["snip_compact_metadata"]["tail_expanded_for_tool_pair"])
        self.assertEqual(update["snip_compact_metadata"]["removed_message_count"], 2)


if __name__ == "__main__":
    unittest.main()
