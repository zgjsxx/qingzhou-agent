import asyncio
import io
import json
import unittest
from pathlib import Path

from agent.acp import AcpServer, JsonRpcWriter, _content_blocks_to_text


class FakeBridge:
    def __init__(self):
        self.created_cwds = []
        self.prompts = []
        self.thread_count = 0

    async def create_thread(self, cwd):
        self.created_cwds.append(cwd)
        self.thread_count += 1
        return f"thread-{self.thread_count}"

    async def stream_prompt(self, thread_id, prompt, cwd):
        self.prompts.append((thread_id, prompt, cwd))
        yield "hello"
        yield " world"


class AcpTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_initialize_returns_minimal_capabilities(self):
        output = io.StringIO()
        server = AcpServer(FakeBridge())

        self._run(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                },
                JsonRpcWriter(output),
            )
        )

        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], 1)
        self.assertFalse(response["result"]["agentCapabilities"]["loadSession"])

    def test_new_session_creates_langgraph_thread(self):
        bridge = FakeBridge()
        output = io.StringIO()
        server = AcpServer(bridge)

        self._run(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": "D:/ai/qingzhou-agent", "mcpServers": []},
                },
                JsonRpcWriter(output),
            )
        )

        response = json.loads(output.getvalue())
        self.assertEqual(response["result"]["sessionId"], "acp_thread-1")
        self.assertIn("acp_thread-1", server.sessions)
        self.assertEqual(Path(bridge.created_cwds[0]), Path("D:/ai/qingzhou-agent"))

    def test_new_session_uses_configured_workspace_when_client_omits_cwd(self):
        bridge = FakeBridge()
        output = io.StringIO()
        server = AcpServer(bridge, workspace_dir="D:/ems/code-new/ems-server")

        self._run(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "session/new",
                    "params": {},
                },
                JsonRpcWriter(output),
            )
        )

        self.assertEqual(Path(bridge.created_cwds[0]), Path("D:/ems/code-new/ems-server"))

    def test_prompt_streams_agent_message_chunks(self):
        bridge = FakeBridge()
        output = io.StringIO()
        server = AcpServer(bridge)
        server.sessions["acp_thread-1"] = type(
            "Session",
            (),
            {
                "session_id": "acp_thread-1",
                "thread_id": "thread-1",
                "cwd": "D:/ai/qingzhou-agent",
            },
        )()

        self._run(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": "acp_thread-1",
                        "prompt": [{"type": "text", "text": "hi"}],
                    },
                },
                JsonRpcWriter(output),
            )
        )

        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[0]["method"], "session/update")
        self.assertEqual(messages[0]["params"]["update"]["content"]["text"], "hello")
        self.assertEqual(messages[1]["params"]["update"]["content"]["text"], " world")
        self.assertEqual(messages[2]["result"]["stopReason"], "end_turn")
        self.assertIn("VSCode workspace cwd: D:/ai/qingzhou-agent", bridge.prompts[0][1])
        self.assertTrue(bridge.prompts[0][1].endswith("hi"))

    def test_content_blocks_to_text_includes_resource_links(self):
        text = _content_blocks_to_text(
            [
                {"type": "text", "text": "look"},
                {"type": "resource_link", "name": "file", "uri": "file:///tmp/a.py"},
            ]
        )

        self.assertIn("look", text)
        self.assertIn("file:///tmp/a.py", text)

    def test_clear_starts_a_new_langgraph_thread(self):
        bridge = FakeBridge()
        output = io.StringIO()
        server = AcpServer(bridge)
        server.sessions["acp_thread-1"] = type(
            "Session",
            (),
            {
                "session_id": "acp_thread-1",
                "thread_id": "thread-old",
                "cwd": "D:/ai/qingzhou-agent",
            },
        )()

        self._run(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": "acp_thread-1",
                        "prompt": [{"type": "text", "text": "/clear"}],
                    },
                },
                JsonRpcWriter(output),
            )
        )

        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(messages[0]["params"]["update"]["content"]["text"], "Context cleared.")
        self.assertEqual(server.sessions["acp_thread-1"].thread_id, "thread-1")
        self.assertEqual(bridge.created_cwds, ["D:/ai/qingzhou-agent"])

    def test_writer_outputs_utf8_non_ascii_without_surrogate_escapes(self):
        output = io.StringIO()

        JsonRpcWriter(output).notification(
            "session/update",
            {"content": {"type": "text", "text": "hello \U0001f60a"}},
        )

        raw = output.getvalue()
        raw.encode("utf-8")
        self.assertIn("\U0001f60a", raw)
        self.assertNotIn("\\ud83d", raw)


if __name__ == "__main__":
    unittest.main()
