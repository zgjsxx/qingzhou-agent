import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.logging import AgentLoggingMiddleware, ROOT_DIR, _log_dir


class AgentLoggingMiddlewareTest(unittest.TestCase):
    def test_relative_log_dir_resolves_from_project_root(self):
        with patch.dict("os.environ", {"AGENT_LOG_DIR": "./logs"}):
            self.assertEqual(_log_dir(), ROOT_DIR / "logs")

    def test_tool_log_includes_agent_name(self):
        middleware = AgentLoggingMiddleware(agent_name="subagent")

        with patch("agent.logging.log_event") as log_event:
            middleware._log_tool_end(1.0, "read_file", "ok")

        _, kwargs = log_event.call_args
        self.assertEqual(kwargs["agent_name"], "subagent")
        self.assertEqual(kwargs["tool"], "read_file")


if __name__ == "__main__":
    unittest.main()
