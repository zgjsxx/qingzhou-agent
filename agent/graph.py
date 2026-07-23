"""Function-calling agent built with LangChain."""

import sys
import os
import threading
import weakref
from pathlib import Path
from typing import Any

# Make the repository root importable when LangGraph loads this file by path.
AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import config_str, start_config_watcher
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

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").strip()
QINGZHOU_CLI_MODE = os.getenv("QINGZHOU_CLI", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_RELOADABLE_GRAPHS: weakref.WeakSet["ReloadableAgentGraph"] = weakref.WeakSet()
_RELOADABLE_GRAPHS_LOCK = threading.RLock()


def _llm_settings() -> dict[str, str]:
    return {
        "adapter": os.getenv("LLM_ADAPTER_TYPE", config_str("llm", "adapterType", "anthropic")).strip(),
        "model": os.getenv("LLM_MODEL", config_str("llm", "model", "glm-5.1")).strip(),
        "api_key": os.getenv("LLM_API_KEY", config_str("llm", "apiKey", "")).strip(),
        "auth_token": os.getenv("LLM_AUTH_TOKEN", os.getenv("ANTHROPIC_AUTH_TOKEN", "")).strip(),
        "base_url": os.getenv("LLM_BASE_URL", config_str("llm", "baseUrl", "")).strip(),
    }


def configure_llm_provider_env(settings: dict[str, str] | None = None) -> None:
    """Map project-level LLM_* settings to provider-specific LangChain env vars."""
    settings = settings or _llm_settings()
    configure_provider_env(
        adapter=settings["adapter"],
        api_key=settings["api_key"],
        auth_token=settings["auth_token"],
        base_url=settings["base_url"],
    )


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


def _build_agent_runtime() -> dict[str, Any]:
    settings = _llm_settings()
    configure_llm_provider_env(settings)
    model_spec = f"{settings['adapter']}:{settings['model']}"
    model = (
        init_chat_model(
            model_spec,
            **provider_model_kwargs(
                adapter=settings["adapter"],
                auth_token=settings["auth_token"],
                base_url=settings["base_url"],
            ),
        )
        if settings["auth_token"] and settings["adapter"].lower() == "anthropic"
        else model_spec
    )
    tools = [*ALL_TOOLS, *load_mcp_tools()]
    prompt_context = build_prompt_context(
        tools=tools,
        skill_catalog=skill_catalog_for_prompt(),
        workspace=REPO_ROOT,
        frontend_url=FRONTEND_URL,
    )
    return {
        "model": model,
        "tools": tools,
        "prompt_context": prompt_context,
    }


def _create_agent_graph(*, checkpointer=None):
    runtime = _build_agent_runtime()
    return create_agent(
        model=runtime["model"],
        tools=runtime["tools"],
        middleware=middleware,
        state_schema=XuAgentState,
        system_prompt=get_system_prompt(runtime["prompt_context"]),
        checkpointer=checkpointer,
    )


class ReloadableAgentGraph:
    """Thread-safe proxy whose target graph is replaced when config changes."""

    def __init__(self, *, checkpointer=None):
        self._checkpointer = checkpointer
        self._lock = threading.RLock()
        self._graph = _create_agent_graph(checkpointer=checkpointer)
        with _RELOADABLE_GRAPHS_LOCK:
            _RELOADABLE_GRAPHS.add(self)

    def reload(self) -> None:
        next_graph = _create_agent_graph(checkpointer=self._checkpointer)
        with self._lock:
            self._graph = next_graph

    def _target(self) -> Any:
        with self._lock:
            return self._graph

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


def _reload_agent_graphs_from_config() -> None:
    print("[qingzhou-agent] config changed; reloading agent graph...", file=sys.stderr, flush=True)
    with _RELOADABLE_GRAPHS_LOCK:
        graphs = list(_RELOADABLE_GRAPHS)
    for item in graphs:
        item.reload()
    print("[qingzhou-agent] agent graph reloaded.", file=sys.stderr, flush=True)


start_config_watcher(_reload_agent_graphs_from_config)
_warm_tts_engine_in_background()


def graph():
    """LangGraph Server factory entrypoint."""
    return _create_agent_graph()

def _start_gateway_bridges_in_background() -> None:
    if QINGZHOU_CLI_MODE:
        return

    def run() -> None:
        try:
            start_cron_scheduler()
            gateway_graph = ReloadableAgentGraph()
            start_lark_ws_bridge(ReloadableAgentGraph(checkpointer=InMemorySaver()))
            start_botpy_bridge(gateway_graph)
            start_weixin_bridge(gateway_graph)
            start_telegram_bridge(gateway_graph)
            start_discord_bridge(gateway_graph)
        except Exception as exc:  # noqa: BLE001 - optional bridges must not block backend startup.
            print(f"[qingzhou-agent] gateway bridge startup failed: {exc}", file=sys.stderr, flush=True)

    threading.Thread(target=run, name="qingzhou-gateway-startup", daemon=True).start()


_start_gateway_bridges_in_background()
