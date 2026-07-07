import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.commands import (
    COMPACT_COMMAND,
    HELP_RESPONSE,
    clear_context_update,
    clear_thread_context,
    handle_thread_slash_command,
    is_clear_command,
    is_help_command,
    parse_slash_command_text,
)


class AgentCommandsTest(unittest.TestCase):
    def test_clear_command_requires_exact_match(self):
        self.assertTrue(is_clear_command(" /CLEAR "))
        self.assertFalse(is_clear_command("/clear now"))

    def test_help_command_requires_exact_match(self):
        self.assertTrue(is_help_command(" /HELP "))
        self.assertFalse(is_help_command("/help compact"))
        self.assertIn("/compact", HELP_RESPONSE)

    def test_parse_slash_command_text_parses_compact_focus(self):
        command = parse_slash_command_text("/compact 保留 SSH 结论")

        self.assertIsNotNone(command)
        self.assertEqual(command.name, COMPACT_COMMAND)
        self.assertEqual(command.args, "保留 SSH 结论")

    def test_clear_context_update_resets_conversation_state(self):
        update = clear_context_update()

        self.assertEqual(update["messages"][0].id, "__remove_all__")
        self.assertEqual(update["context_usage"], {})
        self.assertEqual(update["compact_failure_count"], 0)

    def test_clear_thread_context_updates_same_thread(self):
        calls = []
        graph = SimpleNamespace(update_state=lambda config, values: calls.append((config, values)))

        updated = clear_thread_context(graph, "thread-1", source="test")

        self.assertTrue(updated)
        self.assertEqual(calls[0][0], {"configurable": {"thread_id": "thread-1"}})

    def test_clear_thread_context_allows_graph_without_checkpointer(self):
        def update_state(_config, _values):
            raise ValueError("No checkpointer set")

        updated = clear_thread_context(
            SimpleNamespace(update_state=update_state),
            "thread-1",
            source="test",
        )

        self.assertFalse(updated)

    def test_handle_thread_slash_command_handles_help_without_graph_run(self):
        graph = SimpleNamespace(update_state=lambda *_args, **_kwargs: self.fail("/help should not update state"))

        result = handle_thread_slash_command("/help", graph, "thread-1", source="test")

        self.assertIsNotNone(result)
        self.assertEqual(result.response, HELP_RESPONSE)
        self.assertFalse(result.clear_history)

    def test_handle_thread_slash_command_handles_clear_and_marks_history_clear(self):
        calls = []
        graph = SimpleNamespace(update_state=lambda config, values: calls.append((config, values)))

        result = handle_thread_slash_command("/clear", graph, "thread-1", source="test")

        self.assertIsNotNone(result)
        self.assertTrue(result.clear_history)
        self.assertEqual(calls[0][0], {"configurable": {"thread_id": "thread-1"}})


if __name__ == "__main__":
    unittest.main()
