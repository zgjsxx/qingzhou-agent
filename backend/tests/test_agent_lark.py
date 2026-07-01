import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

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

    def test_parse_file_message_event(self):
        data = {
            "event": {
                "message": {
                    "message_id": "om_file",
                    "chat_id": "oc_file",
                    "message_type": "file",
                    "content": json.dumps({"file_key": "file_abc", "file_name": "report.pdf"}),
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.file_key, "file_abc")
        self.assertEqual(event.filename, "report.pdf")
        self.assertEqual(event.message_type, "file")
        self.assertEqual(event.text, "")

    def test_parse_image_message_event(self):
        data = {
            "event": {
                "message": {
                    "message_id": "om_img",
                    "chat_id": "oc_img",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img_xyz"}),
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.image_key, "img_xyz")
        self.assertEqual(event.message_type, "image")

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

    def test_bridge_handle_event_submits_to_buffer(self):
        """handle_event should add event to the merge buffer, not process directly."""
        graph = SimpleNamespace()
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        event_data = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_1")),
                message=SimpleNamespace(
                    message_id="om_buf",
                    chat_id="oc_buf",
                    message_type="text",
                    content=json.dumps({"text": "ping"}),
                ),
            )
        )

        with (
            patch("agent_lark.add_lark_reaction", return_value="re_1"),
            patch("agent_lark._remember_seen_message", return_value=True),
        ):
            bridge.handle_event(event_data)

        # Event should be in the pending buffer
        buf = agent_lark._pending_buffer
        self.assertIn("oc_buf", buf._events)

    def test_process_merged_events_text_command(self):
        """Slash commands in merged events should be handled."""
        calls = []
        graph = SimpleNamespace(
            update_state=lambda config, values: calls.append((config, values)),
            invoke=lambda *_args, **_kwargs: self.fail("/clear must not invoke model"),
        )
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_clear", chat_id="oc_clear", message_type="text",
                text="/clear", sender_id="ou_1",
            ),
        ]

        with patch("agent_lark.send_lark_text") as send_text:
            bridge._process_merged_events(events, ["re_1"])

        send_text.assert_called()
        self.assertEqual(send_text.call_args.args[:2], ("oc_clear", CLEAR_RESPONSE))

    def test_process_merged_events_invokes_graph(self):
        """Non-command text events should invoke the graph."""
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: {
            "messages": [SimpleNamespace(type="ai", content="收到")]
        }
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_run", chat_id="oc_run", message_type="text",
                text="你好", sender_id="ou_1",
            ),
        ]

        with (
            patch("agent_lark.send_lark_text") as send_text,
            patch("agent_lark._stream_channel_messages_enabled", return_value=False),
        ):
            bridge._process_merged_events(events, ["re_1"])

        send_text.assert_called()
        self.assertEqual(send_text.call_args.args[:2], ("oc_run", "收到"))

    def test_process_merged_events_file_event(self):
        """File events should download and produce text description."""
        graph = SimpleNamespace()
        graph.invoke = lambda *_args, **_kwargs: {
            "messages": [SimpleNamespace(type="ai", content="文件分析完了")]
        }
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_file", chat_id="oc_file", message_type="file",
                text="", file_key="file_abc", filename="report.pdf", sender_id="ou_1",
            ),
        ]

        download_info = {"path": "/tmp/report.pdf", "filename": "report.pdf", "size": "128KB"}
        with (
            patch("agent_lark._download_lark_resource", return_value=download_info) as mock_download,
            patch("agent_lark.send_lark_text") as send_text,
            patch("agent_lark._stream_channel_messages_enabled", return_value=False),
        ):
            bridge._process_merged_events(events, ["re_1"])

        send_text.assert_called()
        mock_download.assert_called_once_with(
            "om_file",
            "file_abc",
            "files",
            preferred_filename="report.pdf",
            app_id="app",
            app_secret="secret",
        )

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

    def test_finish_reactions_preserves_message_mapping(self):
        bridge = agent_lark.LarkWsBridge(
            graph=SimpleNamespace(), app_id="app", app_secret="secret",
        )
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_1", chat_id="oc_1", message_type="text",
                text="hi", sender_id="ou_1",
            ),
            agent_lark.LarkMessageEvent(
                message_id="om_2", chat_id="oc_1", message_type="text",
                text="there", sender_id="ou_1",
            ),
        ]
        with patch("agent_lark.delete_lark_reaction") as del_reaction:
            bridge._finish_reactions(events, ["re_1", "re_2"])
            self.assertEqual(
                del_reaction.call_args_list,
                [
                    unittest.mock.call("om_1", "re_1", app_id="app", app_secret="secret"),
                    unittest.mock.call("om_2", "re_2", app_id="app", app_secret="secret"),
                ],
            )

    def test_late_reaction_waits_until_message_finishes(self):
        bridge = agent_lark.LarkWsBridge(
            graph=SimpleNamespace(), app_id="app", app_secret="secret",
        )
        event = agent_lark.LarkMessageEvent(
            message_id="om_late", chat_id="oc_late", message_type="text",
            text="hi", sender_id="ou_1",
        )

        with (
            patch("agent_lark.add_lark_reaction", return_value="re_late"),
            patch("agent_lark._pending_buffer.set_reaction", return_value=False),
            patch("agent_lark.delete_lark_reaction") as del_reaction,
        ):
            bridge._add_reaction(event)
            del_reaction.assert_not_called()
            bridge._finish_reactions([event], [""])

        del_reaction.assert_called_once_with(
            "om_late", "re_late", app_id="app", app_secret="secret",
        )


if __name__ == "__main__":
    unittest.main()
