import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.moa import MOA_DISABLED_MESSAGE, normalize_moa_request, run_moa_disabled
from tools import registry


class MoaScaffoldTest(unittest.TestCase):
    def test_normalize_moa_request(self):
        request = normalize_moa_request(
            question="Compare options",
            context="Use project context",
            agents=[{"name": "reviewer", "role": "Find risks", "model": "glm-5.1"}],
            aggregator_model="glm-5.1",
        )

        self.assertEqual(request.question, "Compare options")
        self.assertEqual(request.context, "Use project context")
        self.assertEqual(request.agents[0].name, "reviewer")
        self.assertEqual(request.agents[0].role, "Find risks")
        self.assertEqual(request.aggregator_model, "glm-5.1")

    def test_run_moa_disabled_does_not_expose_runtime_work(self):
        result = run_moa_disabled(question="Should we build MOA?")
        payload = json.loads(result)

        self.assertEqual(payload["status"], "disabled")
        self.assertEqual(payload["message"], MOA_DISABLED_MESSAGE)
        self.assertEqual(payload["request"]["question"], "Should we build MOA?")

    def test_moa_is_not_registered_as_tool(self):
        tool_names = {tool.name for tool in registry.ALL_TOOLS}

        self.assertNotIn("moa", tool_names)
        self.assertNotIn("moa_task", tool_names)
        self.assertNotIn("run_moa", tool_names)


if __name__ == "__main__":
    unittest.main()
