import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from qingzhou_cli import main as qingzhou_main


class FakeGraph:
    def stream(self, input_payload, config=None, stream_mode=None):
        self.input_payload = input_payload
        self.config = config
        self.stream_mode = stream_mode
        yield {
            "messages": [
                *input_payload["messages"],
                SimpleNamespace(type="ai", content="hello from cli"),
            ]
        }


class QingzhouCliTest(unittest.TestCase):
    def _run_cli(self, argv):
        graph = FakeGraph()
        output = io.StringIO()
        with patch.object(qingzhou_main, "_load_graph", return_value=graph):
            with redirect_stdout(output):
                status = qingzhou_main.main(argv)
        return status, output.getvalue(), graph

    def test_default_command_runs_chat_once(self):
        status, output, graph = self._run_cli(["hello"])

        self.assertEqual(status, 0)
        self.assertIn("hello from cli", output)
        self.assertEqual(graph.input_payload["messages"][-1]["content"], "hello")
        self.assertEqual(graph.stream_mode, "values")

    def test_chat_subcommand_runs_same_chat_path(self):
        status, output, graph = self._run_cli(["chat", "hello"])

        self.assertEqual(status, 0)
        self.assertIn("hello from cli", output)
        self.assertEqual(graph.input_payload["messages"][-1]["content"], "hello")
        self.assertIn("cli", graph.config["tags"])


if __name__ == "__main__":
    unittest.main()
