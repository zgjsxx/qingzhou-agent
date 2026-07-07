import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.platforms import weixin as agent_weixin

sys.modules["agent_weixin"] = agent_weixin
from agent_commands import HELP_RESPONSE


class AgentWeixinTest(unittest.TestCase):
    def test_headers_include_ilink_authentication(self):
        headers = agent_weixin._headers("secret", "{}")

        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(headers["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(headers["iLink-App-Id"], "bot")

    def test_extract_text(self):
        message = {
            "item_list": [
                {"type": 1, "text_item": {"text": "你好"}},
            ]
        }

        self.assertEqual(agent_weixin._extract_text(message), "你好")

    def test_send_text_echoes_context_token(self):
        async def run_test():
            credentials = {
                "account_id": "bot@im.bot",
                "token": "secret",
                "base_url": "https://example.test",
            }
            with (
                tempfile.TemporaryDirectory() as temp_dir,
                patch.object(
                    agent_weixin,
                    "CONTEXT_TOKENS_FILE",
                    Path(temp_dir) / "tokens.json",
                ),
                patch("agent_weixin._api_post", new_callable=AsyncMock) as post,
            ):
                post.return_value = {"ret": 0}
                bridge = agent_weixin.WeixinBridge(SimpleNamespace(), credentials)
                bridge.context_tokens["user@im.wechat"] = "context-1"

                await bridge.send_text("user@im.wechat", "收到")

                payload = post.await_args.args[2]
                self.assertEqual(payload["msg"]["context_token"], "context-1")
                self.assertEqual(
                    payload["msg"]["item_list"][0]["text_item"]["text"],
                    "收到",
                )

        asyncio.run(run_test())

    def test_help_command_does_not_invoke_graph(self):
        async def run_test():
            credentials = {
                "account_id": "bot@im.bot",
                "token": "secret",
                "base_url": "https://example.test",
            }
            graph = SimpleNamespace(
                invoke=lambda *_args, **_kwargs: self.fail(
                    "/help must not invoke the graph"
                )
            )
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                agent_weixin,
                "CONTEXT_TOKENS_FILE",
                Path(temp_dir) / "tokens.json",
            ):
                bridge = agent_weixin.WeixinBridge(graph, credentials)
                bridge.send_text = AsyncMock()

                await bridge._process_message(
                    {
                        "from_user_id": "user@im.wechat",
                        "message_id": "message-1",
                        "context_token": "context-1",
                        "item_list": [
                            {"type": 1, "text_item": {"text": "/help"}},
                        ],
                    }
                )

                bridge.send_text.assert_awaited_once_with(
                    "user@im.wechat",
                    HELP_RESPONSE,
                )

        asyncio.run(run_test())

    def test_regular_message_streams_graph_reply(self):
        async def run_test():
            credentials = {
                "account_id": "bot@im.bot",
                "token": "secret",
                "base_url": "https://example.test",
            }
            graph = SimpleNamespace(
                stream=lambda *_args, **_kwargs: iter(
                    [
                        {
                            "messages": [
                                SimpleNamespace(
                                    type="ai",
                                    content="微信回复",
                                    id="ai-1",
                                )
                            ]
                        }
                    ]
                )
            )
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                agent_weixin,
                "CONTEXT_TOKENS_FILE",
                Path(temp_dir) / "tokens.json",
            ):
                bridge = agent_weixin.WeixinBridge(graph, credentials)
                bridge.send_text = AsyncMock()

                await bridge._process_message(
                    {
                        "from_user_id": "user-2@im.wechat",
                        "message_id": "message-2",
                        "item_list": [
                            {"type": 1, "text_item": {"text": "你好"}},
                        ],
                    }
                )

                bridge.send_text.assert_awaited_once_with(
                    "user-2@im.wechat",
                    "微信回复",
                )

        asyncio.run(run_test())

    def test_qr_login_saves_confirmed_credentials(self):
        async def run_test():
            responses = [
                {
                    "qrcode": "qr-id",
                    "qrcode_img_content": "https://example.test/qr",
                },
                {
                    "status": "confirmed",
                    "ilink_bot_id": "bot@im.bot",
                    "bot_token": "secret",
                    "baseurl": "https://ilink.example.test",
                    "ilink_user_id": "owner@im.wechat",
                },
            ]
            with (
                patch(
                    "agent_weixin._api_get",
                    new_callable=AsyncMock,
                    side_effect=responses,
                ),
                patch("agent_weixin._print_qr_ascii"),
                patch("agent_weixin.save_account") as save,
            ):
                credentials = await agent_weixin.qr_login(timeout_seconds=2)

            self.assertEqual(credentials["account_id"], "bot@im.bot")
            self.assertEqual(credentials["token"], "secret")
            save.assert_called_once_with(credentials)

        asyncio.run(run_test())

    def test_start_bridge_requires_feature_flag(self):
        with patch.dict("os.environ", {"WEIXIN_ENABLED": "false"}, clear=False):
            agent_weixin._started = False
            agent_weixin.start_weixin_bridge(SimpleNamespace())
            self.assertFalse(agent_weixin._started)

    def test_run_bridge_uses_blockbuster_skip_wrapper(self):
        graph = SimpleNamespace()
        credentials = {
            "account_id": "bot@im.bot",
            "token": "secret",
            "base_url": "https://example.test",
        }
        with patch("agent_weixin._run_with_blockbuster_skip") as run_with_skip:
            agent_weixin._run_bridge(graph, credentials)

        run_with_skip.assert_called_once()
        self.assertTrue(callable(run_with_skip.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
