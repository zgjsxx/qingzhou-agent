import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_commands import AgentCommandMiddleware, CLEAR_RESPONSE, HELP_RESPONSE
from langchain_core.messages import HumanMessage


class AgentContextManualCompactTest(unittest.TestCase):
    def test_help_command_returns_deterministic_response_and_skips_main_model(self):
        messages = [HumanMessage(content="/help", id="help-command")]
        request = SimpleNamespace(state={"messages": messages}, messages=messages)

        response = AgentCommandMiddleware().wrap_model_call(
            request,
            lambda _request: self.fail("/help should not be sent to the main model"),
        )

        update = response.command.update

        self.assertEqual(update["messages"][0].id, "help-command")
        self.assertEqual(response.model_response.result[0].content, HELP_RESPONSE)

    def test_clear_command_returns_clear_update_and_skips_main_model(self):
        messages = [
            HumanMessage(content="old message", id="old-message"),
            HumanMessage(content="/clear", id="clear-command"),
        ]
        request = SimpleNamespace(state={"messages": messages}, messages=messages)

        response = AgentCommandMiddleware().wrap_model_call(
            request,
            lambda _request: self.fail("/clear should not be sent to the main model"),
        )

        update = response.command.update

        self.assertEqual(update["messages"][0].id, "__remove_all__")
        self.assertEqual(update["context_usage"], {})
        self.assertEqual(response.model_response.result[0].content, CLEAR_RESPONSE)

    def test_manual_compact_reuses_state_update_and_skips_main_model(self):
        messages = [HumanMessage(content=f"message {index}", id=f"m-{index}") for index in range(25)]
        messages.append(HumanMessage(content="/compact 保留 SSH 调试结论", id="compact-command"))
        request = SimpleNamespace(
            state={"messages": messages, "context_usage": {"input_tokens": 1234}},
            messages=messages,
        )

        with patch("agent_context._summarize_messages", return_value="Summary:\n保留 SSH 调试结论") as summarize:
            response = AgentCommandMiddleware().wrap_model_call(
                request,
                lambda _request: self.fail("/compact should not be sent to the main model"),
            )

        update = response.command.update
        compacted_messages = update["messages"]

        summarize.assert_called_once()
        self.assertEqual(update["compact_metadata"]["trigger"], "manual")
        self.assertEqual(update["compact_metadata"]["focus"], "保留 SSH 调试结论")
        self.assertEqual(update["compact_metadata"]["before_tokens"], 1234)
        self.assertEqual(update["compact_metadata"]["summarized_messages"], 25)
        self.assertEqual(update["compact_metadata"]["kept_messages"], 0)
        self.assertEqual(compacted_messages[0].id, "__remove_all__")
        self.assertNotIn("/compact 保留 SSH 调试结论", [getattr(message, "content", "") for message in compacted_messages])
        self.assertIn("已压缩上下文", response.model_response.result[0].content)

    def test_manual_compact_removes_command_when_no_history_can_be_compacted(self):
        messages = [HumanMessage(content="/compact", id="compact-command")]
        request = SimpleNamespace(state={"messages": messages}, messages=messages)

        response = AgentCommandMiddleware().wrap_model_call(
            request,
            lambda _request: self.fail("/compact should not be sent to the main model"),
        )

        update = response.command.update

        self.assertEqual(update["messages"][0].id, "compact-command")
        self.assertIn("暂无可压缩", response.model_response.result[0].content)


if __name__ == "__main__":
    unittest.main()
