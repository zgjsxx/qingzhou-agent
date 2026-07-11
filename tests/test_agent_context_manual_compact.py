import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.commands import AgentCommandMiddleware, CLEAR_RESPONSE, HELP_RESPONSE
from agent.context import _merge_or_insert_summary, _summary_message, manual_compact_state
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


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

        with patch("agent.context._summarize_messages", return_value="Summary:\n保留 SSH 调试结论") as summarize:
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
        self.assertEqual(update["compact_metadata"]["summarized_messages"], 22)
        self.assertEqual(update["compact_metadata"]["kept_messages"], 3)
        self.assertEqual(compacted_messages[0].id, "__remove_all__")
        self.assertEqual([message.id for message in compacted_messages[1:4]], ["m-0", "m-1", "m-2"])
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

    def test_manual_compact_updates_previous_summary_with_new_messages_only(self):
        messages = [
            SystemMessage(content="[Context compacted by manual]"),
            _summary_message("Summary:\nold deployment facts"),
            HumanMessage(content="new user request", id="new-user"),
            HumanMessage(content="new follow-up", id="new-follow-up"),
        ]

        with patch("agent.context._summarize_messages", return_value="Summary:\nmerged facts") as summarize:
            update = manual_compact_state(
                {"messages": messages, "context_usage": {"input_tokens": 456}},
                messages=messages,
            )

        summarize.assert_called_once()
        summarized_messages = summarize.call_args.args[0]
        self.assertEqual([message.content for message in summarized_messages], ["new user request", "new follow-up"])
        self.assertEqual(summarize.call_args.kwargs["previous_summary"], "Summary:\nold deployment facts")
        self.assertEqual(update["compact_metadata"]["summarized_messages"], 2)
        self.assertIn("Summary:\nmerged facts", update["messages"][1].content)

    def test_manual_compact_skips_when_only_existing_summary_remains(self):
        messages = [
            SystemMessage(content="[Context compacted by manual]"),
            _summary_message("Summary:\nold deployment facts"),
        ]

        with patch("agent.context._summarize_messages") as summarize:
            update = manual_compact_state({"messages": messages}, messages=messages)

        summarize.assert_not_called()
        self.assertEqual(update, {})

    def test_compact_summary_uses_assistant_role_before_user_tail(self):
        messages = [
            HumanMessage(content="old user", id="old-user"),
            AIMessage(content="old assistant", id="old-assistant"),
            HumanMessage(content="kept user", id="kept-user"),
        ]

        with patch.dict(
            "os.environ",
            {"AGENT_MANUAL_COMPACT_KEEP_MESSAGES": "1", "AGENT_COMPACT_PROTECT_FIRST_MESSAGES": "0"},
        ):
            with patch("agent.context._summarize_messages", return_value="Summary:\nrole-aware facts"):
                update = manual_compact_state({"messages": messages}, messages=messages)

        self.assertIsInstance(update["messages"][1], AIMessage)
        self.assertIsInstance(update["messages"][2], HumanMessage)
        self.assertIn("Summary:\nrole-aware facts", update["messages"][1].content)

    def test_compact_summary_uses_user_role_before_assistant_tail(self):
        messages = [
            HumanMessage(content="old user", id="old-user"),
            AIMessage(content="old assistant", id="old-assistant"),
            AIMessage(content="kept assistant", id="kept-assistant"),
        ]

        with patch.dict(
            "os.environ",
            {"AGENT_MANUAL_COMPACT_KEEP_MESSAGES": "1", "AGENT_COMPACT_PROTECT_FIRST_MESSAGES": "0"},
        ):
            with patch("agent.context._summarize_messages", return_value="Summary:\nrole-aware facts"):
                update = manual_compact_state({"messages": messages}, messages=messages)

        self.assertIsInstance(update["messages"][1], HumanMessage)
        self.assertIsInstance(update["messages"][2], AIMessage)
        self.assertIn("Summary:\nrole-aware facts", update["messages"][1].content)

    def test_summary_merges_into_tail_when_both_dialog_roles_conflict(self):
        compacted = _merge_or_insert_summary(
            "Summary:\nmerged facts",
            [AIMessage(content="head assistant")],
            [HumanMessage(content="tail user", id="tail-user")],
        )

        self.assertEqual(len(compacted), 2)
        self.assertIsInstance(compacted[1], HumanMessage)
        self.assertEqual(compacted[1].id, "tail-user")
        self.assertTrue(compacted[1].content.startswith("This session is being continued"))
        self.assertIn("Summary:\nmerged facts", compacted[1].content)
        self.assertIn("tail user", compacted[1].content)


if __name__ == "__main__":
    unittest.main()
