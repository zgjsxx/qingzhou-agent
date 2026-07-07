import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.platforms import botpy as agent_botpy

sys.modules["agent_botpy"] = agent_botpy
from agent_commands import CLEAR_RESPONSE, HELP_RESPONSE


class AgentBotpyTest(unittest.TestCase):
    def test_parse_botpy_at_message_event_strips_mention(self):
        message = SimpleNamespace(
            id="msg-1",
            content="<@!12345> /help",
            channel_id="channel-1",
            guild_id="guild-1",
            author=SimpleNamespace(id="user-1", user_openid="openid-1"),
        )

        event = agent_botpy.parse_botpy_message_event("at_message_create", message)

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "/help")
        self.assertEqual(event.chat_id, "guild:guild-1:channel:channel-1")
        self.assertEqual(event.sender_id, "user-1")

    def test_thread_id_for_chat_is_stable_and_safe(self):
        self.assertEqual(agent_botpy._thread_id_for_chat("guild:1/channel 2"), "botpy_guild:1_channel_2")

    def test_extract_final_ai_text(self):
        result = {
            "messages": [
                SimpleNamespace(type="human", content="question"),
                SimpleNamespace(type="ai", content="answer"),
            ]
        }

        self.assertEqual(agent_botpy.extract_final_ai_text(result), "answer")

    def test_handle_help_command_replies_without_invoking_graph(self):
        async def run_test():
            graph = SimpleNamespace(invoke=lambda *_args, **_kwargs: self.fail("/help must not invoke the graph"))
            bridge = agent_botpy.BotpyBridgeClient(graph=graph, appid="app", secret="secret", is_sandbox=False)
            message = SimpleNamespace(
                id="msg-help",
                content="/help",
                channel_id="channel-1",
                guild_id="guild-1",
                author=SimpleNamespace(id="user-1", user_openid="openid-1"),
                reply=AsyncMock(),
            )

            with patch("agent_botpy._stream_channel_messages_enabled", return_value=False):
                await bridge._handle_message("at_message_create", message)

            message.reply.assert_awaited_once_with(content=HELP_RESPONSE)

        asyncio.run(run_test())

    def test_handle_clear_command_clears_history_without_invoking_graph(self):
        async def run_test():
            calls = []
            graph = SimpleNamespace(
                update_state=lambda config, values: calls.append((config, values)),
                invoke=lambda *_args, **_kwargs: self.fail("/clear must not invoke the graph"),
            )
            bridge = agent_botpy.BotpyBridgeClient(graph=graph, appid="app", secret="secret", is_sandbox=False)
            thread_id = agent_botpy._thread_id_for_chat("guild:guild-1:channel:channel-1")
            agent_botpy._store_thread_history(thread_id, [SimpleNamespace(type="human", content="old")])
            message = SimpleNamespace(
                id="msg-clear",
                content="/clear",
                channel_id="channel-1",
                guild_id="guild-1",
                author=SimpleNamespace(id="user-1", user_openid="openid-1"),
                reply=AsyncMock(),
            )

            with patch("agent_botpy._stream_channel_messages_enabled", return_value=False):
                await bridge._handle_message("at_message_create", message)

            self.assertEqual(calls[0][0], {"configurable": {"thread_id": thread_id}})
            self.assertEqual(agent_botpy._history_for_thread(thread_id), [])
            message.reply.assert_awaited_once_with(content=CLEAR_RESPONSE)

        asyncio.run(run_test())

    def test_handle_regular_message_invokes_graph_and_replies(self):
        async def run_test():
            graph = SimpleNamespace(
                invoke=lambda *_args, **_kwargs: {
                    "messages": [SimpleNamespace(type="ai", content="收到 QQ 消息")]
                }
            )
            bridge = agent_botpy.BotpyBridgeClient(graph=graph, appid="app", secret="secret", is_sandbox=False)
            message = SimpleNamespace(
                id="msg-normal",
                content="hello",
                channel_id="channel-1",
                guild_id="guild-1",
                author=SimpleNamespace(id="user-1", user_openid="openid-1"),
                reply=AsyncMock(),
            )

            with patch("agent_botpy._stream_channel_messages_enabled", return_value=False):
                await bridge._handle_message("at_message_create", message)

            message.reply.assert_awaited_once_with(content="收到 QQ 消息")

        asyncio.run(run_test())

    def test_start_botpy_bridge_is_disabled_without_flag(self):
        with patch.dict("os.environ", {"BOTPY_ENABLED": "false"}, clear=False):
            agent_botpy._started = False
            agent_botpy.start_botpy_bridge(SimpleNamespace())
            self.assertFalse(agent_botpy._started)

    def test_run_botpy_bridge_forever_creates_thread_local_event_loop(self):
        loop = asyncio.new_event_loop()
        bridge = SimpleNamespace(run_forever=lambda: None)

        with (
            patch("agent_botpy.asyncio.new_event_loop", return_value=loop) as new_loop,
            patch("agent_botpy._run_with_blockbuster_skip") as run_with_skip,
            patch("agent_botpy.asyncio.set_event_loop") as set_event_loop,
        ):
            agent_botpy._run_botpy_bridge_forever(bridge)

        new_loop.assert_called_once()
        set_event_loop.assert_any_call(loop)
        set_event_loop.assert_any_call(None)
        run_with_skip.assert_called_once_with(bridge.run_forever)


if __name__ == "__main__":
    unittest.main()
