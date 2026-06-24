"""Function-calling agent built with LangChain."""

import sys
import os
from pathlib import Path

# Make backend/src importable when LangGraph loads agent.py by file path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain.agents import create_agent

from agent_config import config_str
from agent_context import AgentContextCompactMiddleware, XuAgentState
from agent_logging import AgentLoggingMiddleware, is_agent_logging_enabled
from agent_memory import AgentMemoryMiddleware
from agent_permissions import AgentPermissionMiddleware
from agent_prompt import build_prompt_context, get_system_prompt
from skills import skill_catalog_for_prompt
from tools import ALL_TOOLS

LLM_ADAPTER_TYPE = os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip()
LLM_MODEL = os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip()
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").strip()


def configure_llm_provider_env() -> None:
    """Map project-level LLM_* settings to provider-specific LangChain env vars."""
    provider = LLM_ADAPTER_TYPE.lower()

    if provider == "anthropic":
        if LLM_API_KEY:
            os.environ["ANTHROPIC_API_KEY"] = LLM_API_KEY
        if LLM_BASE_URL:
            os.environ["ANTHROPIC_API_URL"] = LLM_BASE_URL
    elif provider == "openai":
        if LLM_API_KEY:
            os.environ["OPENAI_API_KEY"] = LLM_API_KEY
        if LLM_BASE_URL:
            os.environ["OPENAI_BASE_URL"] = LLM_BASE_URL


configure_llm_provider_env()

SKILL_CATALOG = skill_catalog_for_prompt()
PROMPT_CONTEXT = build_prompt_context(
    tools=ALL_TOOLS,
    skill_catalog=SKILL_CATALOG,
    workspace=Path.cwd(),
    frontend_url=FRONTEND_URL,
)

middleware = [
    AgentMemoryMiddleware(),
    AgentContextCompactMiddleware(),
    AgentPermissionMiddleware(),
]

# Keep interaction logging opt-in so normal chat requests do not create JSONL files.
if is_agent_logging_enabled():
    middleware.append(AgentLoggingMiddleware())

graph = create_agent(
    model=f"{LLM_ADAPTER_TYPE}:{LLM_MODEL}",
    tools=ALL_TOOLS,
    middleware=middleware,
    state_schema=XuAgentState,
    system_prompt=get_system_prompt(PROMPT_CONTEXT),
)
