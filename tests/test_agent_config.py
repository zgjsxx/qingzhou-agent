import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import config as agent_config


class AgentConfigTest(unittest.TestCase):
    def test_config_snapshot_updates_only_after_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "qingzhou-agent.json"
            config_path.write_text(
                json.dumps({"llm": {"model": "first"}}),
                encoding="utf-8",
            )
            with patch("agent.config.CONFIG_FILE", config_path):
                self.assertTrue(agent_config.reload_agent_config(force=True))
                self.assertEqual(agent_config.config_str("llm", "model"), "first")

                config_path.write_text(
                    json.dumps({"llm": {"model": "second-model"}}),
                    encoding="utf-8",
                )
                self.assertEqual(agent_config.config_str("llm", "model"), "first")

                self.assertTrue(agent_config.reload_agent_config())
                self.assertEqual(agent_config.config_str("llm", "model"), "second-model")

    def test_invalid_config_keeps_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "qingzhou-agent.json"
            config_path.write_text(
                json.dumps({"llm": {"model": "stable"}}),
                encoding="utf-8",
            )
            with patch("agent.config.CONFIG_FILE", config_path):
                self.assertTrue(agent_config.reload_agent_config(force=True))
                config_path.write_text("{", encoding="utf-8")

                self.assertFalse(agent_config.reload_agent_config())
                self.assertEqual(agent_config.config_str("llm", "model"), "stable")


if __name__ == "__main__":
    unittest.main()
