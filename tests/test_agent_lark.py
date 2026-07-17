import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.platforms import lark as agent_lark

sys.modules["agent_lark"] = agent_lark
from agent.commands import CLEAR_RESPONSE, HELP_RESPONSE


class AgentLarkTest(unittest.TestCase):
    def tearDown(self):
        buf = agent_lark._pending_buffer
        with buf._lock:
            for timer in buf._timers.values():
                timer.cancel()
            buf._timers.clear()
            buf._events.clear()
            buf._reaction_ids.clear()
        with agent_lark._seen_lock:
            agent_lark._seen_message_ids.clear()

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

    def test_parse_group_mentions(self):
        data = {
            "event": {
                "message": {
                    "message_id": "om_group",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "@轻舟 帮我看下"}),
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "name": "轻舟",
                            "id": {"open_id": "ou_bot"},
                        }
                    ],
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.chat_type, "group")
        self.assertIn("ou_bot", event.mention_ids)
        self.assertIn("轻舟", event.mention_names)

    def test_group_message_requires_mention_by_default(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_group",
            chat_id="oc_group",
            chat_type="group",
            message_type="text",
            text="普通群消息",
            sender_id="ou_1",
        )

        self.assertFalse(agent_lark.should_process_lark_event(event))

    def test_group_message_with_bot_mention_is_processed(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_group",
            chat_id="oc_group",
            chat_type="group",
            message_type="text",
            text="@轻舟 帮我看下",
            sender_id="ou_1",
            mention_ids=("ou_bot",),
        )

        with patch.dict("os.environ", {"LARK_BOT_OPEN_ID": "ou_bot"}, clear=False):
            self.assertTrue(agent_lark.should_process_lark_event(event))

    def test_group_message_with_raw_lark_mention_token_is_processed_without_bot_identity(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_group",
            chat_id="oc_group",
            chat_type="group",
            message_type="text",
            text="@_user_1 你好",
            sender_id="ou_1",
        )

        with patch.dict(
            "os.environ",
            {
                "LARK_BOT_OPEN_ID": "",
                "FEISHU_BOT_OPEN_ID": "",
                "LARK_BOT_USER_ID": "",
                "FEISHU_BOT_USER_ID": "",
                "LARK_BOT_UNION_ID": "",
                "FEISHU_BOT_UNION_ID": "",
                "LARK_BOT_NAME": "",
                "FEISHU_BOT_NAME": "",
            },
            clear=False,
        ):
            self.assertTrue(agent_lark.should_process_lark_event(event))

    def test_private_message_does_not_require_mention(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_private",
            chat_id="oc_private",
            chat_type="p2p",
            message_type="text",
            text="你好",
            sender_id="ou_1",
        )

        self.assertTrue(agent_lark.should_process_lark_event(event))

    def test_lark_allowed_users_empty_allows_everyone(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_private",
            chat_id="oc_private",
            chat_type="p2p",
            message_type="text",
            text="你好",
            sender_id="ou_anyone",
        )

        with patch.dict("os.environ", {"LARK_ALLOWED_USERS": "", "FEISHU_ALLOWED_USERS": ""}, clear=False):
            self.assertTrue(agent_lark.should_process_lark_event(event))

    def test_lark_allowed_users_allows_matching_sender(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_private",
            chat_id="oc_private",
            chat_type="p2p",
            message_type="text",
            text="你好",
            sender_id="ou_allowed",
        )

        with patch.dict("os.environ", {"LARK_ALLOWED_USERS": "ou_allowed,ou_other"}, clear=False):
            self.assertTrue(agent_lark.should_process_lark_event(event))

    def test_lark_allowed_users_blocks_non_matching_sender(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_private",
            chat_id="oc_private",
            chat_type="p2p",
            message_type="text",
            text="你好",
            sender_id="ou_blocked",
        )

        with patch.dict("os.environ", {"LARK_ALLOWED_USERS": "ou_allowed"}, clear=False):
            self.assertFalse(agent_lark.should_process_lark_event(event))

    def test_handle_event_ignores_unmentioned_group_message_before_reaction(self):
        graph = SimpleNamespace(invoke=MagicMock())
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        data = {
            "event": {
                "message": {
                    "message_id": "om_ignore",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "普通群消息"}),
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }

        with patch.object(bridge.reaction_executor, "submit") as submit_reaction:
            bridge.handle_event(data)

        submit_reaction.assert_not_called()
        graph.invoke.assert_not_called()

    def test_handle_event_ignores_disallowed_user_before_reaction(self):
        graph = SimpleNamespace(invoke=MagicMock())
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        data = {
            "event": {
                "message": {
                    "message_id": "om_user_blocked",
                    "chat_id": "oc_private",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "你好"}),
                },
                "sender": {"sender_id": {"open_id": "ou_blocked"}},
            }
        }

        with (
            patch.dict("os.environ", {"LARK_ALLOWED_USERS": "ou_allowed"}, clear=False),
            patch.object(bridge.reaction_executor, "submit") as submit_reaction,
        ):
            bridge.handle_event(data)

        submit_reaction.assert_not_called()
        graph.invoke.assert_not_called()

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

    def test_parse_audio_message_event(self):
        data = {
            "event": {
                "message": {
                    "message_id": "om_audio",
                    "chat_id": "oc_audio",
                    "message_type": "audio",
                    "content": json.dumps(
                        {"file_key": "file_audio", "file_name": "voice.opus", "duration": 1200}
                    ),
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }

        event = agent_lark.parse_lark_message_event(data)

        self.assertIsNotNone(event)
        self.assertEqual(event.file_key, "file_audio")
        self.assertEqual(event.filename, "voice.opus")
        self.assertEqual(event.duration_ms, 1200)
        self.assertEqual(event.message_type, "audio")

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
        first_payload = post_json.call_args_list[0].args[2]
        self.assertEqual(first_payload["msg_type"], "interactive")
        card = json.loads(first_payload["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["body"]["elements"][0]["tag"], "markdown")
        self.assertEqual(card["body"]["elements"][0]["content"], "hello")

    def test_send_lark_text_falls_back_when_markdown_card_fails(self):
        with (
            patch("agent_lark._get_tenant_access_token", return_value="token"),
            patch(
                "agent_lark._post_lark_json",
                side_effect=[{"code": 230001}, {"code": 0}],
            ) as post_json,
        ):
            agent_lark.send_lark_text(
                "oc_1",
                "**hello**",
                app_id="app",
                app_secret="secret",
            )

        self.assertEqual(post_json.call_count, 2)
        card_payload = post_json.call_args_list[0].args[2]
        text_payload = post_json.call_args_list[1].args[2]
        self.assertEqual(card_payload["msg_type"], "interactive")
        self.assertEqual(text_payload["msg_type"], "text")
        self.assertEqual(
            json.loads(text_payload["content"]),
            {"text": "**hello**"},
        )

    def test_send_lark_text_can_disable_markdown_cards(self):
        with (
            patch.dict("os.environ", {"LARK_MARKDOWN_ENABLED": "false"}),
            patch("agent_lark._get_tenant_access_token", return_value="token"),
            patch("agent_lark._post_lark_json", return_value={"code": 0}) as post_json,
        ):
            agent_lark.send_lark_text(
                "oc_1",
                "plain",
                app_id="app",
                app_secret="secret",
            )

        payload = post_json.call_args.args[2]
        self.assertEqual(payload["msg_type"], "text")

    def test_send_lark_text_strips_web_audio_markers(self):
        with (
            patch.dict("os.environ", {"LARK_MARKDOWN_ENABLED": "false"}),
            patch("agent_lark._get_tenant_access_token", return_value="token"),
            patch("agent_lark._post_lark_json", return_value={"code": 0}) as post_json,
        ):
            agent_lark.send_lark_text(
                "oc_1",
                'hello [[qingzhou-audio:{"url":"/api/local/downloads/a.wav"}]]',
                app_id="app",
                app_secret="secret",
            )

        payload = post_json.call_args.args[2]
        self.assertEqual(json.loads(payload["content"]), {"text": "hello"})

    def test_extract_qingzhou_audio_marker_paths_resolves_local_download_url(self):
        marker = (
            '[[qingzhou-audio:{"url":"/api/local/downloads/.agent_outputs/tts/voice.mp3"}]]'
        )

        paths = agent_lark._extract_qingzhou_audio_marker_paths(marker)

        self.assertEqual(
            paths,
            [(agent_lark.ROOT_DIR / ".agent_outputs" / "tts" / "voice.mp3").resolve()],
        )

    def test_extract_qingzhou_audio_marker_paths_rejects_path_escape(self):
        marker = '[[qingzhou-audio:{"url":"/api/local/downloads/../secret.wav"}]]'

        self.assertEqual(agent_lark._extract_qingzhou_audio_marker_paths(marker), [])

    def test_extract_local_download_paths_finds_only_images(self):
        text = (
            "![chart](http://localhost:3000/api/local/downloads/.agent_outputs/charts/a.png) "
            "[csv](http://localhost:3000/api/local/downloads/output/a.csv)"
        )

        paths = agent_lark._extract_local_download_paths(text, agent_lark.IMAGE_SUFFIXES)

        self.assertEqual(
            paths,
            [(agent_lark.ROOT_DIR / ".agent_outputs" / "charts" / "a.png").resolve()],
        )

    def test_upload_lark_file_returns_file_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.opus"
            audio_path.write_bytes(b"opus")

            with (
                patch("agent_lark._get_tenant_access_token", return_value="token"),
                patch(
                    "agent_lark._request_lark_multipart",
                    return_value={"code": 0, "data": {"file_key": "file_voice"}},
                ) as request_multipart,
            ):
                file_key = agent_lark._upload_lark_file(
                    audio_path,
                    file_type="opus",
                    app_id="app",
                    app_secret="secret",
                )

        self.assertEqual(file_key, "file_voice")
        request_multipart.assert_called_once()
        self.assertEqual(request_multipart.call_args.kwargs["fields"]["file_type"], "opus")
        self.assertIn("file", request_multipart.call_args.kwargs["files"])

    def test_upload_lark_image_returns_image_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "chart.png"
            image_path.write_bytes(b"png")

            with (
                patch("agent_lark._get_tenant_access_token", return_value="token"),
                patch(
                    "agent_lark._request_lark_multipart",
                    return_value={"code": 0, "data": {"image_key": "img_key"}},
                ) as request_multipart,
            ):
                image_key = agent_lark._upload_lark_image(
                    image_path,
                    app_id="app",
                    app_secret="secret",
                )

        self.assertEqual(image_key, "img_key")
        request_multipart.assert_called_once()
        self.assertEqual(request_multipart.call_args.kwargs["fields"]["image_type"], "message")
        self.assertIn("image", request_multipart.call_args.kwargs["files"])

    def test_send_lark_image_uploads_and_sends_image_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "chart.png"
            image_path.write_bytes(b"png")

            with (
                patch("agent_lark._upload_lark_image", return_value="img_key") as upload,
                patch("agent_lark._get_tenant_access_token", return_value="token"),
                patch("agent_lark._send_lark_message", return_value={"code": 0}) as send_message,
            ):
                agent_lark.send_lark_image(
                    "oc_1",
                    image_path,
                    app_id="app",
                    app_secret="secret",
                )

        upload.assert_called_once_with(image_path, app_id="app", app_secret="secret", token="token")
        send_message.assert_called_once_with(
            "oc_1",
            "token",
            msg_type="image",
            content={"image_key": "img_key"},
        )

    def test_send_lark_audio_uploads_and_sends_audio_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.opus"
            audio_path.write_bytes(b"opus")

            with (
                patch("agent_lark._convert_audio_to_opus", return_value=audio_path) as convert,
                patch("agent_lark._upload_lark_file", return_value="file_voice") as upload,
                patch("agent_lark._get_tenant_access_token", return_value="token"),
                patch("agent_lark._send_lark_message", return_value={"code": 0}) as send_message,
            ):
                agent_lark.send_lark_audio(
                    "oc_1",
                    audio_path,
                    app_id="app",
                    app_secret="secret",
                )

        convert.assert_called_once_with(audio_path)
        upload.assert_called_once_with(
            audio_path,
            file_type="opus",
            app_id="app",
            app_secret="secret",
            token="token",
        )
        send_message.assert_called_once_with(
            "oc_1",
            "token",
            msg_type="audio",
            content={"file_key": "file_voice"},
        )

    def test_transcribe_with_asr_server_posts_audio_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "voice.opus"
            audio_path.write_bytes(b"opus")

            with (
                patch.dict("os.environ", {"QINGZHOU_ASR_URL": "http://127.0.0.1:9999"}),
                patch(
                    "agent_lark._request_local_multipart",
                    return_value={"ok": True, "text": "转写内容"},
                ) as request_multipart,
            ):
                result = agent_lark._transcribe_with_asr_server(audio_path, "auto")

        self.assertEqual(result["text"], "转写内容")
        request_multipart.assert_called_once()
        self.assertEqual(request_multipart.call_args.args[0], "http://127.0.0.1:9999/transcribe")
        self.assertEqual(request_multipart.call_args.kwargs["fields"], {"language": "auto"})
        self.assertIn("file", request_multipart.call_args.kwargs["files"])

    def test_send_lark_audio_markers_sends_marker_audio(self):
        audio_path = agent_lark.ROOT_DIR / ".agent_outputs" / "tts" / "marker-test.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"wav")
        marker = (
            f'hello [[qingzhou-audio:{{"url":"/api/local/downloads/.agent_outputs/tts/{audio_path.name}"}}]]'
        )
        try:
            with patch("agent_lark.send_lark_audio") as send_audio:
                sent = agent_lark.send_lark_audio_markers(
                    "oc_1",
                    marker,
                    app_id="app",
                    app_secret="secret",
                )
        finally:
            audio_path.unlink(missing_ok=True)

        self.assertTrue(sent)
        send_audio.assert_called_once_with(
            "oc_1",
            audio_path.resolve(),
            app_id="app",
            app_secret="secret",
        )

    def test_send_lark_images_from_text_sends_local_images(self):
        image_path = agent_lark.ROOT_DIR / ".agent_outputs" / "images" / "marker-test.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"png")
        text = f"![chart](/api/local/downloads/.agent_outputs/images/{image_path.name})"
        try:
            with patch("agent_lark.send_lark_image") as send_image:
                sent = agent_lark.send_lark_images_from_text(
                    "oc_1",
                    text,
                    app_id="app",
                    app_secret="secret",
                )
        finally:
            image_path.unlink(missing_ok=True)

        self.assertTrue(sent)
        send_image.assert_called_once_with(
            "oc_1",
            image_path.resolve(),
            app_id="app",
            app_secret="secret",
        )

    def test_send_lark_voice_reply_synthesizes_and_sends_audio_when_enabled(self):
        with (
            patch.dict(
                "os.environ",
                {"LARK_VOICE_REPLY_ENABLED": "true", "AGENT_TTS_ENABLED": "true"},
            ),
            patch("agent_lark.synthesize_speech", return_value={"path": "voice.wav"}) as synthesize,
            patch("agent_lark.send_lark_audio") as send_audio,
        ):
            sent = agent_lark.send_lark_voice_reply(
                "oc_1",
                "hello",
                app_id="app",
                app_secret="secret",
            )

        self.assertTrue(sent)
        synthesize.assert_called_once_with("hello", voice="", audio_format="opus")
        send_audio.assert_called_once_with("oc_1", "voice.wav", app_id="app", app_secret="secret")

    def test_bridge_prefers_marker_audio_over_auto_voice_reply(self):
        bridge = agent_lark.LarkWsBridge(graph=SimpleNamespace(), app_id="app", app_secret="secret")
        text = 'hello [[qingzhou-audio:{"url":"/api/local/downloads/.agent_outputs/tts/voice.mp3"}]]'

        with (
            patch("agent_lark.send_lark_images_from_text") as send_images,
            patch("agent_lark.send_lark_audio_markers", return_value=True) as send_markers,
            patch("agent_lark.send_lark_voice_reply") as send_voice,
        ):
            bridge._send_voice_reply_if_enabled("oc_1", text)

        send_images.assert_called_once_with("oc_1", text, app_id="app", app_secret="secret")
        send_markers.assert_called_once_with("oc_1", text, app_id="app", app_secret="secret")
        send_voice.assert_not_called()

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
            patch("agent_lark.send_lark_voice_reply") as send_voice,
            patch("agent_lark._stream_channel_messages_enabled", return_value=False),
        ):
            bridge._process_merged_events(events, ["re_1"])

        send_text.assert_called()
        self.assertEqual(send_text.call_args.args[:2], ("oc_run", "收到"))
        send_voice.assert_called_once_with("oc_run", "收到", app_id="app", app_secret="secret")

    def test_process_merged_events_interrupt_sends_approval_card(self):
        interrupt_value = {
            "action_requests": [
                {
                    "name": "run_shell_command",
                    "args": {"command": "Remove-Item demo.txt"},
                    "description": "Potentially destructive file removal command.",
                }
            ],
            "review_configs": [
                {
                    "action_name": "run_shell_command",
                    "allowed_decisions": ["approve", "reject"],
                }
            ],
        }
        graph = SimpleNamespace(
            invoke=lambda *_args, **_kwargs: {
                "__interrupt__": [SimpleNamespace(value=interrupt_value, id="int_1")]
            }
        )
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_run", chat_id="oc_run", message_type="text",
                text="删除 demo.txt", sender_id="ou_1",
            ),
        ]

        with (
            patch("agent_lark.send_lark_approval_card") as send_card,
            patch("agent_lark.send_lark_text") as send_text,
            patch("agent_lark._stream_channel_messages_enabled", return_value=False),
        ):
            bridge._process_merged_events(events, ["re_1"])

        send_card.assert_called_once()
        self.assertEqual(send_card.call_args.args[0], "oc_run")
        approval_id = send_card.call_args.args[1]
        self.assertIn(approval_id, bridge._pending_approvals)
        send_text.assert_not_called()

    def test_handle_card_action_resumes_pending_approval(self):
        calls = []

        def invoke(payload, *_args, **_kwargs):
            calls.append(payload)
            return {"messages": [SimpleNamespace(type="ai", content="已执行")]}

        graph = SimpleNamespace(invoke=invoke)
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        pending = agent_lark.LarkPendingApproval(
            approval_id="appr_1",
            thread_id="lark_oc_run",
            chat_id="oc_run",
            requester_id="ou_1",
            interrupt_value={},
            message_ids=("om_run",),
            reaction_ids=("re_1",),
        )
        bridge._pending_approvals[pending.approval_id] = pending
        card_event = {
            "event": {
                "operator": {"open_id": "ou_1"},
                "action": {"value": {"approval_id": "appr_1", "decision": "approve"}},
            }
        }

        with (
            patch.object(bridge.executor, "submit", side_effect=lambda fn, *args: fn(*args)),
            patch("agent_lark.send_lark_text") as send_text,
            patch("agent_lark.send_lark_voice_reply"),
            patch("agent_lark.delete_lark_reaction"),
            patch("agent_lark.add_lark_reaction"),
        ):
            response = bridge.handle_card_action(card_event)

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(len(calls), 1)
        self.assertEqual(send_text.call_args.args[:2], ("oc_run", "已执行"))
        self.assertNotIn("appr_1", bridge._pending_approvals)

    def test_handle_card_action_rejects_unauthorized_operator(self):
        graph = SimpleNamespace(invoke=MagicMock())
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        pending = agent_lark.LarkPendingApproval(
            approval_id="appr_1",
            thread_id="lark_oc_run",
            chat_id="oc_run",
            requester_id="ou_requester",
            interrupt_value={},
            message_ids=("om_run",),
            reaction_ids=("re_1",),
        )
        bridge._pending_approvals[pending.approval_id] = pending
        card_event = {
            "event": {
                "operator": {"open_id": "ou_other"},
                "action": {"value": {"approval_id": "appr_1", "decision": "approve"}},
            }
        }

        with patch.object(bridge.executor, "submit") as submit:
            response = bridge.handle_card_action(card_event)

        self.assertEqual(response["toast"]["type"], "warning")
        submit.assert_not_called()
        graph.invoke.assert_not_called()
        self.assertIn("appr_1", bridge._pending_approvals)

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

    def test_lark_file_kind_supports_office_and_text_files(self):
        cases = {
            "notes.txt": "text",
            "readme.md": "text",
            "table.csv": "text",
            "plan.doc": "word",
            "plan.docx": "word",
            "budget.xls": "excel",
            "budget.xlsx": "excel",
            "report.pdf": "pdf",
        }

        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(agent_lark._lark_file_kind(filename), expected)

    def test_lark_excel_file_event_prompt_includes_local_file_hint(self):
        event = agent_lark.LarkMessageEvent(
            message_id="om_xlsx",
            chat_id="oc_file",
            message_type="file",
            text="",
            file_key="file_xlsx",
            filename="budget.xlsx",
            sender_id="ou_1",
        )
        download_info = {"path": "/tmp/budget.xlsx", "filename": "budget.xlsx", "size": "12KB"}

        with patch("agent_lark._download_lark_resource", return_value=download_info) as mock_download:
            fragment = agent_lark._event_to_text_fragment(event, "app", "secret")

        mock_download.assert_called_once_with(
            "om_xlsx",
            "file_xlsx",
            "files",
            preferred_filename="budget.xlsx",
            app_id="app",
            app_secret="secret",
        )
        self.assertIn("File message", fragment)
        self.assertIn("filename: budget.xlsx", fragment)
        self.assertIn("kind: excel", fragment)
        self.assertIn("path: /tmp/budget.xlsx", fragment)
        self.assertIn("spreadsheet/xlsx parsing tools", fragment)

    def test_process_merged_events_audio_event_transcribes_before_invoke(self):
        captured_payloads = []

        def invoke(payload, *_args, **_kwargs):
            captured_payloads.append(payload)
            return {"messages": [SimpleNamespace(type="ai", content="收到语音")]}

        graph = SimpleNamespace(invoke=invoke)
        bridge = agent_lark.LarkWsBridge(graph=graph, app_id="app", app_secret="secret")
        events = [
            agent_lark.LarkMessageEvent(
                message_id="om_audio",
                chat_id="oc_audio",
                message_type="audio",
                text="",
                file_key="file_audio",
                filename="voice.opus",
                duration_ms=1200,
                sender_id="ou_1",
            ),
        ]
        download_info = {"path": "/tmp/voice.opus", "filename": "voice.opus", "size": "8KB"}

        with (
            patch("agent_lark._download_lark_resource", return_value=download_info) as mock_download,
            patch("agent_lark._transcribe_lark_audio", return_value={"text": "帮我查一下天气"}) as transcribe,
            patch("agent_lark.send_lark_text"),
            patch("agent_lark._stream_channel_messages_enabled", return_value=False),
        ):
            bridge._process_merged_events(events, ["re_1"])

        mock_download.assert_called_once_with(
            "om_audio",
            "file_audio",
            "audio",
            preferred_filename="voice.opus",
            app_id="app",
            app_secret="secret",
        )
        transcribe.assert_called_once_with("/tmp/voice.opus")
        user_content = captured_payloads[0]["messages"][-1]["content"]
        self.assertIn("语音消息", user_content)
        self.assertIn("transcription: 帮我查一下天气", user_content)

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
        with (
            patch("agent_lark.delete_lark_reaction") as del_reaction,
            patch("agent_lark.add_lark_reaction") as add_reaction,
        ):
            bridge._finish_reactions(events, ["re_1", "re_2"], agent_lark.LARK_DONE_EMOJI)
            self.assertEqual(
                del_reaction.call_args_list,
                [
                    unittest.mock.call("om_1", "re_1", app_id="app", app_secret="secret"),
                    unittest.mock.call("om_2", "re_2", app_id="app", app_secret="secret"),
                ],
            )
            self.assertEqual(
                add_reaction.call_args_list,
                [
                    unittest.mock.call(
                        "om_1",
                        emoji_type=agent_lark.LARK_DONE_EMOJI,
                        app_id="app",
                        app_secret="secret",
                    ),
                    unittest.mock.call(
                        "om_2",
                        emoji_type=agent_lark.LARK_DONE_EMOJI,
                        app_id="app",
                        app_secret="secret",
                    ),
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
            patch("agent_lark.add_lark_reaction", side_effect=["re_late", "re_done"]) as add_reaction,
            patch("agent_lark._pending_buffer.set_reaction", return_value=False),
            patch("agent_lark.delete_lark_reaction") as del_reaction,
        ):
            bridge._add_reaction(event)
            del_reaction.assert_not_called()
            bridge._finish_reactions([event], [""], agent_lark.LARK_DONE_EMOJI)

        del_reaction.assert_called_once_with(
            "om_late", "re_late", app_id="app", app_secret="secret",
        )
        self.assertEqual(add_reaction.call_count, 2)
        add_reaction.assert_called_with(
            "om_late",
            emoji_type=agent_lark.LARK_DONE_EMOJI,
            app_id="app",
            app_secret="secret",
        )


if __name__ == "__main__":
    unittest.main()
