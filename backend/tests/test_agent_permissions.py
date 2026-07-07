import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.permissions import AgentPermissionMiddleware, check_tool_permission


class AgentPermissionMiddlewareTest(unittest.TestCase):
    def test_noninteractive_ask_rule_denies_without_interrupt(self):
        middleware = AgentPermissionMiddleware(interactive=False)
        request = SimpleNamespace(
            tool_call={
                "name": "run_shell_command",
                "args": {"command": "python -m pip install demo"},
            },
        )

        with patch("agent.permissions.interrupt") as interrupt:
            result = middleware._check_request(request)

        self.assertIsInstance(result, str)
        self.assertIn("requires user approval", result)
        interrupt.assert_not_called()

    def test_rag_rebuild_requires_approval(self):
        decision = check_tool_permission("rag_rebuild_index", {"data_dir": ""})

        self.assertEqual(decision.behavior, "ask")
        self.assertIn("RAG index", decision.reason)


if __name__ == "__main__":
    unittest.main()
