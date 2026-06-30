"""Function-calling agent built with LangChain."""

import sys
import os
from pathlib import Path

# Make backend/src importable when LangGraph loads agent.py by file path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from agent_config import config_str
from agent_commands import AgentCommandMiddleware
from agent_context import AgentContextCompactMiddleware, XuAgentState
from agent_cron import start_cron_scheduler
from agent_botpy import start_botpy_bridge
from agent_logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent_lark import start_lark_ws_bridge
from agent_memory import AgentMemoryMiddleware
from agent_mcp import load_mcp_tools
from agent_permissions import AgentPermissionMiddleware
from agent_prompt import build_prompt_context, get_system_prompt
from llm_config import configure_provider_env, provider_model_kwargs
from skills import skill_catalog_for_prompt
from tools import ALL_TOOLS

LLM_ADAPTER_TYPE = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
LLM_MODEL = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
LLM_AUTH_TOKEN = os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").strip()


def configure_llm_provider_env() -> None:
    """Map project-level LLM_* settings to provider-specific LangChain env vars."""
    configure_provider_env(
        adapter=LLM_ADAPTER_TYPE,
        api_key=LLM_API_KEY,
        auth_token=LLM_AUTH_TOKEN,
        base_url=LLM_BASE_URL,
    )


configure_llm_provider_env()

MODEL_SPEC = f"{LLM_ADAPTER_TYPE}:{LLM_MODEL}"
MODEL = (
    init_chat_model(
        MODEL_SPEC,
        **provider_model_kwargs(adapter=LLM_ADAPTER_TYPE, auth_token=LLM_AUTH_TOKEN),
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
    AgentContextCompactMiddleware(),
    AgentPermissionMiddleware(),
]

# Keep interaction logging opt-in so normal chat requests do not create JSONL files.
if is_agent_logging_enabled():
    middleware.append(AgentLoggingMiddleware())

graph = create_agent(
    model=MODEL,
    tools=AGENT_TOOLS,
    middleware=middleware,
    state_schema=XuAgentState,
    system_prompt=get_system_prompt(PROMPT_CONTEXT),
)

start_cron_scheduler()
start_lark_ws_bridge(graph)
start_botpy_bridge(graph)
