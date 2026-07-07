import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gateway.platforms import telegram as agent_telegram

sys.modules["agent_telegram"] = agent_telegram
from agent_commands import HELP_RESPONSE


def make_update(
    *,
    text="hello",
    chat_id=100,
    chat_type="private",
    sender_id=200,
    message_id=1,
    reply_text=None,
):
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        document=None,
        photo=None,
        reply_to_message=None,
        reply_text=reply_text or AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=sender_id, is_bot=False),
    )
    return update


def make_context(username="xu_agent_bot", bot_id=999):
    return SimpleNamespace(
        bot=SimpleNamespace(
            id=bot_id,
            username=username,
            get_file=AsyncMock(),
        )
    )


class AgentTelegramTest(unittest.TestCase):
    def test_thread_id_is_stable(self):
        self.assertEqual(
            agent_telegram._thread_id_for_chat("-100/abc"),
            "telegram_-100_abc",
        )

    def test_private_message_is_queued(self):
        async def run_test():
            bridge = agent_telegram.TelegramBridge(
                SimpleNamespace(),
                "token",
                {"200"},
            )
            bridge._enqueue = AsyncMock()

            await bridge.handle_update(make_update(), make_context())

            bridge._enqueue.assert_awaited_once()
            event = bridge._enqueue.await_args.args[0]
            self.assertEqual(event.text, "hello")
            self.assertEqual(event.sender_id, "200")

        asyncio.run(run_test())

    def test_disallowed_sender_is_ignored(self):
        async def run_test():
            bridge = agent_telegram.TelegramBridge(
                SimpleNamespace(),
                "token",
                {"201"},
            )
            bridge._enqueue = AsyncMock()

            await bridge.handle_update(make_update(), make_context())

            bridge._enqueue.assert_not_awaited()

        asyncio.run(run_test())

    def test_group_message_requires_mention(self):
        async def run_test():
            bridge = agent_telegram.TelegramBridge(
                SimpleNamespace(),
                "token",
                set(),
            )
            bridge._enqueue = AsyncMock()
            context = make_context()

            await bridge.handle_update(
                make_update(chat_type="group", text="hello"),
                context,
            )
            bridge._enqueue.assert_not_awaited()

            await bridge.handle_update(
                make_update(
                    chat_type="group",
                    text="@xu_agent_bot hello",
                    message_id=2,
                ),
                context,
            )
            bridge._enqueue.assert_awaited_once()
            event = bridge._enqueue.await_args.args[0]
            self.assertEqual(event.text, "hello")

        asyncio.run(run_test())

    def test_help_command_replies_without_graph(self):
        async def run_test():
            graph = SimpleNamespace(
                stream=lambda *_args, **_kwargs: self.fail(
                    "/help must not invoke graph"
                )
            )
            bridge = agent_telegram.TelegramBridge(graph, "token", set())
            message = make_update(text="/help").effective_message
            event = agent_telegram.TelegramMessageEvent(
                message_id="1",
                chat_id="100",
                chat_type="private",
                sender_id="200",
                text="/help",
                message=message,
            )

            await bridge._process_events([event])

            message.reply_text.assert_awaited_once_with(HELP_RESPONSE)

        asyncio.run(run_test())

    def test_regular_message_streams_graph_reply(self):
        async def run_test():
            graph = SimpleNamespace(
                stream=lambda *_args, **_kwargs: iter(
                    [
                        {
                            "messages": [
                                SimpleNamespace(
                                    type="ai",
                                    content="Telegram reply",
                                    id="ai-1",
                                )
                            ]
                        }
                    ]
                )
            )
            bridge = agent_telegram.TelegramBridge(graph, "token", set())
            message = make_update().effective_message
            event = agent_telegram.TelegramMessageEvent(
                message_id="1",
                chat_id="100",
                chat_type="private",
                sender_id="200",
                text="hello",
                message=message,
            )

            await bridge._process_events([event])

            message.reply_text.assert_awaited_once_with("Telegram reply")

        asyncio.run(run_test())

    def test_document_is_downloaded(self):
        async def run_test():
            bridge = agent_telegram.TelegramBridge(
                SimpleNamespace(),
                "token",
                set(),
            )
            message = make_update().effective_message
            message.document = SimpleNamespace(
                file_id="file-1",
                file_name="report.txt",
            )
            telegram_file = SimpleNamespace(download_to_drive=AsyncMock())
            context = make_context()
            context.bot.get_file.return_value = telegram_file

            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                agent_telegram,
                "TELEGRAM_UPLOAD_DIR",
                Path(temp_dir),
            ):
                path, kind = await bridge._download_attachment(
                    message,
                    context,
                )

            self.assertEqual(kind, "document")
            self.assertTrue(path.endswith("report.txt"))
            telegram_file.download_to_drive.assert_awaited_once()

        asyncio.run(run_test())

    def test_start_bridge_is_disabled_without_flag(self):
        with patch.dict(
            "os.environ",
            {"TELEGRAM_ENABLED": "false"},
            clear=False,
        ):
            agent_telegram._started = False
            agent_telegram.start_telegram_bridge(SimpleNamespace())
            self.assertFalse(agent_telegram._started)


if __name__ == "__main__":
    unittest.main()
