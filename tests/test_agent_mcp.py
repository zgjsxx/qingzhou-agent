import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.mcp import load_mcp_tools
from agent.permissions import check_tool_permission


class _McpHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.calls.append(payload)
        method = payload.get("method")
        rpc_id = payload.get("id")

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock", "version": "1.0.0"},
            }
            self._send({"jsonrpc": "2.0", "id": rpc_id, "result": result})
            return
        if method == "notifications/initialized":
            self._send({})
            return
        if method == "tools/list":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "tools": [
                            {
                                "name": "search-docs",
                                "description": "Search docs",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            }
                        ]
                    },
                }
            )
            return
        if method == "tools/call":
            args = payload.get("params", {}).get("arguments", {})
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"found: {args.get('query')}",
                            }
                        ]
                    },
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": rpc_id, "error": {"message": "unknown"}})

    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class McpToolTest(unittest.TestCase):
    def setUp(self):
        _McpHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_load_http_mcp_tool_and_call_it(self):
        url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / ".mcp.json"
            config_path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "docs.server": {
                                "type": "http",
                                "url": url,
                                "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AGENT_MCP_CONFIG": str(config_path), "MCP_TEST_TOKEN": "secret"},
                clear=False,
            ):
                tools = load_mcp_tools()

        self.assertEqual([tool.name for tool in tools], ["mcp__docs_server__search-docs"])
        self.assertEqual(tools[0].invoke({"query": "agent"}), "found: agent")

        methods = [call.get("method") for call in _McpHandler.calls]
        self.assertIn("initialize", methods)
        self.assertIn("tools/list", methods)
        self.assertIn("tools/call", methods)
        self.assertEqual(_McpHandler.calls[-1]["params"]["name"], "search-docs")

    def test_load_http_mcp_tool_from_unified_config(self):
        url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "xu-agent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcp": {
                            "servers": {
                                "docs.server": {
                                    "type": "http",
                                    "url": url,
                                    "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AGENT_MCP_CONFIG": "", "MCP_TEST_TOKEN": "secret"},
                clear=False,
            ), patch("agent.config.CONFIG_FILE", config_path), patch("agent.mcp.CONFIG_FILE", config_path):
                tools = load_mcp_tools()

        self.assertEqual([tool.name for tool in tools], ["mcp__docs_server__search-docs"])

    def test_mcp_tools_require_approval(self):
        decision = check_tool_permission("mcp__github__create_issue", {"title": "x"})
        self.assertEqual(decision.behavior, "ask")


if __name__ == "__main__":
    unittest.main()
