"""Function-calling agent built with LangChain."""

import sys
import os
import threading
from pathlib import Path

# Make the repository root importable when LangGraph loads this file by path.
AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import config_str
from agent.commands import AgentCommandMiddleware
from agent.context import AgentContextCompactMiddleware, XuAgentState
from agent.context_references import AgentContextReferenceMiddleware
from agent.cron import start_cron_scheduler
from agent.logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent.memory import AgentMemoryMiddleware
from agent.mcp import load_mcp_tools
from agent.permissions import AgentPermissionMiddleware
from agent.prompt import build_prompt_context, get_system_prompt
from agent.llm_config import configure_provider_env, provider_model_kwargs
from agent.skills import skill_catalog_for_prompt
from agent.tts import tts_enabled, warm_tts_engine
from tools import ALL_TOOLS
from gateway.platforms.botpy import start_botpy_bridge
from gateway.platforms.discord import start_discord_bridge
from gateway.platforms.lark import start_lark_ws_bridge
from gateway.platforms.telegram import start_telegram_bridge
from gateway.platforms.weixin import start_weixin_bridge

LLM_ADAPTER_TYPE = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
LLM_MODEL = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
LLM_AUTH_TOKEN = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").strip()
QINGZHOU_CLI_MODE = os.getenv("QINGZHOU_CLI", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def configure_llm_provider_env() -> None:
    """Map project-level LLM_* settings to provider-specific LangChain env vars."""
    configure_provider_env(
        adapter=LLM_ADAPTER_TYPE,
        api_key=LLM_API_KEY,
        auth_token=LLM_AUTH_TOKEN,
        base_url=LLM_BASE_URL,
    )


configure_llm_provider_env()


def _warm_tts_engine_in_background() -> None:
    if not tts_enabled():
        return

    def run() -> None:
        try:
            print("[qingzhou-agent] warming TTS engine...", file=sys.stderr, flush=True)
            warm_tts_engine()
            print("[qingzhou-agent] TTS engine ready.", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - optional TTS must not block backend startup.
            print(f"[qingzhou-agent] TTS warm failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=run, name="qingzhou-tts-warmup", daemon=True).start()

MODEL_SPEC = f"{LLM_ADAPTER_TYPE}:{LLM_MODEL}"
MODEL = (
    init_chat_model(
        MODEL_SPEC,
        **provider_model_kwargs(
            adapter=LLM_ADAPTER_TYPE,
            auth_token=LLM_AUTH_TOKEN,
            base_url=LLM_BASE_URL,
        ),
    )
    if LLM_AUTH_TOKEN and LLM_ADAPTER_TYPE.lower() == "anthropic"
    else MODEL_SPEC
)

MCP_TOOLS = load_mcp_tools()
AGENT_TOOLS = [*ALL_TOOLS, *MCP_TOOLS]
SKILL_CATALOG = skill_catalog_for_prompt()
PROMPT_CONTEXT = build_prompt_context(
    tools=AGENT_TOOLS,
    skill_catalog=SKILL_CATALOG,
    workspace=Path.cwd(),
    frontend_url=FRONTEND_URL,
)

middleware = [
    AgentMemoryMiddleware(),
    AgentCommandMiddleware(),
    AgentContextReferenceMiddleware(),
    AgentContextCompactMiddleware(),
    AgentPermissionMiddleware(),
]

# Keep interaction logging opt-in so normal chat requests do not create JSONL files.
if is_agent_logging_enabled():
    middleware.append(AgentLoggingMiddleware())

def _create_agent_graph(*, checkpointer=None):
    return create_agent(
        model=MODEL,
        tools=AGENT_TOOLS,
        middleware=middleware,
        state_schema=XuAgentState,
        system_prompt=get_system_prompt(PROMPT_CONTEXT),
        checkpointer=checkpointer,
    )


graph = _create_agent_graph()
_warm_tts_engine_in_background()

def _start_gateway_bridges_in_background() -> None:
    if QINGZHOU_CLI_MODE:
        return

    def run() -> None:
        try:
            start_cron_scheduler()
            start_lark_ws_bridge(_create_agent_graph(checkpointer=InMemorySaver()))
            start_botpy_bridge(graph)
            start_weixin_bridge(graph)
            start_telegram_bridge(graph)
            start_discord_bridge(graph)
        except Exception as exc:  # noqa: BLE001 - optional bridges must not block backend startup.
            print(f"[qingzhou-agent] gateway bridge startup failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=run, name="qingzhou-gateway-startup", daemon=True).start()


_start_gateway_bridges_in_background()
