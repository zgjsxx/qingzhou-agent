import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_commands import clear_context_update, clear_thread_context, is_clear_command


class AgentCommandsTest(unittest.TestCase):
    def test_clear_command_requires_exact_match(self):
        self.assertTrue(is_clear_command(" /CLEAR "))
        self.assertFalse(is_clear_command("/clear now"))

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


if __name__ == "__main__":
    unittest.main()
