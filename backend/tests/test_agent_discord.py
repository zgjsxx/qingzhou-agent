import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.platforms import discord as agent_discord

sys.modules["agent_discord"] = agent_discord
from agent_commands import HELP_RESPONSE


def make_message(
    *,
    text="hello",
    channel_id=100,
    guild_id=None,
    sender_id=200,
    message_id=1,
    mentions=None,
    reference=None,
    attachments=None,
):
    channel = SimpleNamespace(id=channel_id, send=AsyncMock())
    message = SimpleNamespace(
        id=message_id,
        content=text,
        author=SimpleNamespace(id=sender_id, bot=False),
        channel=channel,
        guild=(
            SimpleNamespace(id=guild_id)
            if guild_id is not None
            else None
        ),
        mentions=mentions or [],
        reference=reference,
        attachments=attachments or [],
        reply=AsyncMock(),
    )
    return message


def make_bot_user(bot_id=999):
    return SimpleNamespace(id=bot_id, name="xu-agent")


class AgentDiscordTest(unittest.TestCase):
    def test_thread_id_is_stable(self):
        self.assertEqual(
            agent_discord._thread_id_for_channel("100/abc"),
            "discord_100_abc",
        )

    def test_dm_message_is_queued(self):
        async def run_test():
            bridge = agent_discord.DiscordBridge(
                SimpleNamespace(),
                "token",
                {"200"},
            )
            bridge._enqueue = AsyncMock()

            await bridge.handle_message(make_message(), make_bot_user())

            bridge._enqueue.assert_awaited_once()
            event = bridge._enqueue.await_args.args[0]
            self.assertEqual(event.text, "hello")
            self.assertEqual(event.sender_id, "200")

        asyncio.run(run_test())

    def test_disallowed_sender_is_ignored(self):
        async def run_test():
            bridge = agent_discord.DiscordBridge(
                SimpleNamespace(),
                "token",
                {"201"},
            )
            bridge._enqueue = AsyncMock()

            await bridge.handle_message(make_message(), make_bot_user())

            bridge._enqueue.assert_not_awaited()

        asyncio.run(run_test())

    def test_guild_message_requires_mention(self):
        async def run_test():
            bridge = agent_discord.DiscordBridge(
                SimpleNamespace(),
                "token",
                set(),
            )
            bridge._enqueue = AsyncMock()
            bot_user = make_bot_user()

            await bridge.handle_message(
                make_message(guild_id=300, text="hello"),
                bot_user,
            )
            bridge._enqueue.assert_not_awaited()

            await bridge.handle_message(
                make_message(
                    guild_id=300,
                    text="<@999> hello",
                    mentions=[bot_user],
                    message_id=2,
                ),
                bot_user,
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
            bridge = agent_discord.DiscordBridge(graph, "token", set())
            message = make_message(text="/help")
            event = agent_discord.DiscordMessageEvent(
                message_id="1",
                channel_id="100",
                guild_id="",
                sender_id="200",
                text="/help",
                message=message,
                attachment_paths=[],
            )

            await bridge._process_events([event])

            message.reply.assert_awaited_once_with(
                HELP_RESPONSE,
                mention_author=False,
            )

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
                                    content="Discord reply",
                                    id="ai-1",
                                )
                            ]
                        }
                    ]
                )
            )
            bridge = agent_discord.DiscordBridge(graph, "token", set())
            message = make_message()
            event = agent_discord.DiscordMessageEvent(
                message_id="1",
                channel_id="100",
                guild_id="",
                sender_id="200",
                text="hello",
                message=message,
                attachment_paths=[],
            )

            await bridge._process_events([event])

            message.reply.assert_awaited_once_with(
                "Discord reply",
                mention_author=False,
            )

        asyncio.run(run_test())

    def test_attachment_is_downloaded(self):
        async def run_test():
            bridge = agent_discord.DiscordBridge(
                SimpleNamespace(),
                "token",
                set(),
            )
            attachment = SimpleNamespace(
                filename="report.txt",
                save=AsyncMock(),
            )
            message = make_message(attachments=[attachment])

            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                agent_discord,
                "DISCORD_UPLOAD_DIR",
                Path(temp_dir),
            ):
                paths = await bridge._download_attachments(message)

            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith("report.txt"))
            attachment.save.assert_awaited_once()

        asyncio.run(run_test())

    def test_start_bridge_is_disabled_without_flag(self):
        with patch.dict(
            "os.environ",
            {"DISCORD_ENABLED": "false"},
            clear=False,
        ):
            agent_discord._started = False
            agent_discord.start_discord_bridge(SimpleNamespace())
            self.assertFalse(agent_discord._started)


if __name__ == "__main__":
    unittest.main()
