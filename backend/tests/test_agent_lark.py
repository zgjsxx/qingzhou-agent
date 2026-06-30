import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_lark
from agent_commands import CLEAR_RESPONSE, HELP_RESPONSE


class AgentLarkTest(unittest.TestCase):
    def test_worker_pool_uses_daemon_threads(self):
        pool = agent_lark.DaemonWorkerPool(max_workers=2, thread_name_prefix="test-lark")
        try:
            self.assertTrue(all(thread.daemon for thread in pool._threads))
        finally:
            pool.shutdown(wait=True)

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

    def test_bridge_clear_command_reuses_thread_and_clears_history(self):
        calls = []
        graph = SimpleNamespace(
            update_state=lambda config, values: calls.append((config, values)),
            invoke=lambda *_args, **_kwargs: self.fail("/clear must not invoke the model"),
        )
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        thread_id = agent_lark._thread_id_for_chat("oc_clear")
        agent_lark._store_thread_history(thread_id, [SimpleNamespace(type="human", content="old")])
        event = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(
                    message_id="om_clear",
                    chat_id="oc_clear",
                    message_type="text",
                    content=json.dumps({"text": "/clear"}),
                ),
            )
        )

        with patch("agent_lark.send_lark_text") as send_text:
            bridge.handle_event(event)
            bridge.executor.shutdown(wait=True)

        self.assertEqual(calls[0][0], {"configurable": {"thread_id": thread_id}})
        self.assertEqual(agent_lark._history_for_thread(thread_id), [])
        self.assertEqual(send_text.call_args.args[:2], ("oc_clear", CLEAR_RESPONSE))

    def test_bridge_help_command_replies_without_invoking_graph(self):
        graph = SimpleNamespace(invoke=lambda *_args, **_kwargs: self.fail("/help must not invoke the model"))
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(
                    message_id="om_help",
                    chat_id="oc_help",
                    message_type="text",
                    content=json.dumps({"text": "/help"}),
                ),
            )
        )

        with patch("agent_lark.send_lark_text") as send_text:
            bridge.handle_event(event)
            bridge.executor.shutdown(wait=True)

        self.assertEqual(send_text.call_args.args[:2], ("oc_help", HELP_RESPONSE))

    def test_add_lark_reaction_returns_reaction_id(self):
        with (
            patch(
                "agent_lark._get_tenant_access_token", return_value="tok",
            ),
            patch(
                "agent_lark._request_lark_json",
                return_value={"code": 0, "data": {"reaction_id": "re_abc"}},
            ) as request_json,
        ):
            result = agent_lark.add_lark_reaction("om_1", app_id="app", app_secret="secret")
        self.assertEqual(result, "re_abc")
        request_json.assert_called_once()
        self.assertIn("om_1/reactions", request_json.call_args.args[0])

    def test_delete_lark_reaction_calls_delete_endpoint(self):
        with (
            patch("agent_lark._get_tenant_access_token", return_value="tok"),
            patch("agent_lark.urllib.request.Request") as mock_request,
            patch("agent_lark.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda self: self
            mock_urlopen.return_value.__exit__ = lambda self, *a: None
            mock_urlopen.return_value.read.return_value = b'{"code": 0}'
            agent_lark.delete_lark_reaction("om_1", "re_abc", app_id="app", app_secret="secret")
        mock_request.assert_called_once()
        call_url = mock_request.call_args.args[0]
        self.assertIn("om_1/reactions/re_abc", call_url)
        self.assertEqual(mock_request.call_args.kwargs["method"], "DELETE")

    def test_handle_event_adds_reaction_before_processing(self):
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: {
            "messages": [SimpleNamespace(type="ai", content="ok")]
        }
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(
                    message_id="om_react",
                    chat_id="oc_react",
                    message_type="text",
                    content=json.dumps({"text": "hello"}),
                ),
            )
        )

        with (
            patch("agent_lark.add_lark_reaction", return_value="re_1") as add_reaction,
            patch("agent_lark.send_lark_text"),
            patch("agent_lark.delete_lark_reaction") as del_reaction,
        ):
            bridge.handle_event(event)
            bridge.executor.shutdown(wait=True)

        add_reaction.assert_called_once_with("om_react", app_id="app", app_secret="secret")
        del_reaction.assert_called_once_with("om_react", "re_1", app_id="app", app_secret="secret")

    def test_process_event_removes_reaction_on_success(self):
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: {
            "messages": [SimpleNamespace(type="ai", content="done")]
        }
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event = agent_lark.LarkMessageEvent(
            message_id="om_ok", chat_id="oc_ok", message_type="text", text="hi", sender_id="ou_1",
        )

        with (
            patch("agent_lark.send_lark_text"),
            patch("agent_lark.delete_lark_reaction") as del_reaction,
        ):
            bridge._process_event(event, "re_success")
            del_reaction.assert_called_once_with("om_ok", "re_success", app_id="app", app_secret="secret")

    def test_process_event_removes_reaction_on_failure(self):
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event = agent_lark.LarkMessageEvent(
            message_id="om_err", chat_id="oc_err", message_type="text", text="hi", sender_id="ou_1",
        )

        with (
            patch("agent_lark.send_lark_text"),
            patch("agent_lark.delete_lark_reaction") as del_reaction,
        ):
            bridge._process_event(event, "re_fail")
            del_reaction.assert_called_once_with("om_err", "re_fail", app_id="app", app_secret="secret")


if __name__ == "__main__":
    unittest.main()
