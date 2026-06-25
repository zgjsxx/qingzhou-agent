import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_permissions import AgentPermissionMiddleware


class AgentPermissionMiddlewareTest(unittest.TestCase):
    def test_noninteractive_ask_rule_denies_without_interrupt(self):
        middleware = AgentPermissionMiddleware(interactive=False)
        request = SimpleNamespace(
            tool_call={
                "name": "run_shell_command",
                "args": {"command": "python -m pip install demo"},
            },
        )

        with patch("agent_permissions.interrupt") as interrupt:
            result = middleware._check_request(request)

        self.assertIsInstance(result, str)
        self.assertIn("requires user approval", result)
        interrupt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
