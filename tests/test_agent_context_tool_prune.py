import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.context import prune_old_tool_results


class ToolResultPruneTest(unittest.TestCase):
    def test_does_not_remove_or_rewrite_conversation_messages(self):
        messages = [
            HumanMessage(content="question", id="human-1"),
            AIMessage(content="answer", id="ai-1"),
        ]

        pruned, metadata = prune_old_tool_results(messages)

        self.assertIsNone(metadata)
        self.assertEqual(pruned, messages)

    def test_preserves_recent_tool_result(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES": "2",
                "AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS": "100",
            },
        ):
            messages = [
                HumanMessage(content="request", id="human-1"),
                AIMessage(
                    content="",
                    id="ai-1",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "demo.txt"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="x" * 500, tool_call_id="call-1", id="tool-1"),
            ]

            pruned, metadata = prune_old_tool_results(messages)

        self.assertIsNone(metadata)
        self.assertEqual(pruned, messages)

    def test_prunes_old_tool_result_without_removing_messages(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES": "1",
                "AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS": "1",
            },
        ):
            messages = [
                AIMessage(
                    content="",
                    id="ai-1",
                    tool_calls=[
                        {
                            "name": "run_shell_command",
                            "args": {"command": "pytest"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content="exit_code: 1\n" + ("failure\n" * 79) + "failure",
                    tool_call_id="call-1",
                    id="tool-1",
                ),
                HumanMessage(content="what failed?", id="human-1"),
            ]

            pruned, metadata = prune_old_tool_results(messages)

        self.assertEqual(len(pruned), len(messages))
        self.assertEqual(pruned[2].content, "what failed?")
        self.assertEqual(
            pruned[1].content,
            "[run_shell_command] ran `pytest` -> exit 1, 81 lines output",
        )
        self.assertEqual(metadata["pruned_tool_results"], 1)

    def test_deduplicates_older_tool_result(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES": "10",
                "AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS": "10000",
            },
        ):
            content = "same file\n" * 30
            messages = [
                ToolMessage(content=content, tool_call_id="call-1", id="tool-1"),
                ToolMessage(content=content, tool_call_id="call-2", id="tool-2"),
            ]

            pruned, metadata = prune_old_tool_results(messages)

        self.assertTrue(pruned[0].content.startswith("[Duplicate tool output"))
        self.assertEqual(pruned[1].content, content)
        self.assertEqual(metadata["deduplicated_tool_results"], 1)

    def test_truncates_large_tool_call_arguments(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_TOOL_RESULT_PROTECT_LAST_MESSAGES": "1",
                "AGENT_TOOL_RESULT_PROTECT_TAIL_TOKENS": "1",
            },
        ):
            messages = [
                AIMessage(
                    content="",
                    id="ai-1",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "demo.txt", "content": "x" * 1000},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                HumanMessage(content="continue", id="human-1"),
            ]

            pruned, metadata = prune_old_tool_results(messages)

        args = pruned[0].tool_calls[0]["args"]
        self.assertEqual(args["path"], "demo.txt")
        self.assertTrue(args["content"].endswith("...[truncated]"))
        self.assertEqual(metadata["truncated_tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
