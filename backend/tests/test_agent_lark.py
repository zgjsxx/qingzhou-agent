import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_lark


class AgentLarkTest(unittest.TestCase):
    def test_parse_text_message_event(self):
        data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(open_id="ou_123"),
                ),
                message=SimpleNamespace(
                    message_id="om_123",
                    chat_id="oc_456",
                    message_type="text",
                    content=json.dumps({"text": "你好"}),
                ),
            )
        )

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.message_id, "om_123")
        self.assertEqual(event.chat_id, "oc_456")
        self.assertEqual(event.sender_id, "ou_123")
        self.assertEqual(event.text, "你好")

    def test_parse_post_message_event(self):
        content = {
            "content": [
                [
                    {"tag": "text", "text": "hello "},
                    {"tag": "text", "text": "world"},
                ]
            ]
        }
        data = {
            "event": {
                "message": {
                    "message_id": "om_post",
                    "chat_id": "oc_post",
                    "message_type": "post",
                    "content": json.dumps(content),
                }
            }
        }

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "hello world")

    def test_thread_id_for_chat_is_stable_and_safe(self):
        self.assertEqual(agent_lark._thread_id_for_chat("oc abc/123"), "lark_oc_abc_123")

    def test_extract_final_ai_text(self):
        result = {
            "messages": [
                SimpleNamespace(type="human", content="question"),
                SimpleNamespace(type="ai", content="answer"),
            ]
        }

        self.assertEqual(agent_lark.extract_final_ai_text(result), "answer")

    def test_send_lark_text_reuses_cached_token_without_overwriting_function(self):
        agent_lark._tenant_access_token_value = ""
        agent_lark._tenant_access_token_expires_at = 0.0

        with (
            patch(
                "agent_lark._tenant_token_request",
                return_value={"tenant_access_token": "token", "expire": 7200},
            ) as token_request,
            patch("agent_lark._post_lark_json", return_value={"code": 0}) as post_json,
        ):
            agent_lark.send_lark_text("oc_1", "hello", app_id="app", app_secret="secret")
            agent_lark.send_lark_text("oc_1", "world", app_id="app", app_secret="secret")

        token_request.assert_called_once_with("app", "secret")
        self.assertEqual(post_json.call_count, 2)

    def test_bridge_handle_event_invokes_graph_and_replies(self):
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: {
            "messages": [SimpleNamespace(type="ai", content="收到")]
        }
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(
                    message_id="om_bridge",
                    chat_id="oc_bridge",
                    message_type="text",
                    content=json.dumps({"text": "ping"}),
                ),
            )
        )

        with patch("agent_lark.send_lark_text") as send_text:
            bridge.handle_event(event)
            bridge.executor.shutdown(wait=True)

        send_text.assert_called_once()
        self.assertEqual(send_text.call_args.args[:2], ("oc_bridge", "收到"))


if __name__ == "__main__":
    unittest.main()
